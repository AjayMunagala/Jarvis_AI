"""Browser checks expressed with the accepted v2.5 verification contracts.

A *planned* check is never reported as executed: planning returns
``not_checked`` items. Running checks requires ``JARVIS_BROWSER_CHECKS_ENABLED``;
when it is off every check is ``check_blocked`` with ``execution_enabled=False``.
Checks open a page through the browser provider only; they never use the
v2.5 process runner and do not change ``verification_execution_enabled``.
A check passes only when every assertion was evaluated against an observed page.
"""

import hashlib
import json
from datetime import datetime, timezone
from time import perf_counter

from jarvis.browser.models import (
    AssertionResult, BrowserAssertion, BrowserCheckPlan, BrowserCheckPlanItem, BrowserCheckRequest, PageObservation,
)
from jarvis.browser.providers import ProviderError
from jarvis.browser.service import BrowserService
from jarvis.browser.url_policy import BlockedURL
from jarvis.verification.models import (
    CheckResult, CheckScope, VerificationEvidence, VerificationReport, VerificationState,
)

PROFILE_ID = "browser"
_SEVERITY = [
    VerificationState.CHECK_PASSED, VerificationState.INCONCLUSIVE, VerificationState.CHECK_RESULT_UNAVAILABLE,
    VerificationState.CHECK_TIMED_OUT, VerificationState.CHECK_FAILED, VerificationState.CHECK_BLOCKED,
]


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(*parts: str, length: int = 12) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:length]


def check_id(check: BrowserCheckRequest) -> str:
    return "CHK-" + _digest(check.name, check.url, json.dumps([item.model_dump() for item in check.assertions]))


def plan_checks(checks: tuple[BrowserCheckRequest, ...]) -> BrowserCheckPlan:
    items = tuple(
        BrowserCheckPlanItem(check_id=check_id(item), name=item.name, url=item.url, assertions=item.assertions)
        for item in checks
    )
    return BrowserCheckPlan(
        plan_id="VP-" + _digest(*(item.check_id for item in items)),
        checks=items,
        note="Planned only: no page was opened and no assertion was evaluated.",
    )


def evaluate(assertion: BrowserAssertion, page: PageObservation) -> AssertionResult:
    expected = assertion.expected
    kind = assertion.kind
    text_lower = page.text.lower()
    if kind == "url_equals":
        observed, ok = page.url, page.url == expected
    elif kind == "url_contains":
        observed, ok = page.url, expected in page.url
    elif kind == "title_contains":
        observed, ok = page.title, expected.lower() in page.title.lower()
    elif kind == "text_present":
        ok = bool(expected) and expected.lower() in text_lower
        observed = "present" if ok else "absent"
    elif kind == "text_absent":
        ok = bool(expected) and expected.lower() not in text_lower
        observed = "absent" if ok else "present"
    elif kind == "element_present":
        matches = [item for item in page.elements
                   if expected.lower() in f"{item.label} {item.text} {item.name or ''}".lower()]
        ok, observed = bool(expected) and bool(matches), f"{len(matches)} matching element(s)"
    elif kind == "status_code":
        observed = str(page.http_status) if page.http_status is not None else "unknown"
        if page.http_status is None:
            return AssertionResult(kind=kind, expected=expected, observed=observed, outcome="not_evaluated")
        ok = observed == expected.strip()
    elif kind == "no_console_errors":
        errors = [item for item in page.console_errors if item.level == "error"]
        observed, ok = f"{len(errors)} console error(s)", not errors
    else:  # no_network_failures
        observed, ok = f"{len(page.network_failures)} network failure(s)", not page.network_failures
    return AssertionResult(kind=kind, expected=expected, observed=observed[:500], outcome="pass" if ok else "fail")


def _evidence(cid, name, state, started_at, duration, excerpt, limitations, error=None) -> VerificationEvidence:
    return VerificationEvidence(
        evidence_id="VE-" + _digest(cid, started_at), check_id=cid, check_name=name, profile_id=PROFILE_ID,
        state=state, command_executed=None, exit_code=None, started_at=started_at, completed_at=_iso(),
        duration_seconds=round(duration, 3), output_excerpt=excerpt[:16000], output_truncated=len(excerpt) > 16000,
        redacted_error_details=error, provenance="direct_execution", limitations=tuple(limitations[:10]),
    )


