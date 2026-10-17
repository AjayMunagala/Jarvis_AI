"""Typed, versioned contracts for capabilities, tool calls, results, and audit (v2.7).

These follow the master reference contracts (CapabilityDescriptor, ToolCall,
ToolResult, AuditEvent). ``ApprovalRequest`` is intentionally absent: approval
handling is a v2.9 concern; in v2.7 anything that is not a LOW-risk read-only
tool resolves to ``approval_required`` without executing.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

CONTRACT_VERSION = "1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskClass(StrEnum):
    """Master-reference risk policy. Only LOW is executable in v2.7."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"
    PARTIAL = "partial"
    APPROVAL_REQUIRED = "approval_required"


class CapabilityDescriptor(Contract):
    tool_id: StrictStr = Field(pattern=r"^[a-z0-9_.-]{1,80}$")
    version: StrictStr = Field(min_length=1, max_length=32)
    title: StrictStr = Field(min_length=1, max_length=120)
    description: StrictStr = Field(min_length=1, max_length=400)
    risk: RiskClass
    read_only: StrictBool
    side_effects: Literal["none"] = "none"
    required_permissions: tuple[StrictStr, ...] = Field(max_length=5)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout_seconds: float = Field(gt=0, le=60)
    max_output_bytes: int = Field(ge=1024, le=262_144)
    # Runtime state, computed per request; never trusted from configuration.
    available: StrictBool
    availability_reason: StrictStr = Field(default="", max_length=300)
    invocable: StrictBool
    # True only when JARVIS_CHAT_TOOLS_ENABLED lets the chat loop propose this tool (v2.8).
    autonomous: StrictBool = False


class CapabilitiesResponse(Contract):
    contract_version: StrictStr = CONTRACT_VERSION
    tools_enabled: StrictBool
    capabilities: tuple[CapabilityDescriptor, ...]
    note: StrictStr


class ToolCallRequest(Contract):
    """Client request body: the server assigns ids; clients cannot set permissions."""

    tool_id: StrictStr = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCall(Contract):
    call_id: StrictStr = Field(pattern=r"^TC-[a-f0-9]{16}$")
    tool_id: StrictStr = Field(min_length=1, max_length=80)
    tool_version: StrictStr | None = None
    arguments: dict[str, Any]
    correlation_id: StrictStr = Field(min_length=1, max_length=64)
    deadline_seconds: float = Field(gt=0, le=60)


class ToolError(Contract):
    code: StrictStr = Field(min_length=1, max_length=60)
    message: StrictStr = Field(min_length=1, max_length=500)


class ToolResult(Contract):
    contract_version: StrictStr = CONTRACT_VERSION
    call_id: StrictStr
    tool_id: StrictStr
    tool_version: StrictStr | None = None
    status: ToolStatus
    output: dict[str, Any] | None = None
    error: ToolError | None = None
    evidence: tuple[StrictStr, ...] = Field(default=(), max_length=50)
    started_at: StrictStr
    completed_at: StrictStr
    duration_ms: float = Field(ge=0)
    side_effects: Literal["none"] = "none"
    retryable: StrictBool = False
    output_bytes: int = Field(default=0, ge=0)
    audit_event_id: StrictStr
    untrusted_content: StrictBool = False


AuditDecision = Literal["allowed", "denied", "approval_required", "invalid_input", "unavailable", "disabled"]


class ToolAuditEvent(Contract):
    """Metadata-only audit record. Arguments are never stored, only their digest."""

    event_id: StrictStr = Field(pattern=r"^AE-[a-f0-9]{16}$")
    timestamp: StrictStr
    call_id: StrictStr
    correlation_id: StrictStr
    tool_id: StrictStr = Field(max_length=80)
    actor: StrictStr = Field(max_length=80)
    decision: AuditDecision
    status: ToolStatus
    error_code: StrictStr | None = None
    argument_keys: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    argument_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    duration_ms: float = Field(ge=0)
    evidence_count: int = Field(ge=0)
    durable: Literal[False] = False  # v2.7 audit is in-memory only (PostgreSQL storage is v2.9)


class AuditPage(Contract):
    durable: Literal[False] = False
    retention: StrictStr
    events: tuple[ToolAuditEvent, ...]
