"""Knowledge graph contracts and relationship schema."""

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

EntityType = Literal["person", "project", "document", "task", "software_component", "service", "concept", "event",
                     "organization"]
RelationType = Literal["belongs_to", "depends_on", "references", "created_by", "related_to", "describes", "supersedes"]
AssertionStatus = Literal["verified", "inferred", "disputed", "superseded", "retracted"]
SourceKind = Literal["memory", "document", "conversation", "repository", "user_statement", "system", "fixture"]
Extractor = Literal["user", "fixture", "rule", "model"]
Scalar = StrictStr | int | float | bool | None

ENTITY_TYPES: tuple[str, ...] = EntityType.__args__  # type: ignore[attr-defined]
ACTIVE_STATUSES = frozenset({"verified", "inferred", "disputed"})
MAX_PROVENANCE = 20

_WORK = {"project", "task", "document", "software_component", "service", "event"}
_ALL = set(ENTITY_TYPES)

# relation -> (allowed subject types, allowed object types, functional)
# A functional relation has at most one active object per subject; a second,
# different object is a conflict that must be resolved, never overwritten.
RELATION_SCHEMA: dict[str, tuple[frozenset[str], frozenset[str], bool]] = {
    "belongs_to": (frozenset(_WORK | {"person"}), frozenset({"project", "organization", "software_component", "service"}), True),
    "depends_on": (frozenset({"project", "task", "software_component", "service"}),
                   frozenset({"project", "task", "software_component", "service"}), False),
    "references": (frozenset(_ALL), frozenset({"document", "concept", "software_component", "service", "project", "task"}), False),
    "created_by": (frozenset({"document", "task", "project", "software_component", "event"}),
                   frozenset({"person", "organization"}), True),
    "related_to": (frozenset(_ALL), frozenset(_ALL), False),
    "describes": (frozenset({"document"}), frozenset(_ALL), False),
    "supersedes": (frozenset(_ALL), frozenset(_ALL), False),
}


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())[:200]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Provenance(Frozen):
    source_kind: SourceKind
    source_ref: StrictStr = Field(min_length=1, max_length=300)
    extractor: Extractor
    excerpt: StrictStr | None = Field(default=None, max_length=500)
    recorded_at: StrictStr | None = None


def _bounded_attributes(value: dict) -> dict:
    if len(value) > 20:
        raise ValueError("at most 20 attributes are allowed")
    for key, item in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
            raise ValueError(f"invalid attribute name {key!r}")
        if isinstance(item, str) and len(item) > 500:
            raise ValueError("attribute values are limited to 500 characters")
    return value


class EntityInput(Frozen):
    entity_type: EntityType
    name: StrictStr = Field(min_length=1, max_length=200)
    key: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    aliases: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    attributes: dict[str, Scalar] = Field(default_factory=dict)
    provenance: Provenance

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict) -> dict:
        return _bounded_attributes(value)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("entity names must not be blank")
        return value

    @property
    def normalized_key(self) -> str:
        return normalize_key(self.key or self.name)


class Entity(Frozen):
    entity_id: StrictStr = Field(pattern=r"^KE-[a-f0-9]{16}$")
    owner_id: UUID
    entity_type: EntityType
    key: StrictStr
    name: StrictStr
    aliases: tuple[StrictStr, ...] = ()
    attributes: dict[str, Scalar] = Field(default_factory=dict)
    provenance: tuple[Provenance, ...] = Field(default=(), max_length=MAX_PROVENANCE)
    created_at: StrictStr
    updated_at: StrictStr


class RelationshipInput(Frozen):
    subject_id: StrictStr = Field(pattern=r"^KE-[a-f0-9]{16}$")
    relation: RelationType
    object_id: StrictStr = Field(pattern=r"^KE-[a-f0-9]{16}$")
    status: Literal["verified", "inferred"]
    confidence: float = Field(ge=0, le=1)
    provenance: Provenance

    @model_validator(mode="after")
    def consistent(self) -> "RelationshipInput":
        if self.subject_id == self.object_id:
            raise ValueError("an entity cannot relate to itself")
        if self.status == "verified":
            if self.provenance.extractor == "model":
                raise ValueError("model-extracted relationships can only be inferred")
            if self.confidence != 1:
                raise ValueError("verified relationships must have confidence 1.0")
        elif self.confidence >= 1:
            raise ValueError("inferred relationships must have confidence below 1.0")
        return self


class Relationship(Frozen):
    relationship_id: StrictStr = Field(pattern=r"^KR-[a-f0-9]{16}$")
    owner_id: UUID
    subject_id: StrictStr
    relation: RelationType
    object_id: StrictStr
    status: AssertionStatus
    confidence: float = Field(ge=0, le=1)
    provenance: tuple[Provenance, ...] = Field(min_length=1, max_length=MAX_PROVENANCE)
    conflicts_with: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    superseded_by: StrictStr | None = None
    created_at: StrictStr
    updated_at: StrictStr


class Neighborhood(Frozen):
    root_id: StrictStr
    depth: int = Field(ge=0, le=3)
    entities: tuple[Entity, ...]
    relationships: tuple[Relationship, ...]
    truncated: bool
    note: StrictStr


# ------------------------------------------------------ normalized records

class RecordEntity(Frozen):
    ref: StrictStr = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    entity_type: EntityType
    name: StrictStr = Field(min_length=1, max_length=200)
    key: StrictStr | None = Field(default=None, max_length=200)
    aliases: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    attributes: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict) -> dict:
        return _bounded_attributes(value)


class RecordRelationship(Frozen):
    subject_ref: StrictStr
    relation: RelationType
    object_ref: StrictStr
    status: Literal["verified", "inferred"]
    confidence: float = Field(ge=0, le=1)
    excerpt: StrictStr | None = Field(default=None, max_length=500)


class KnowledgeRecord(Frozen):
    """Upstream contract: what a knowledge/document module hands the graph.

    The producing module owns ingestion and retrieval; the graph only stores
    the entities and relationships it is given, with this record's source as
    provenance for each.
    """

    source_kind: SourceKind
    source_ref: StrictStr = Field(min_length=1, max_length=300)
    extractor: Extractor
    entities: tuple[RecordEntity, ...] = Field(default=(), max_length=100)
    relationships: tuple[RecordRelationship, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def refs_resolve(self) -> "KnowledgeRecord":
        refs = [item.ref for item in self.entities]
        if len(refs) != len(set(refs)):
            raise ValueError("entity refs must be unique within a record")
        known = set(refs)
        for item in self.relationships:
            if item.subject_ref not in known or item.object_ref not in known:
                raise ValueError("relationship refs must name entities in the same record")
        return self


class IngestResult(Frozen):
    entity_ids: dict[str, str]
    relationships: tuple[Relationship, ...]