def run_checks(service: BrowserService, checks: tuple[BrowserCheckRequest, ...], *, execution_id: str) -> VerificationReport:
    plan = plan_checks(checks)
    enabled = bool(service.settings.browser_checks_enabled)
    results = []
    for request, item in zip(checks, plan.checks):
        started_at, started = _iso(), perf_counter()
        if not enabled:
            evidence = _evidence(item.check_id, item.name, VerificationState.CHECK_BLOCKED, started_at, 0.0, "",
                                 ["Browser check execution is disabled (JARVIS_BROWSER_CHECKS_ENABLED=false)."])
            evidence = evidence.model_copy(update={"provenance": "static_analysis"})
            results.append(CheckResult(check_id=item.check_id, name=item.name, scope=CheckScope.INTEGRATION,
                                       state=VerificationState.CHECK_BLOCKED, evidence=evidence,
                                       summary="Not executed: browser check execution is disabled."))
            continue
        try:
            page = service.fetch_observation(request.url)
        except BlockedURL as exc:
            state, summary, error, page = VerificationState.CHECK_BLOCKED, exc.public_message, exc.code, None
        except ProviderError as exc:
            state = VerificationState.CHECK_TIMED_OUT if exc.code == "timeout" else VerificationState.CHECK_RESULT_UNAVAILABLE
            summary, error, page = exc.public_message, exc.code, None
        if page is None:
            evidence = _evidence(item.check_id, item.name, state, started_at, perf_counter() - started, "",
                                 ["The page was not observed; no assertion was evaluated."], error)
            results.append(CheckResult(check_id=item.check_id, name=item.name, scope=CheckScope.INTEGRATION,
                                       state=state, evidence=evidence, summary=summary[:1000]))
            continue
        outcomes = [evaluate(assertion, page) for assertion in request.assertions]
        if any(result.outcome == "fail" for result in outcomes):
            state = VerificationState.CHECK_FAILED
        elif any(result.outcome == "not_evaluated" for result in outcomes):
            state = VerificationState.INCONCLUSIVE
        else:
            state = VerificationState.CHECK_PASSED
        excerpt = "\n".join(
            f"{result.outcome.upper()} {result.kind} expected={result.expected!r} observed={result.observed!r}"
            for result in outcomes
        ) + f"\nobserved_url={page.url} observation_id={page.observation_id} sha256={page.content_sha256}"
        limitations = list(page.limitations) or ["Assertions were evaluated against one observed page state."]
        evidence = _evidence(item.check_id, item.name, state, started_at, perf_counter() - started, excerpt, limitations)
        passed = sum(result.outcome == "pass" for result in outcomes)
        results.append(CheckResult(
            check_id=item.check_id, name=item.name, scope=CheckScope.INTEGRATION, state=state, evidence=evidence,
            summary=f"{passed} of {len(outcomes)} assertion(s) passed on the observed page.",
            diagnostics=tuple(f"{result.kind}: {result.outcome}" for result in outcomes)[:10],
        ))
    overall = max((result.state for result in results), key=_SEVERITY.index)
    executed = enabled and any(result.state != VerificationState.CHECK_BLOCKED for result in results)
    return VerificationReport(
        execution_id=execution_id, plan_id=plan.plan_id, overall_state=overall, results=tuple(results),
        summary=(f"{sum(result.state == VerificationState.CHECK_PASSED for result in results)} of {len(results)} "
                 f"browser check(s) passed." if enabled else "Browser checks were planned but not executed."),
        facts=tuple(f"{result.name}: {result.state.value}" for result in results)[:16],
        limitations=(
            "Browser checks observe a single page load per check; they are not end-to-end test suites.",
            f"Provider: {service.provider.name}.",
        ),
        execution_enabled=executed,
    )
