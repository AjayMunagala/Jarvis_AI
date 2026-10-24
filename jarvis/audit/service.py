"""Central audit trail and approval gateway services (v2.9).

- ``AuditService.record`` writes to the configured store. If a durable write
  fails, the record is kept in the in-memory fallback marked ``durable=false``
  and the failure is counted; it is never reported as durable.
- ``ApprovalService.request`` records an approval request for an action the
  system refused to run (``approval_required``). ``approve``/``reject`` decide a
  pending, unexpired, owner-scoped request exactly once. Deciding never
  executes anything; every decision is audited.
- v4.3 enforcement: ``run_approved`` guards an enforced action (autonomous
  writes, improvement apply/restore, quarantine/restore). Without an approval
  ID it records a pending request bound to a SHA-256 of the exact action and
  raises ``ApprovalRequired`` (HTTP 202); nothing runs. With the ID of an
  approved request for the same action and scope, it marks the request used
  (once, atomically) and then runs the action, auditing the outcome. A request
  for a different action or a changed target, a pending, rejected, expired or
  already-used request, and a request approved longer ago than the approval
  window are refused with HTTP 409. If the gateway is missing or cannot store
  the request, nothing runs (HTTP 503).
"""

import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar
from uuid import UUID

from jarvis.audit.models import (ActivityPage, ApprovalPage, ApprovalProposal, ApprovalRequest, AuditRecord,
                                 execution_for)
from jarvis.audit.stores import MemoryApprovalStore, MemoryAuditStore, StorageError
from jarvis.core.correlation import get_correlation_id
from jarvis.repo.secret_redactor import redact_secrets

logger = logging.getLogger(__name__)
EXECUTION_NOTE = ("Approving a tool or browser request records your decision only; nothing is executed. "
                  "Autonomous, improvement and quarantine actions wait for your approval: once approved, the action "
                  "runs once, when it is submitted again with its approval ID within the approval window.")
T = TypeVar("T")


def scope_hash(category: str, action: str, scope_material: str) -> str:
    return hashlib.sha256(f"{category}|{action}|{scope_material}".encode("utf-8", "replace")).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _clean(value, limit: int = 200):
    if isinstance(value, str):
        return redact_secrets(value)[0][:limit]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:limit]


class AuditService:
    def __init__(self, store, *, fallback: MemoryAuditStore | None = None):
        self.store = store
        self.fallback = fallback or MemoryAuditStore()
        self.failures = 0
        self._lock = threading.Lock()

    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def record(self, *, owner_id: UUID, category: str, event_type: str, decision: str, status: str, subject: str,
               correlation_id: str | None = None, details: dict | None = None,
               occurred_at: str | None = None, event_id: str | None = None) -> AuditRecord:
        clean = {str(key)[:60]: _clean(value) for key, value in list((details or {}).items())[:12]}
        correlation_id = correlation_id or get_correlation_id()  # v4.2: the request's ID when not given
        record = AuditRecord(
            event_id=event_id or "AE-" + secrets.token_hex(8), occurred_at=occurred_at or _iso(now_utc()),
            owner_id=owner_id, category=category, event_type=event_type[:80], decision=str(decision)[:40],
            status=str(status)[:40], subject=_clean(subject), correlation_id=(correlation_id or None) and correlation_id[:64],
            details=clean, durable=self.durable,
        )
        try:
            self.store.append(record)
            return record
        except StorageError:
            with self._lock:
                self.failures += 1
            logger.warning("Durable audit write failed; kept in memory", extra={"outcome": "audit_fallback"})
            record = record.model_copy(update={"durable": False})
            self.fallback.append(record)
            return record

    def page(self, owner_id: UUID, *, limit: int, category: str | None = None) -> ActivityPage:
        events: tuple[AuditRecord, ...] = ()
        note = ("PostgreSQL (durable)" if self.durable else "In-memory only; cleared on restart (persistence disabled).")
        try:
            events = self.store.recent(owner_id, limit=limit, category=category)
        except StorageError:
            note = "Durable audit storage is unavailable; showing in-memory records only."
        if self.failures or not self.durable:
            fallback = self.fallback.recent(owner_id, limit=limit, category=category) if self.durable else ()
            events = tuple(sorted({item.event_id: item for item in (*events, *fallback)}.values(),
                                  key=lambda item: item.occurred_at, reverse=True))[:limit]
        if self.failures:
            note += f" {self.failures} record(s) could not be stored durably and are kept in memory."
        return ActivityPage(durable_storage=self.durable, storage_note=note, events=events)


