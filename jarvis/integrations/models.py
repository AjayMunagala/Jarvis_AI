"""Contracts for integration and device readiness (v4.8).

An *adapter* describes one kind of external integration and its actions. An
*enrollment* is the owner's connection to one adapter instance. It moves
``pending`` -> ``active`` (only with an approved request) -> ``revoked``
(always allowed, immediately, and final). Credentials are never stored: an
enrollment holds a *secret reference* such as ``env:JARVIS_INTEGRATION_LAMP``,
which is resolved only when an action runs, and its value is never returned,
logged or audited. Only a mock adapter exists; there is no real device
control.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

EnrollmentState = Literal["pending", "active", "revoked"]
SECRET_REF_PATTERN = r"^env:JARVIS_INTEGRATION_[A-Z0-9_]{1,64}$"
ENROLLMENT_ID_PATTERN = r"^EN-[a-f0-9]{16}$"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdapterAction(Contract):
    name: StrictStr
    read_only: StrictBool
    description: StrictStr


class AdapterDescriptor(Contract):
    adapter_id: StrictStr
    name: StrictStr
    mode: Literal["mock"]
    requires_secret: StrictBool
    actions: tuple[AdapterAction, ...]
    limitations: tuple[StrictStr, ...] = ()


class Enrollment(Contract):
    enrollment_id: StrictStr = Field(pattern=ENROLLMENT_ID_PATTERN)
    owner_id: UUID
    adapter_id: StrictStr = Field(min_length=1, max_length=80)
    display_name: StrictStr = Field(min_length=1, max_length=100)
    secret_ref: StrictStr | None = Field(default=None, pattern=SECRET_REF_PATTERN)
    state: EnrollmentState
    created_at: StrictStr
    activated_at: StrictStr | None = None
    revoked_at: StrictStr | None = None
    last_action_at: StrictStr | None = None
    durable: StrictBool


class EnrollmentView(Contract):
    """What the API returns: the enrollment plus whether its secret reference currently resolves (never its value)."""

    enrollment: Enrollment
    secret_status: Literal["not_required", "present", "missing"]


class EnrollmentPage(Contract):
    durable_storage: StrictBool
    enrollments: tuple[EnrollmentView, ...]


class EnrollmentCreateRequest(Contract):
    adapter_id: StrictStr = Field(min_length=1, max_length=80)
    display_name: StrictStr = Field(min_length=1, max_length=100)
    secret_ref: StrictStr | None = Field(default=None, pattern=SECRET_REF_PATTERN)

    @field_validator("display_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return value.strip()


class ActionRequest(Contract):
    action: StrictStr = Field(min_length=1, max_length=60)
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=10)
    approval_id: StrictStr | None = Field(default=None, pattern=r"^AR-[a-f0-9]{16}$")


class ActionResult(Contract):
    enrollment_id: StrictStr
    action: StrictStr
    read_only: StrictBool
    mode: Literal["mock"]
    output: dict[str, Any]
    executed_at: StrictStr
    approval_id: StrictStr | None = None
