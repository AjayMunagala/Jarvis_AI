"""Bounded browser sessions: lifecycle, observation, enforced actions, and audit.

All permission and safety decisions happen here, in trusted code, before a
provider is called. Page content never influences what is allowed.
"""

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic, perf_counter
from uuid import UUID

from jarvis.browser.actions import classify
from jarvis.browser.extraction import Extraction, ExtractedElement, extract_page
from jarvis.browser.models import (
    ActionRecord, ActionResponse, AuditEventView, BrowserActionRequest, BrowserSessionView, BrowserStatus,
    ConsoleEntry, NetworkFailure, PageElement, PageForm, PageObservation, ScreenshotRecord, SelectOption,
    SessionLimits,
)
from jarvis.browser.providers import BrowserProvider, ContextLimits, ProviderError, RawPage, build_provider
from jarvis.browser.redaction import contains_secret, redact_text, sanitize_url
from jarvis.browser.url_policy import BlockedURL, URLPolicy

logger = logging.getLogger(__name__)

MAX_OBSERVATIONS_KEPT = 5
MAX_SCREENSHOTS = 5
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_AUDIT_EVENTS = 200
MAX_ACTION_RECORDS = 60
LOOP_REPEAT_LIMIT = 3
LOCK_WAIT_SECONDS = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