class ApprovalError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


class ApprovalRequired(Exception):
    """The action was not run: it waits for the owner to approve ``request`` (HTTP 202)."""

    def __init__(self, request: ApprovalRequest):
        super().__init__(request.approval_id)
        self.request = request


class ApprovalService:
    def __init__(self, store, audit: AuditService, *, ttl_seconds: int, max_pending: int = 100):
        self.store = store
        self.audit = audit
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_pending = max_pending

    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def request(self, owner_id: UUID, proposal: ApprovalProposal) -> ApprovalRequest | None:
        """Record a pending approval; returns None (and audits) if it cannot be stored."""
        created = now_utc()
        scope = scope_hash(proposal.category, proposal.action, proposal.scope_material)
        try:
            self.store.expire(owner_id, now=created)
            pending = self.store.list(owner_id, limit=self.max_pending + 1, state="pending")
            duplicate = next((item for item in pending if item.scope_sha256 == scope), None)
            if duplicate is not None:
                return duplicate  # the same exact request is already awaiting a decision
            if len(pending) >= self.max_pending:
                self.audit.record(owner_id=owner_id, category="approval", event_type="approval_not_recorded",
                                  decision="denied", status="limit", subject=proposal.action,
                                  correlation_id=proposal.correlation_id)
                return None
            request = ApprovalRequest(
                approval_id="AR-" + secrets.token_hex(8), owner_id=owner_id, category=proposal.category,
                action=proposal.action, target=_clean(proposal.target, 300), reason=_clean(proposal.reason, 500),
                risk=proposal.risk, expected_side_effects=_clean(proposal.expected_side_effects, 300),
                scope_sha256=scope, correlation_id=proposal.correlation_id, created_at=_iso(created),
                expires_at=_iso(created + self.ttl), state="pending", execution=execution_for(proposal.category),
                durable=self.durable,
            )
            self.store.create(request)
        except StorageError:
            self.audit.record(owner_id=owner_id, category="approval", event_type="approval_not_recorded",
                              decision="failed", status="storage_unavailable", subject=proposal.action,
                              correlation_id=proposal.correlation_id)
            return None
        self.audit.record(owner_id=owner_id, category="approval", event_type="approval_requested",
                          decision="approval_required", status="pending", subject=proposal.action,
                          correlation_id=proposal.correlation_id,
                          details={"approval_id": request.approval_id, "category": request.category, "risk": request.risk})
        return request

    def list(self, owner_id: UUID, *, limit: int, state: str | None = None) -> ApprovalPage:
        try:
            self.store.expire(owner_id, now=now_utc())
            items = self.store.list(owner_id, limit=limit, state=state)
        except StorageError as exc:
            raise ApprovalError(503, "Approval storage is unavailable.") from exc
        return ApprovalPage(durable_storage=self.durable, execution_note=EXECUTION_NOTE, approvals=items)

    def get(self, owner_id: UUID, approval_id: str) -> ApprovalRequest:
        try:
            self.store.expire(owner_id, now=now_utc())
            item = self.store.get(owner_id, approval_id)
        except StorageError as exc:
            raise ApprovalError(503, "Approval storage is unavailable.") from exc
        if item is None:
            raise ApprovalError(404, "The approval request was not found.")
        return item

    def decide(self, owner_id: UUID, approval_id: str, *, approve: bool, note: str | None) -> ApprovalRequest:
        current = self.get(owner_id, approval_id)  # 404 for unknown/foreign ids; expires stale requests
        state = "approved" if approve else "rejected"
        try:
            updated = self.store.decide(owner_id, approval_id, state=state,
                                        note=_clean(note, 300) if note else None, now=now_utc())
        except StorageError as exc:
            raise ApprovalError(503, "Approval storage is unavailable.") from exc
        if updated is None:
            self.audit.record(owner_id=owner_id, category="approval", event_type="approval_decision_refused",
                              decision="denied", status=current.state, subject=current.action,
                              correlation_id=current.correlation_id, details={"approval_id": approval_id})
            raise ApprovalError(409, f"The approval request is {self.get(owner_id, approval_id).state}; it can no longer be decided.")
        self.audit.record(owner_id=owner_id, category="approval", event_type=f"approval_{state}",
                          decision=state, status=state, subject=updated.action, correlation_id=updated.correlation_id,
                          details={"approval_id": approval_id, "executed": False})
        return updated

    # ------------------------------------------------------------ v4.3 enforcement

    def consume(self, owner_id: UUID, approval_id: str, proposal: ApprovalProposal) -> ApprovalRequest:
        """Mark an approved request for exactly this action and scope as used, or raise ApprovalError."""
        current = self.get(owner_id, approval_id)  # 404 for unknown/foreign ids; expires stale requests
        now = now_utc()
        scope = scope_hash(proposal.category, proposal.action, proposal.scope_material)
        if (current.category, current.action, current.scope_sha256) != (proposal.category, proposal.action, scope):
            problem = ("The approval was granted for a different action or target, or the target has changed since "
                       "it was requested. Nothing was executed.")
        elif current.state == "pending":
            problem = "The approval request is still waiting for your decision. Nothing was executed."
        elif current.state != "approved":
            problem = f"The approval request is {current.state}. Nothing was executed."
        elif current.consumed_at is not None:
            problem = "The approval has already been used; each approval runs its action once. Nothing was executed."
        elif current.decided_at is None or datetime.fromisoformat(current.decided_at) + self.ttl <= now:
            problem = "The approval window has ended; request a new approval. Nothing was executed."
        else:
            problem = None
        updated = None
        if problem is None:
            try:
                updated = self.store.consume(owner_id, approval_id, category=proposal.category, action=proposal.action,
                                             scope_sha256=scope, now=now, decided_after=now - self.ttl)
            except StorageError as exc:
                raise ApprovalError(503, "Approval storage is unavailable. Nothing was executed.") from exc
            if updated is None:  # lost a race with another use of the same approval
                problem = "The approval could not be used; it was used or changed concurrently. Nothing was executed."
        if updated is None:
            self.audit.record(owner_id=owner_id, category="approval", event_type="approval_use_refused",
                              decision="denied", status=current.state, subject=proposal.action,
                              details={"approval_id": approval_id, "category": proposal.category})
            raise ApprovalError(409, problem)
        self.audit.record(owner_id=owner_id, category="approval", event_type="approval_used", decision="allowed",
                          status="consumed", subject=proposal.action,
                          details={"approval_id": approval_id, "category": proposal.category})
        return updated

    def record_outcome(self, owner_id: UUID, approval: ApprovalRequest, *, succeeded: bool,
                       error_type: str | None = None) -> None:
        details = {"approval_id": approval.approval_id, "executed": True}
        if error_type:
            details["error_type"] = error_type
        self.audit.record(owner_id=owner_id, category="approval",
                          event_type="approved_action_succeeded" if succeeded else "approved_action_failed",
                          decision="allowed", status="succeeded" if succeeded else "failed", subject=approval.action,
                          details=details)


def run_approved(approvals: "ApprovalService | None", owner_id: UUID, proposal: ApprovalProposal,
                 approval_id: str | None, action: Callable[[], T],
                 succeeded: Callable[[T], bool] | None = None) -> T:
    """Run ``action`` only under an approved, unused request for exactly this proposal (v4.3).

    ``succeeded`` decides the audited outcome for actions that report failure in their result instead of raising.
    """
    if approvals is None:
        raise ApprovalError(503, "The approval gateway is unavailable, so nothing was executed.")
    if approval_id is None:
        request = approvals.request(owner_id, proposal)
        if request is None:
            raise ApprovalError(503, "The approval request could not be recorded (storage unavailable or too many "
                                     "pending requests), so nothing was executed.")
        raise ApprovalRequired(request)
    approval = approvals.consume(owner_id, approval_id, proposal)
    try:
        result = action()
    except Exception as exc:
        approvals.record_outcome(owner_id, approval, succeeded=False, error_type=type(exc).__name__)
        raise
    approvals.record_outcome(owner_id, approval, succeeded=succeeded(result) if succeeded else True)
    return result
