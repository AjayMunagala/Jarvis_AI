"""Read-only tool runtime: registry binding, enforcement, bounded execution, audit (v2.7).

Every call passes these checks in order, in trusted code, before a handler
runs; the first failing check decides the result:

1. runtime enabled (``JARVIS_TOOLS_ENABLED``, default false)  -> unavailable
2. tool registered in ``tools.json`` **and** bound to an allowlisted handler -> denied
3. risk is LOW and the tool is read-only, else not executed     -> approval_required
4. caller holds ``tools.invoke`` and every capability the tool needs -> denied
5. arguments are within the byte budget and match the input schema -> invalid_input
6. the tool's dependency is configured (persistence, browser, ...) -> unavailable

Then the handler runs on a bounded worker pool with a per-tool timeout. Its
output must validate against the declared output model and fit the output
byte budget; otherwise the call fails. Every call, including refusals,
produces a metadata-only audit event (no argument values, no output).
"""

import hashlib
import json
import logging
import secrets
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID

from pydantic import ValidationError

from jarvis.browser.url_policy import BlockedURL
from jarvis.orchestration.registry import ToolRegistry
from jarvis.repo.secret_redactor import redact_secrets
from jarvis.security.policy import Capability, CapabilityPolicy
from jarvis.tools.calculator import CalculatorError
from jarvis.tools.handlers import HANDLERS, ToolContext, ToolHandler
from jarvis.tools.models import (
    AuditPage, CapabilitiesResponse, CapabilityDescriptor, RiskClass, ToolAuditEvent, ToolCall, ToolError,
    ToolResult, ToolStatus,
)

logger = logging.getLogger(__name__)
MAX_AUDIT_EVENTS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(arguments: dict) -> str:
    try:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        canonical = "<unserializable>"
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditLog:
    """Bounded in-memory audit ring. Non-durable: cleared on restart (v2.9 adds PostgreSQL)."""

    def __init__(self, capacity: int = MAX_AUDIT_EVENTS):
        self._events: deque[ToolAuditEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, event: ToolAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int) -> tuple[ToolAuditEvent, ...]:
        with self._lock:
            return tuple(list(self._events)[-limit:][::-1])


