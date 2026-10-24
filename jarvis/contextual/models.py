"""Contracts for categorized memory candidates, context packages and updates."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

# conversation / long_term / retrieved_knowledge map onto existing JARVIS data
# (conversation history, explicit memories, ingested documents). task, project
# and procedural are supplied by callers; this module stores none of them.
MemoryCategory = Literal["conversation", "task", "long_term", "project", "procedural", "retrieved_knowledge"]
Reliability = Literal["verified", "user_stated", "document", "inferred"]
CandidateStatus = Literal["active", "outdated"]

CATEGORY_PREFIX = {"conversation": "H", "task": "T", "long_term": "M", "project": "P", "procedural": "R",
                   "retrieved_knowledge": "D"}


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Fact(Frozen):
    """Optional structured claim used for contradiction detection."""

    subject: StrictStr = Field(min_length=1, max_length=120)
    attribute: StrictStr = Field(min_length=1, max_length=80)
    value: StrictStr = Field(min_length=1, max_length=300)

    @property
    def key(self) -> tuple[str, str]:
        return (" ".join(self.subject.lower().split()), " ".join(self.attribute.lower().split()))

    @property
    def normalized_value(self) -> str:
        return " ".join(self.value.lower().split())


class MemoryCandidate(Frozen):
    candidate_id: StrictStr = Field(min_length=1, max_length=80)
    owner_id: UUID
    category: MemoryCategory
    content: StrictStr = Field(min_length=1, max_length=4000)
    source_ref: StrictStr = Field(min_length=1, max_length=300)
    project_id: StrictStr | None = Field(default=None, max_length=80)
    task_id: StrictStr | None = Field(default=None, max_length=80)
    updated_at: datetime | None = None
    reliability: Reliability = "user_stated"
    status: CandidateStatus = "active"
    pinned: StrictBool = False  # the owner explicitly marked this as always relevant
    fact: Fact | None = None

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class RelevanceWeights(Frozen):
    similarity: float = Field(default=1.0, ge=0, le=10)
    recency: float = Field(default=0.3, ge=0, le=10)
    reliability: float = Field(default=0.4, ge=0, le=10)
    scope: float = Field(default=0.3, ge=0, le=10)
    preference: float = Field(default=0.5, ge=0, le=10)
    task: float = Field(default=0.4, ge=0, le=10)


class ContextRequest(Frozen):
    owner_id: UUID
    query: StrictStr = Field(min_length=1, max_length=20000)
    project_id: StrictStr | None = None
    task_id: StrictStr | None = None
    categories: tuple[MemoryCategory, ...] = ("conversation", "task", "long_term", "project", "procedural",
                                              "retrieved_knowledge")
    max_items: int = Field(default=12, ge=1, le=30)
    min_score: float = Field(default=0.15, ge=0, le=1)
    include_outdated: StrictBool = False
    reserve_output_tokens: int = Field(default=1024, ge=0, le=200_000)


class PackagedItem(Frozen):
    ref: StrictStr = Field(pattern=r"^[HTMPRD][0-9]{1,2}$")
    candidate_id: StrictStr
    category: MemoryCategory
    source_ref: StrictStr
    reliability: Reliability
    excerpt: StrictStr
    truncated: StrictBool
    score: float
    components: dict[str, float]
    reasons: tuple[StrictStr, ...] = ()


class Omission(Frozen):
    candidate_id: StrictStr
    reason: Literal["other_owner", "other_project", "category_not_requested", "outdated", "below_threshold",
                    "contradicted", "duplicate", "budget", "item_limit"]


class Contradiction(Frozen):
    subject: StrictStr
    attribute: StrictStr
    candidate_ids: tuple[StrictStr, ...] = Field(min_length=2)
    values: tuple[StrictStr, ...]
    resolution: Literal["more_reliable_kept", "unresolved"]
    kept: tuple[StrictStr, ...]


class ContextPackage(Frozen):
    items: tuple[PackagedItem, ...]
    omitted: tuple[Omission, ...]
    contradictions: tuple[Contradiction, ...]
    budget_tokens: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    budget_source: Literal["model_context", "conservative_default", "request"]
    model_key: StrictStr | None = None
    similarity: StrictStr
    note: StrictStr


class MemoryUpdateProposal(Frozen):
    proposal_id: StrictStr = Field(pattern=r"^MP-[a-f0-9]{16}$")
    owner_id: UUID
    kind: Literal["add", "mark_outdated"]
    category: MemoryCategory
    content: StrictStr | None = Field(default=None, max_length=4000)
    target_ref: StrictStr | None = Field(default=None, max_length=300)
    fact: Fact | None = None
    source_ref: StrictStr = Field(min_length=1, max_length=300)
    reliability: Reliability
    # "applying" = the owner accepted an ``add`` and the memory write is in
    # progress (or was interrupted); it is never reported as accepted until the
    # write is confirmed.
    status: Literal["proposed", "applying", "accepted", "rejected"] = "proposed"
    reason: StrictStr = Field(max_length=500)
    conflicts_with: tuple[StrictStr, ...] = ()
    created_at: StrictStr
    decided_at: StrictStr | None = None
    applied_ref: StrictStr | None = Field(default=None, max_length=300)
    last_error: StrictStr | None = Field(default=None, max_length=200)
