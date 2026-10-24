"""Integration enrollments and device actions (v4.8).

- Creating an enrollment records it as ``pending``; it can do nothing yet.
- Activating it needs an approved request (v4.3 gateway, category
  ``integration``), bound to the enrollment, adapter and secret reference.
- Revoking is always allowed, immediate and final; a revoked enrollment can
  never run an action again.
- A read-only action runs directly on an active enrollment. An action that
  changes state needs its own approved request, bound to the enrollment, the
  action and its exact arguments.
- The secret reference is resolved only when an action runs; its value is
  never stored, returned, logged or audited.
"""

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from jarvis.audit.models import ApprovalProposal
from jarvis.audit.service import run_approved
from jarvis.audit.stores import StorageError
from jarvis.integrations.adapters import AdapterError, AdapterSpec, default_adapters, resolve_secret
from jarvis.integrations.models import (ActionRequest, ActionResult, AdapterDescriptor, Enrollment,
                                        EnrollmentCreateRequest, EnrollmentPage, EnrollmentView)

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationService:
    def __init__(self, store, audit_service: Any = None, adapters: dict[str, AdapterSpec] | None = None):
        self.store = store
        self.audit_service = audit_service
        self.adapters = adapters if adapters is not None else default_adapters()
        self.approval_service: Any = None

    # ------------------------------------------------------------ helpers

    def _audit(self, owner_id: UUID, event_type: str, enrollment: Enrollment, details: dict[str, Any] | None = None,
               *, decision: str = "allowed", status: str = "succeeded") -> None:
        if self.audit_service is None:
            return
        try:  # metadata only: never arguments' secret values, never the secret
            self.audit_service.record(owner_id=owner_id, category="integration", event_type=event_type,
                                      decision=decision, status=status,
                                      subject=f"integration:{enrollment.enrollment_id}",
                                      details={"adapter": enrollment.adapter_id, **(details or {})})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Integration audit could not be recorded: %s", type(exc).__name__)

    def _storage(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except StorageError as exc:
            raise IntegrationError(503, "Integration storage is unavailable.") from exc

    def _adapter(self, adapter_id: str) -> AdapterSpec:
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise IntegrationError(422, f"Unknown adapter '{adapter_id}'.")
        return adapter

    def _get(self, owner_id: UUID, enrollment_id: str) -> Enrollment:
        enrollment = self._storage(self.store.get, owner_id, enrollment_id)
        if enrollment is None:
            raise IntegrationError(404, "The enrollment was not found.")
        return enrollment

    def _view(self, enrollment: Enrollment) -> EnrollmentView:
        if enrollment.secret_ref is None:
            secret_status = "not_required"
        else:
            secret_status = "present" if resolve_secret(enrollment.secret_ref) is not None else "missing"
        return EnrollmentView(enrollment=enrollment, secret_status=secret_status)

    # ------------------------------------------------------------ read

    def list_adapters(self) -> tuple[AdapterDescriptor, ...]:
        return tuple(adapter.descriptor for adapter in self.adapters.values())

    def list_enrollments(self, owner_id: UUID, *, limit: int = 50) -> EnrollmentPage:
        items = self._storage(self.store.list, owner_id, limit=limit)
        return EnrollmentPage(durable_storage=bool(getattr(self.store, "durable", False)),
                              enrollments=tuple(self._view(item) for item in items))

    def get_enrollment(self, owner_id: UUID, enrollment_id: str) -> EnrollmentView:
        return self._view(self._get(owner_id, enrollment_id))

    # ------------------------------------------------------------ lifecycle

    def create_enrollment(self, owner_id: UUID, req: EnrollmentCreateRequest) -> EnrollmentView:
        adapter = self._adapter(req.adapter_id)
        if adapter.descriptor.requires_secret and req.secret_ref is None:
            raise IntegrationError(422, "This adapter needs a secret reference (env:JARVIS_INTEGRATION_...).")
        enrollment = Enrollment(
            enrollment_id="EN-" + secrets.token_hex(8), owner_id=owner_id, adapter_id=req.adapter_id,
            display_name=req.display_name, secret_ref=req.secret_ref, state="pending",
            created_at=_now().isoformat(timespec="milliseconds"), durable=bool(getattr(self.store, "durable", False)))
        self._storage(self.store.create, enrollment)
        self._audit(owner_id, "integration.enroll", enrollment, {"state": "pending"})
        return self._view(enrollment)

    def activate(self, owner_id: UUID, enrollment_id: str, approval_id: str | None = None) -> EnrollmentView:
        enrollment = self._get(owner_id, enrollment_id)
        if enrollment.state != "pending":
            raise IntegrationError(409, f"The enrollment is {enrollment.state}; only a pending enrollment can be activated.")
        if enrollment.secret_ref is not None and resolve_secret(enrollment.secret_ref) is None:
            raise IntegrationError(422, "The secret reference does not resolve; set the environment variable first.")
        proposal = ApprovalProposal(
            category="integration", action="integration.activate",
            target=f"{enrollment.adapter_id}: {enrollment.display_name}"[:300],
            reason=f"Connect the integration '{enrollment.display_name}'"[:500], risk="high",
            expected_side_effects="Lets JARVIS run this adapter's actions; state-changing actions still need approval.",
            scope_material=f"{enrollment.enrollment_id}|{enrollment.adapter_id}|{enrollment.secret_ref or ''}",
        )

        def activate_now() -> Enrollment:
            updated = self._storage(self.store.transition, owner_id, enrollment_id, from_states=("pending",),
                                    to_state="active", now=_now())
            if updated is None:
                raise IntegrationError(409, "The enrollment changed before it could be activated.")
            return updated

        updated = run_approved(self.approval_service, owner_id, proposal, approval_id, activate_now)
        self._audit(owner_id, "integration.activate", updated, {"state": "active"})
        return self._view(updated)

    def revoke(self, owner_id: UUID, enrollment_id: str) -> EnrollmentView:
        enrollment = self._get(owner_id, enrollment_id)
        if enrollment.state == "revoked":
            return self._view(enrollment)
        updated = self._storage(self.store.transition, owner_id, enrollment_id, from_states=("pending", "active"),
                                to_state="revoked", now=_now())
        if updated is None:  # revoked concurrently
            return self._view(self._get(owner_id, enrollment_id))
        self._adapter(updated.adapter_id).forget(enrollment_id)
        self._audit(owner_id, "integration.revoke", updated, {"state": "revoked"})
        return self._view(updated)

    # ------------------------------------------------------------ actions

    def act(self, owner_id: UUID, enrollment_id: str, req: ActionRequest) -> ActionResult:
        enrollment = self._get(owner_id, enrollment_id)
        if enrollment.state != "active":
            raise IntegrationError(409, f"The enrollment is {enrollment.state}; actions need an active enrollment.")
        adapter = self._adapter(enrollment.adapter_id)
        spec = adapter.action(req.action)
        if spec is None:
            raise IntegrationError(422, f"The adapter has no action '{req.action}'.")
        try:
            arguments = adapter.validate(req.action, req.arguments)
        except AdapterError as exc:
            raise IntegrationError(422, str(exc)) from exc

        def run_now() -> ActionResult:
            current = self._get(owner_id, enrollment_id)  # re-checked right before running: revoke wins
            if current.state != "active":
                raise IntegrationError(409, f"The enrollment is {current.state}; nothing was executed.")
            secret = resolve_secret(current.secret_ref)
            if current.secret_ref is not None and secret is None:
                raise IntegrationError(503, "The secret reference does not resolve; nothing was executed.")
            output = adapter.execute(enrollment_id, req.action, arguments, secret)
            self._storage(self.store.touch, owner_id, enrollment_id, _now())
            return ActionResult(enrollment_id=enrollment_id, action=req.action, read_only=spec.read_only, mode="mock",
                                output=output, executed_at=_now().isoformat(timespec="milliseconds"),
                                approval_id=None if spec.read_only else req.approval_id)

        if spec.read_only:
            result = run_now()
        else:
            material = json.dumps({"enrollment": enrollment_id, "action": req.action, "arguments": arguments},
                                  sort_keys=True)
            proposal = ApprovalProposal(
                category="integration", action="integration.action",
                target=f"{enrollment.display_name}: {req.action}"[:300],
                reason=f"Run '{req.action}' on '{enrollment.display_name}'"[:500], risk="medium",
                expected_side_effects=spec.description[:300],
                scope_material=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            )
            result = run_approved(self.approval_service, owner_id, proposal, req.approval_id, run_now)
        self._audit(owner_id, "integration.action", enrollment, {"action": req.action, "read_only": spec.read_only})
        return result