class ToolRuntime:
    def __init__(self, settings, registry: ToolRegistry, policy: CapabilityPolicy, *,
                 handlers: dict[str, ToolHandler] | None = None, audit: AuditLog | None = None):
        self.settings = settings
        self.registry = registry
        self.policy = policy
        self.handlers = HANDLERS if handlers is None else handlers
        self.audit = audit or AuditLog()
        self._executor = ThreadPoolExecutor(max_workers=settings.tools_max_concurrent, thread_name_prefix="jarvis-tool")
        self._slots = threading.BoundedSemaphore(settings.tools_max_concurrent)

    # ----------------------------------------------------------- discovery
    def _handler(self, tool_id: str) -> ToolHandler | None:
        definition = self.registry.get(tool_id)
        handler = self.handlers.get(tool_id)
        if definition is None or handler is None or definition.version != handler.version:
            return None
        return handler

    def _limits(self, handler: ToolHandler) -> tuple[float, int]:
        timeout = min(handler.timeout_seconds, self.settings.tools_max_timeout_seconds)
        return timeout, self.settings.tools_max_output_bytes

    def describe(self, owner_id: UUID | None, context: ToolContext | None,
                 *, policy: CapabilityPolicy | None = None) -> CapabilitiesResponse:
        policy = policy or self.policy
        enabled = bool(self.settings.tools_enabled)
        items = []
        for definition in self.registry.config.tools:
            handler = self.handlers.get(definition.id)
            if handler is None or definition.version != handler.version:
                available, reason = False, "No allowlisted handler is bound to this registry entry."
            elif context is None:
                available, reason = False, "Authentication is required to check availability."
            else:
                try:
                    available, reason = handler.available(context)
                except Exception:
                    available, reason = False, "Availability could not be determined."
            permitted = handler is not None and owner_id is not None and all(
                self._allows(policy, owner_id, capability) for capability in (Capability.TOOLS_INVOKE, *handler.required)
            )
            timeout, max_bytes = self._limits(handler) if handler else (1.0, self.settings.tools_max_output_bytes)
            items.append(CapabilityDescriptor(
                tool_id=definition.id, version=definition.version,
                title=handler.title if handler else definition.id, description=definition.description,
                risk=RiskClass(definition.risk), read_only=definition.read_only,
                required_permissions=tuple(str(item) for item in ((Capability.TOOLS_INVOKE, *handler.required) if handler else ())),
                input_schema=definition.input_schema, output_schema=definition.output_schema,
                timeout_seconds=timeout, max_output_bytes=max_bytes,
                available=available, availability_reason=reason,
                invocable=(invocable := enabled and available and permitted and handler is not None
                           and definition.risk == "low" and definition.read_only),
                autonomous=bool(invocable and getattr(self.settings, "chat_tools_enabled", False)),
            ))
        return CapabilitiesResponse(
            tools_enabled=enabled, capabilities=tuple(items),
            note=("Read-only tools only. "
                  + ("Chat may propose calls to invocable tools; trusted code validates each one. "
                     if getattr(self.settings, "chat_tools_enabled", False) else "Chat does not call tools. ")
                  + ("" if enabled else "Tool execution is disabled (JARVIS_TOOLS_ENABLED=false).")).strip(),
        )

    @staticmethod
    def _allows(policy, owner_id: UUID, capability: Capability) -> bool:
        if not isinstance(policy, CapabilityPolicy):
            return False
        try:
            return policy.allows(owner_id, capability) is True
        except Exception:
            return False  # policy errors are never permission

    # ------------------------------------------------------------- invocation
    def invoke(self, owner_id: UUID, tool_id: str, arguments: dict, context: ToolContext,
               *, correlation_id: str | None = None, policy: CapabilityPolicy | None = None) -> ToolResult:
        policy = policy or self.policy
        started_at, started = _now(), perf_counter()
        call = ToolCall(
            call_id="TC-" + secrets.token_hex(8), tool_id=tool_id[:80], arguments={},
            correlation_id=(correlation_id or secrets.token_hex(8))[:64],
            deadline_seconds=self.settings.tools_max_timeout_seconds,
        )
        handler = self._handler(tool_id)

        def finish(status, decision, *, output=None, error=None, evidence=(), retryable=False,
                   output_bytes=0, untrusted=False) -> ToolResult:
            event = ToolAuditEvent(
                event_id="AE-" + secrets.token_hex(8), timestamp=_now(), call_id=call.call_id,
                correlation_id=call.correlation_id, tool_id=call.tool_id, actor=str(owner_id), decision=decision,
                status=status, error_code=error.code if error else None,
                argument_keys=tuple(sorted(str(key)[:60] for key in list(arguments)[:20])) if isinstance(arguments, dict) else (),
                argument_sha256=_digest(arguments if isinstance(arguments, dict) else {}),
                duration_ms=round((perf_counter() - started) * 1000, 3), evidence_count=len(evidence),
            )
            self.audit.append(event)
            logger.info("Tool call recorded", extra={
                "outcome": status.value, "error_code": f"{call.tool_id}:{decision}", "duration_ms": event.duration_ms,
            })
            return ToolResult(
                call_id=call.call_id, tool_id=call.tool_id, tool_version=handler.version if handler else None,
                status=status, output=output, error=error, evidence=tuple(evidence)[:50], started_at=started_at,
                completed_at=_now(), duration_ms=event.duration_ms, retryable=retryable, output_bytes=output_bytes,
                audit_event_id=event.event_id, untrusted_content=untrusted,
            )

        if not self.settings.tools_enabled:
            return finish(ToolStatus.UNAVAILABLE, "disabled",
                          error=ToolError(code="tools_disabled", message="Tool execution is disabled (JARVIS_TOOLS_ENABLED=false)."))
        if handler is None:
            return finish(ToolStatus.DENIED, "denied",
                          error=ToolError(code="unknown_tool", message="The tool is not registered."))
        definition = self.registry.get(tool_id)
        if definition.risk != "low" or not definition.read_only or handler.risk != RiskClass.LOW or not handler.read_only:
            return finish(ToolStatus.APPROVAL_REQUIRED, "approval_required",
                          error=ToolError(code="approval_required",
                                          message="Only low-risk read-only tools can run; this tool was not executed."))
        for capability in (Capability.TOOLS_INVOKE, *handler.required):
            if not self._allows(policy, owner_id, capability):
                return finish(ToolStatus.DENIED, "denied",
                              error=ToolError(code="permission_denied", message=f"Missing permission: {capability.value}."))
        if not isinstance(arguments, dict):
            return finish(ToolStatus.INVALID_INPUT, "invalid_input",
                          error=ToolError(code="invalid_arguments", message="Arguments must be a JSON object."))
        if len(json.dumps(arguments, default=str).encode()) > self.settings.tools_max_argument_bytes:
            return finish(ToolStatus.INVALID_INPUT, "invalid_input",
                          error=ToolError(code="arguments_too_large", message="Arguments exceed the configured size limit."))
        try:
            args = handler.args_model.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted({".".join(str(part) for part in item["loc"]) or "arguments" for item in exc.errors()})[:5]
            return finish(ToolStatus.INVALID_INPUT, "invalid_input",
                          error=ToolError(code="schema_violation", message="Invalid arguments: " + ", ".join(fields)))
        try:
            available, reason = handler.available(context)
        except Exception:
            available, reason = False, "Availability could not be determined."
        if not available:
            return finish(ToolStatus.UNAVAILABLE, "unavailable",
                          error=ToolError(code="dependency_unavailable", message=reason[:500] or "The tool is unavailable."))

        timeout, max_bytes = self._limits(handler)
        if not self._slots.acquire(timeout=1.0):
            return finish(ToolStatus.UNAVAILABLE, "allowed", retryable=True,
                          error=ToolError(code="runtime_busy", message="Too many tool calls are running; try again."))
        try:
            future = self._executor.submit(handler.run, context, args)
        except RuntimeError:
            self._slots.release()
            return finish(ToolStatus.UNAVAILABLE, "allowed",
                          error=ToolError(code="runtime_closed", message="The tool runtime is shutting down."))
        # The slot is released only when the worker really finishes, so a timed-out
        # call still counts against the concurrency limit until it stops.
        future.add_done_callback(lambda _: self._slots.release())
        try:
            result = future.result(timeout=timeout)
        except FutureTimeout:
            return finish(ToolStatus.TIMED_OUT, "allowed", retryable=True,
                          error=ToolError(code="timeout", message=f"The tool did not finish within {timeout:g} seconds."))
        except CalculatorError as exc:
            return finish(ToolStatus.INVALID_INPUT, "invalid_input",
                          error=ToolError(code="invalid_expression", message=str(exc)[:500]))
        except BlockedURL as exc:
            return finish(ToolStatus.DENIED, "denied", error=ToolError(code="destination_blocked", message=exc.public_message))
        except Exception as exc:  # sanitized: never expose exception text
            code = getattr(exc, "code", None)
            message = getattr(exc, "public_message", None)
            return finish(ToolStatus.FAILED, "allowed", retryable=False,
                          error=ToolError(code=str(code)[:60] if isinstance(code, str) else "tool_failed",
                                          message=str(message)[:500] if isinstance(message, str) else "The tool failed."))

        try:
            output = handler.output_model.model_validate(result.output.model_dump()).model_dump(mode="json")
        except (ValidationError, AttributeError):
            return finish(ToolStatus.FAILED, "allowed",
                          error=ToolError(code="invalid_output", message="The tool returned output that failed validation."))
        encoded = json.dumps(output, ensure_ascii=False)
        redacted, redactions = redact_secrets(encoded)
        if redactions:
            output = json.loads(redacted)
            encoded = redacted
        size = len(encoded.encode())
        if size > max_bytes:
            return finish(ToolStatus.FAILED, "allowed", output_bytes=size,
                          error=ToolError(code="output_too_large", message="The tool output exceeded the configured size limit."))
        return finish(ToolStatus.SUCCEEDED, "allowed", output=output, evidence=result.evidence,
                      output_bytes=size, untrusted=result.untrusted_content)

    def audit_page(self, limit: int) -> AuditPage:
        return AuditPage(retention=f"In-memory only, last {MAX_AUDIT_EVENTS} events; cleared on restart.",
                         events=self.audit.recent(limit))

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
