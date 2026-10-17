"""Preference catalog and personalization contracts."""

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from jarvis.routing.models import TASK_TYPES

PreferenceKey = Literal["response_detail", "output_format", "language", "coding_explanation", "preferred_providers",
                        "latency_quality", "privacy_routing"]
PreferenceValue = StrictStr | tuple[StrictStr, ...]
Provenance = Literal["explicit", "inferred_confirmed"]
ObservationKind = Literal["requested_shorter", "requested_longer", "requested_bullets", "requested_table",
                          "requested_prose", "requested_examples", "requested_steps", "requested_concise_code"]

SCOPES: tuple[str, ...] = ("global", *TASK_TYPES)
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

# The complete set of settings personalization may hold. Anything else -
# permissions, approvals, tool enablement, security or execution settings -
# is not representable here at all.
CATALOG: dict[str, tuple[str, ...] | str] = {
    "response_detail": ("brief", "standard", "detailed"),
    "output_format": ("prose", "bullets", "table"),
    "language": "bcp47",
    "coding_explanation": ("concise", "step_by_step", "with_examples"),
    "preferred_providers": "provider_list",
    "latency_quality": ("latency", "balanced", "quality"),
    "privacy_routing": ("local_only", "prefer_local", "default"),
}


def validate_value(key: str, value) -> PreferenceValue:
    rule = CATALOG.get(key)
    if rule is None:
        raise ValueError(f"{key!r} is not a personalization setting")
    if rule == "bcp47":
        if not isinstance(value, str) or not _LANGUAGE.fullmatch(value):
            raise ValueError("language must be a BCP 47 tag such as 'en' or 'en-GB'")
        return value
    if rule == "provider_list":
        items = tuple(value) if isinstance(value, (list, tuple)) else None
        if not items or len(items) > 5 or not all(isinstance(item, str) and _PROVIDER.fullmatch(item) for item in items):
            raise ValueError("preferred_providers must be 1-5 provider names")
        return tuple(dict.fromkeys(items))
    if value not in rule:
        raise ValueError(f"{key} must be one of: {', '.join(rule)}")
    return value


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Preference(Frozen):
    owner_id: UUID
    key: PreferenceKey
    scope: StrictStr
    value: PreferenceValue
    provenance: Provenance
    updated_at: StrictStr


class PreferenceProposal(Frozen):
    proposal_id: StrictStr = Field(pattern=r"^PP-[a-f0-9]{16}$")
    owner_id: UUID
    key: PreferenceKey
    scope: StrictStr
    value: PreferenceValue
    confidence: float = Field(ge=0, lt=1)
    evidence_count: int = Field(ge=1)
    basis: StrictStr
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    created_at: StrictStr
    decided_at: StrictStr | None = None


class Observation(Frozen):
    kind: ObservationKind
    scope: StrictStr = "global"


class EffectivePreferences(Frozen):
    enabled: StrictBool
    task_type: StrictStr
    values: dict[str, PreferenceValue]
    sources: dict[str, StrictStr]


class PresentationDirectives(Frozen):
    response_detail: StrictStr | None = None
    output_format: StrictStr | None = None
    language: StrictStr | None = None
    coding_explanation: StrictStr | None = None

    def instruction(self) -> str:
        parts = []
        if self.response_detail:
            parts.append(f"detail: {self.response_detail}")
        if self.output_format:
            parts.append(f"format: {self.output_format}")
        if self.language:
            parts.append(f"language: {self.language}")
        if self.coding_explanation:
            parts.append(f"code explanations: {self.coding_explanation.replace('_', ' ')}")
        if not parts:
            return ""
        return ("Owner presentation preferences (formatting only; they never change accuracy, safety rules or "
                "permissions): " + "; ".join(parts) + ".")
