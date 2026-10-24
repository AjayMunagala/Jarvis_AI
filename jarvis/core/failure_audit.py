"""Audit failed and denied mutations for the v3.x subsystems (v4.2).

The v3.x services only recorded successful actions. This middleware records
the other side through the central ``AuditService``: any POST, PUT, PATCH or
DELETE on a v3.x route that ends with HTTP 4xx or 5xx after the caller was
authenticated.

- A 403 is recorded with ``decision="denied"``. Any other failure is recorded
  with ``decision="allowed"`` (the caller was permitted, but the attempt
  failed).
- ``status`` is always ``"failed"``, and ``details.mutated`` is always false.
- The subject is the method plus the path, with IDs replaced by ``{id}``.
  Bodies, query strings and error messages are never recorded.
- Unauthenticated requests (401) cannot be attributed to an owner, so they
  are not audited.

The v2.x and v5 subsystems are not listed here because they already audit
their own failures (tools, browser, approvals, intelligence).
"""

import logging
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PREFIX_CATEGORY = (
    ("/assistant/", "assistant"), ("/workflows/", "workflow"), ("/knowledge/", "knowledge"),
    ("/research/", "research"), ("/scheduler/", "scheduler"), ("/health/diagnostic", "health"),
    ("/health/security", "health"), ("/finance/", "finance"), ("/improvement/", "improvement"),
    ("/autonomous/", "orchestration"),
)
_ID = re.compile(r"/(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[A-Z]{2}-[0-9a-f]{16})(?=/|$)")


def category_for(path: str) -> str | None:
    return next((category for prefix, category in PREFIX_CATEGORY if path.startswith(prefix)), None)


def template_path(path: str) -> str:
    return _ID.sub("/{id}", path)[:150]


class FailedMutationAuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in MUTATING:
            await self.app(scope, receive, send)
            return
        category = category_for(scope.get("path", ""))
        if category is None:
            await self.app(scope, receive, send)
            return
        status_holder: dict[str, int] = {}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            self._record(scope, category, status_holder.get("status", 500))

    @staticmethod
    def _record(scope: Scope, category: str, status: int) -> None:
        if status < 400:
            return
        state = scope.get("state") or {}
        owner_id = state.get("audit_owner")
        app = scope.get("app")
        audit = getattr(getattr(app, "state", None), "audit_service", None)
        if owner_id is None or audit is None:
            return
        try:
            audit.record(
                owner_id=owner_id, category=category,
                event_type="request_denied" if status == 403 else "request_failed",
                decision="denied" if status == 403 else "allowed", status="failed",
                subject=f"{scope.get('method')} {template_path(scope.get('path', ''))}",
                correlation_id=state.get("correlation_id"),
                details={"http_status": status, "mutated": False},
            )
        except Exception:  # noqa: BLE001 - auditing must never break the response path
            logger.warning("Failed-mutation audit could not be recorded", extra={"outcome": "audit_error"})