class BrowserServiceError(Exception):
    """Client-safe failure with an HTTP status hint."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.public_message = message


@dataclass
class _StoredObservation:
    view: PageObservation
    extraction: Extraction
    elements: dict[str, ExtractedElement]


@dataclass
class _Session:
    session_id: str
    owner_id: UUID
    created_at: datetime
    expires_at: datetime
    expires_monotonic: float
    context: object
    state: str = "active"
    navigations_used: int = 0
    actions_used: int = 0
    sequence: int = 0
    observation_sequence: int = 0
    current_url: str | None = None
    observations: dict[str, _StoredObservation] = field(default_factory=dict)
    last_observation_id: str | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    screenshots: dict[str, tuple[ScreenshotRecord, bytes]] = field(default_factory=dict)
    audit: list[AuditEventView] = field(default_factory=list)
    recent_signatures: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class BrowserService:
    def __init__(self, settings, provider: BrowserProvider | None = None, *, policy: URLPolicy | None = None):
        self.settings = settings
        self.provider = provider or build_provider(settings.browser_provider)
        self.policy = self.provider.effective_policy(policy or URLPolicy(
            allow_private_network=settings.browser_allow_private_network,
            allowed_domains=_domains(settings.browser_allowed_domains),
            blocked_domains=_domains(settings.browser_blocked_domains),
        ))
        self.limits = SessionLimits(
            max_navigations=settings.browser_max_navigations,
            max_actions=settings.browser_max_actions,
            navigation_timeout_seconds=settings.browser_navigation_timeout_seconds,
            session_ttl_seconds=settings.browser_session_ttl_seconds,
            max_text_chars=settings.browser_max_text_chars,
            max_elements=settings.browser_max_elements,
        )
        self.context_limits = ContextLimits(
            navigation_timeout_seconds=settings.browser_navigation_timeout_seconds,
            max_page_bytes=settings.browser_max_page_bytes,
        )
        self._sessions: dict[str, _Session] = {}
        self._registry_lock = threading.Lock()

    # ------------------------------------------------------------- status
    def status(self) -> BrowserStatus:
        available, detail = self.provider.availability()
        return BrowserStatus(
            enabled=self.settings.browser_enabled, provider=self.provider.name,
            provider_available=available, provider_detail=detail,
            capabilities=self.provider.capabilities, limits=self.limits,
            private_network_allowed=self.policy.allow_private_network,
            allowed_domains=self.policy.allowed_domains,
        )

    # ---------------------------------------------------------- lifecycle
    def create_session(self, owner_id: UUID, url: str | None = None) -> BrowserSessionView:
        self.cleanup_expired()
        available, _ = self.provider.availability()
        if not available:
            raise BrowserServiceError(503, "provider_unavailable", "The configured browser provider is unavailable.")
        if url is not None:
            self._check_url(url)  # fail fast before any browser starts
        with self._registry_lock:
            active = [item for item in self._sessions.values() if item.state == "active"]
            if len(active) >= self.settings.browser_max_sessions:
                raise BrowserServiceError(409, "session_limit", "The maximum number of browser sessions is open.")
            try:
                context = self.provider.open_context(self.policy, self.context_limits)
            except ProviderError as exc:
                raise BrowserServiceError(503, exc.code, exc.public_message) from exc
            created = _now()
            session = _Session(
                session_id="BS-" + secrets.token_hex(12), owner_id=owner_id, created_at=created,
                expires_at=created + timedelta(seconds=self.limits.session_ttl_seconds),
                expires_monotonic=monotonic() + self.limits.session_ttl_seconds, context=context,
            )
            self._sessions[session.session_id] = session
        self._audit(session, "session_created", "ok", f"provider={self.provider.name}")
        if url is not None:
            self.act(owner_id, session.session_id, BrowserActionRequest(action="navigate", url=url))
        return self._view(session)

    def list_sessions(self, owner_id: UUID) -> list[BrowserSessionView]:
        self.cleanup_expired()
        return [self._view(item) for item in self._sessions.values() if item.owner_id == owner_id]

    def get_session(self, owner_id: UUID, session_id: str) -> BrowserSessionView:
        return self._view(self._owned(owner_id, session_id, require_active=False))

    def close_session(self, owner_id: UUID, session_id: str) -> BrowserSessionView:
        session = self._owned(owner_id, session_id, require_active=False)
        self._close(session, "closed", None)
        return self._view(session)

    def cleanup_expired(self) -> None:
        now = monotonic()
        for session in list(self._sessions.values()):
            if session.state == "active" and now >= session.expires_monotonic:
                self._close(session, "expired", "The session reached its time limit.")
        # Keep closed sessions briefly for review, bounded in number.
        inactive = [item for item in self._sessions.values() if item.state != "active"]
        for session in sorted(inactive, key=lambda item: item.created_at)[:-10]:
            self._sessions.pop(session.session_id, None)

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            self._close(session, "closed", None)

    def _close(self, session: _Session, state: str, reason: str | None) -> None:
        if session.state == "active":
            try:
                session.context.close()
            except Exception:
                pass
            session.state = state
            session.failure_reason = reason
            # Drop page content and images; keep the action record for review.
            session.screenshots.clear()
            session.observations = {
                key: value for key, value in session.observations.items() if key == session.last_observation_id
            }
            self._audit(session, f"session_{state}", "ok", reason or "")

    def _owned(self, owner_id: UUID, session_id: str, *, require_active: bool = True) -> _Session:
        self.cleanup_expired()
        session = self._sessions.get(session_id)
        if session is None or session.owner_id != owner_id:
            # Same response for foreign and unknown ids.
            raise BrowserServiceError(404, "session_not_found", "The browser session was not found.")
        if require_active and session.state != "active":
            raise BrowserServiceError(409, "session_inactive", f"The browser session is {session.state}.")
        return session

    # ------------------------------------------------------------ actions
    def act(self, owner_id: UUID, session_id: str, request: BrowserActionRequest) -> ActionResponse:
        session = self._owned(owner_id, session_id)
        if not session.lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise BrowserServiceError(409, "session_busy", "Another action is running in this session.")
        try:
            record = self._act_locked(session, request)
        finally:
            session.lock.release()
        return ActionResponse(record=record, session=self._view(session))

    def _act_locked(self, session: _Session, request: BrowserActionRequest) -> ActionRecord:
        started_at, started = _now(), perf_counter()
        action = request.action
        stored = session.observations.get(session.last_observation_id) if session.last_observation_id else None
        element: ExtractedElement | None = None
        form = None

        def finish(classification, outcome, reason, *, url_after=None, transition="none",
                   observation_id=None, screenshot_id=None) -> ActionRecord:
            session.sequence += 1
            record = ActionRecord(
                action_id="ACT-" + secrets.token_hex(6), sequence=session.sequence, action=action,
                classification=classification, outcome=outcome, reason=reason,
                element_id=element.element_id if element else None,
                element_label=(element.label or element.text)[:200] if element else "",
                started_at=_iso(started_at), duration_ms=round((perf_counter() - started) * 1000, 3),
                url_before=sanitize_url(url_before) if url_before else None,
                url_after=sanitize_url(url_after) if url_after else None,
                transition=transition, resulting_observation_id=observation_id, screenshot_id=screenshot_id,
            )
            session.actions.append(record)
            del session.actions[:-MAX_ACTION_RECORDS]
            self._audit(session, f"action_{action}", outcome, f"{classification}: {reason}")
            logger.info("Browser action recorded", extra={
                "outcome": outcome, "error_code": f"{action}:{classification}",
                "duration_ms": record.duration_ms,
            })
            return record

        url_before = session.current_url

        # 1. Bounds (checked before anything else; blocked attempts still count as attempts).
        if session.actions_used >= self.limits.max_actions:
            return finish("blocked", "blocked", "The session action limit has been reached.")
        if action in {"navigate", "back", "reload"} and session.navigations_used >= self.limits.max_navigations:
            return finish("navigate", "blocked", "The session navigation limit has been reached.")
        session.actions_used += 1

        # 2. Element-bound actions must target the latest observation (stale detection).
        if action in {"click", "type", "select"}:
            if stored is None:
                return finish("blocked", "blocked", "Observe a page before interacting with it.")
            if request.observation_id != session.last_observation_id:
                return finish("blocked", "blocked",
                              "The page changed since the referenced observation; observe again before acting.")
            element = stored.elements.get(request.element_id or "")
            if element is None:
                return finish("blocked", "blocked", "The element id is not present in the latest observation.")
            form = next((item for item in stored.extraction.forms if item.form_id == element.form_id), None)
            if element.disabled:
                return finish("blocked", "blocked", "The element is disabled or read-only.")

        classification, reason = classify(action, element, form)
        if classification == "blocked":
            return finish("blocked", "blocked", reason)
        if classification == "consequential":
            # Never executed in v2.6, regardless of what the page or model requests.
            return finish("consequential", "approval_required",
                          f"{reason} Owner approval is required; the action was not performed.")

        # 3. Field-level rules.
        if action == "type":
            if element.kind not in {"text_input", "textarea"}:
                return finish(classification, "blocked", "Text can only be typed into text fields.")
            if element.sensitive:
                return finish(classification, "blocked",
                              "Typing into password, payment, one-time-code, or other sensitive fields is not allowed.")
            if request.text is None:
                return finish(classification, "blocked", "Text is required.")
            if contains_secret(request.text):
                return finish(classification, "blocked", "The text appears to contain a credential and was not typed.")
        if action == "select":
            if element.kind != "select":
                return finish(classification, "blocked", "Options can only be chosen in select fields.")
            if request.option not in {value for value, _, _ in element.options}:
                return finish(classification, "blocked", "The option is not one of the observed choices.")
        if action == "click" and element.sensitive:
            return finish(classification, "blocked", "Sensitive controls such as file inputs are not supported.")
        if action == "scroll" and request.direction is None:
            return finish(classification, "blocked", "A scroll direction is required.")
        if action == "navigate" and not request.url:
            return finish(classification, "blocked", "A URL is required.")

        # 4. Provider support.
        capabilities = self.provider.capabilities
        if action in {"type", "select", "scroll"} and not capabilities.interaction:
            return finish(classification, "unsupported", f"The {self.provider.name} provider does not support {action}.")
        if action == "click" and element.kind != "link" and not capabilities.interaction:
            return finish(classification, "unsupported", f"The {self.provider.name} provider can only follow links.")
        if action == "screenshot" and not capabilities.screenshots:
            return finish(classification, "unsupported", f"The {self.provider.name} provider cannot take screenshots.")
        if action == "click" and element.kind == "link":
            if session.navigations_used >= self.limits.max_navigations:
                return finish("navigate", "blocked", "The session navigation limit has been reached.")

        # 5. Loop detection on repeated identical requests against an unchanged page.
        signature = hashlib.sha256(
            f"{action}|{request.element_id}|{request.url}|{request.text}|{request.option}|"
            f"{stored.view.content_sha256 if stored else ''}".encode()
        ).hexdigest()
        session.recent_signatures = (session.recent_signatures + [signature])[-LOOP_REPEAT_LIMIT:]
        if action not in {"observe", "screenshot"} and len(session.recent_signatures) == LOOP_REPEAT_LIMIT \
                and len(set(session.recent_signatures)) == 1:
            return finish(classification, "blocked", "Repeated identical action on an unchanged page (loop detected).")

        # 6. Execute through the provider.
        context = session.context
        try:
            if action == "screenshot":
                image = context.screenshot()
                if not image or len(image) > MAX_SCREENSHOT_BYTES:
                    return finish(classification, "failed", "The screenshot was empty or too large.")
                record = self._store_screenshot(session, image)
                return finish(classification, "performed", "Captured the current viewport.",
                              url_after=session.current_url, screenshot_id=record.screenshot_id)
            if action == "observe":
                if session.current_url is None:
                    return finish(classification, "blocked", "No page is open.")
                # Re-read the current page; observation is not counted as navigation.
                page = context.reload()
            elif action == "navigate":
                self._check_url(request.url)
                page = context.navigate(request.url)
            elif action == "back":
                page = context.back()
            elif action == "reload":
                page = context.reload()
            elif action == "click":
                page = context.click(element)
            elif action == "type":
                page = context.type_text(element, request.text)
            elif action == "select":
                page = context.select_option(element, request.option)
            else:
                page = context.scroll(request.direction)
        except BlockedURL as exc:
            return finish(classification, "blocked", exc.public_message)
        except ProviderError as exc:
            if exc.code == "disconnected":
                self._close(session, "failed", exc.public_message)
            if exc.code == "timeout":
                return finish(classification, "timed_out", exc.public_message)
            if exc.code == "unsupported":
                return finish(classification, "unsupported", exc.public_message)
            return finish(classification, "failed", exc.public_message)

        if action in {"navigate", "back", "reload"} or (action == "click" and page.url != url_before):
            session.navigations_used += 1
        observation = self._observe(session, page)
        url_after = page.url
        if action in {"type", "select", "scroll"}:
            transition = "unexpected_navigation" if url_after != url_before else "same_page"
        elif action == "observe":
            transition = "none"
        elif action == "click" and element.kind == "link":
            transition = "navigated" if url_after != url_before else "same_page"
        else:
            transition = "navigated" if url_after != url_before else "same_page"
        outcome_reason = {
            "navigate": "Navigated and observed the resulting page.",
            "back": "Went back and observed the page.",
            "reload": "Reloaded and observed the page.",
            "observe": "Observed the current page.",
            "click": "Clicked the observed element and observed the result.",
            "type": "Entered text into the observed field (not submitted).",
            "select": "Selected the observed option (not submitted).",
            "scroll": "Scrolled and observed the page.",
        }[action]
        if transition == "unexpected_navigation":
            outcome_reason += " The page navigated unexpectedly."
        return finish(classification, "performed", outcome_reason, url_after=url_after,
                      transition=transition, observation_id=observation.observation_id)

    def _check_url(self, url: str) -> str:
        return self.policy.check(url)

    # -------------------------------------------------------- observation
    def _observe(self, session: _Session, page: RawPage) -> PageObservation:
        observation = build_observation(
            page, provider=self.provider.name, session_id=session.session_id,
            sequence=session.observation_sequence + 1,
            max_text_chars=self.limits.max_text_chars, max_elements=self.limits.max_elements,
        )
        session.observation_sequence += 1
        session.current_url = page.url
        view, extraction = observation
        session.observations[view.observation_id] = _StoredObservation(
            view=view, extraction=extraction, elements={item.element_id: item for item in extraction.elements},
        )
        session.last_observation_id = view.observation_id
        if len(session.observations) > MAX_OBSERVATIONS_KEPT:
            for key in list(session.observations)[:-MAX_OBSERVATIONS_KEPT]:
                session.observations.pop(key, None)
        return view

    def _store_screenshot(self, session: _Session, image: bytes) -> ScreenshotRecord:
        record = ScreenshotRecord(
            screenshot_id="SHOT-" + secrets.token_hex(6), observation_id=session.last_observation_id,
            captured_at=_iso(_now()), byte_size=len(image), sha256=hashlib.sha256(image).hexdigest(),
        )
        session.screenshots[record.screenshot_id] = (record, image)
        while len(session.screenshots) > MAX_SCREENSHOTS:
            session.screenshots.pop(next(iter(session.screenshots)))
        return record

    def screenshot_bytes(self, owner_id: UUID, session_id: str, screenshot_id: str) -> bytes:
        session = self._owned(owner_id, session_id, require_active=False)
        stored = session.screenshots.get(screenshot_id)
        if stored is None:
            raise BrowserServiceError(404, "screenshot_not_found", "The screenshot was not found.")
        return stored[1]

    def audit_events(self, owner_id: UUID, session_id: str) -> list[AuditEventView]:
        return list(self._owned(owner_id, session_id, require_active=False).audit)

    def _audit(self, session: _Session, event: str, outcome: str, detail: str) -> None:
        session.audit.append(AuditEventView(
            sequence=len(session.audit) + 1, timestamp=_iso(_now()), event=event[:200],
            outcome=outcome[:200], detail=redact_text(detail)[0][:1000],
        ))
        del session.audit[:-MAX_AUDIT_EVENTS]

    def _view(self, session: _Session) -> BrowserSessionView:
        last = session.observations.get(session.last_observation_id) if session.last_observation_id else None
        return BrowserSessionView(
            session_id=session.session_id, state=session.state, provider=self.provider.name,
            capabilities=self.provider.capabilities, created_at=_iso(session.created_at),
            expires_at=_iso(session.expires_at),
            current_url=sanitize_url(session.current_url) if session.current_url else None,
            navigations_used=session.navigations_used, actions_used=session.actions_used, limits=self.limits,
            last_observation=last.view if last else None, actions=tuple(session.actions),
            screenshots=tuple(record for record, _ in session.screenshots.values()),
            pending_approvals=tuple(item for item in session.actions if item.outcome == "approval_required"),
            failure_reason=session.failure_reason,
        )

    # ------------------------------------------------ ephemeral page reads
    def fetch_observation(self, url: str) -> PageObservation:
        """Open a short-lived context, observe one page, and close it (research/checks)."""
        self.policy.check(url)
        context = self.provider.open_context(self.policy, self.context_limits)
        try:
            page = context.navigate(url)
            return build_observation(
                page, provider=self.provider.name, session_id=None, sequence=0,
                max_text_chars=self.limits.max_text_chars, max_elements=self.limits.max_elements,
            )[0]
        finally:
            try:
                context.close()
            except Exception:
                pass


def build_observation(
    page: RawPage, *, provider: str, session_id: str | None, sequence: int, max_text_chars: int, max_elements: int,
) -> tuple[PageObservation, Extraction]:
    extraction = extract_page(page.html, base_url=page.url, max_text_chars=max_text_chars, max_elements=max_elements)
    limitations = []
    if not page.javascript_executed:
        limitations.append("JavaScript was not executed; dynamically rendered content may be missing.")
    if extraction.text_truncated:
        limitations.append(f"Page text was truncated to {max_text_chars} characters.")
    if extraction.elements_truncated:
        limitations.append(f"Only the first {max_elements} interactive elements were recorded.")
    if extraction.skipped_links:
        limitations.append(f"{extraction.skipped_links} non-HTTP link(s) (javascript:, mailto:, file:, ...) were excluded.")
    elements = tuple(
        PageElement(
            element_id=item.element_id, kind=item.kind, tag=item.tag[:200], label=item.label, text=item.text,
            href=sanitize_url(item.href) if item.href else None, input_type=item.input_type, name=item.name,
            form_id=item.form_id, required=item.required, disabled=item.disabled, sensitive=item.sensitive,
            options=tuple(SelectOption(value=value[:200], label=label[:200], selected=selected)
                          for value, label, selected in item.options),
        )
        for item in extraction.elements
    )
    forms = tuple(
        PageForm(form_id=item.form_id, action=sanitize_url(item.action) if item.action else None,
                 method=item.method, label=item.label, element_ids=tuple(item.element_ids[:60]))
        for item in extraction.forms
    )
    console = tuple(ConsoleEntry(level=level, message=redact_text(message)[0][:1000])
                    for level, message in page.console[:20])
    failures = tuple(
        NetworkFailure(url=item.get("url") or "[unknown]", method=str(item.get("method") or "GET")[:200],
                       status=item.get("status"), error=str(item.get("error") or "")[:200],
                       resource_type=str(item.get("resource_type") or "")[:200])
        for item in page.network_failures[:20]
    )
    view = PageObservation(
        observation_id="OBS-" + secrets.token_hex(6), session_id=session_id, sequence=sequence,
        url=sanitize_url(page.url), title=extraction.title[:200], retrieved_at=_iso(_now()),
        http_status=page.status, content_sha256=hashlib.sha256(page.html.encode("utf-8", "replace")).hexdigest(),
        text=extraction.text, text_truncated=extraction.text_truncated, headings=tuple(extraction.headings),
        elements=elements, elements_truncated=extraction.elements_truncated, forms=forms,
        console_errors=console, network_failures=failures, provider=provider,
        javascript_executed=page.javascript_executed, redaction_count=extraction.redaction_count,
        limitations=tuple(limitations),
    )
    return view, extraction


def _domains(raw: str) -> tuple[str, ...]:
    return tuple(sorted({item.strip().lower().rstrip(".") for item in (raw or "").split(",") if item.strip()}))
