"""Domain models and schemas for knowledge intelligence & context continuity (v3.3)."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    MANUAL = "manual"
    NOTE = "note"
    FILE = "file"
    WEB = "web"
    MEMORY = "memory"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ------------------------------------------------------------- Entities

class KnowledgeChunk(StrictSchema):
    id: UUID
    document_id: UUID
    owner_id: UUID
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1)
    token_count: int = Field(ge=0)
    created_at: datetime


class KnowledgeDocument(StrictSchema):
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=300)
    source_type: SourceType = SourceType.MANUAL
    source_uri: str | None = Field(default=None, max_length=1000)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------- Requests

class KnowledgeDocumentCreateRequest(StrictSchema):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_000_000)
    source_type: SourceType = SourceType.MANUAL
    source_uri: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchRequest(StrictSchema):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=50)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)


class ContextBudgetRequest(StrictSchema):
    prompt: str = Field(min_length=1, max_length=50_000)
    max_context_tokens: int = Field(default=4000, ge=500, le=32000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)


# ------------------------------------------------------------- Responses

class ScoredChunk(StrictSchema):
    chunk: KnowledgeChunk
    document_title: str
    score: float
    match_type: str


class KnowledgeSearchResponse(StrictSchema):
    query: str
    results: list[ScoredChunk]
    total_matched: int


class ContextBudgetAllocation(StrictSchema):
    max_tokens: int
    used_tokens: int
    remaining_tokens: int
    sections: dict[str, int]
    selected_chunks: list[KnowledgeChunk]
    # v4.6: chunks left out (budget, duplicates) and chunks shortened to fit; selected_chunks carry the excerpt.
    omitted_count: int = 0
    truncated_count: int = 0


class KnowledgeDocumentPage(StrictSchema):
    items: list[KnowledgeDocument]
    total: int = Field(ge=0)


class KnowledgeDocumentDetail(StrictSchema):
    document: KnowledgeDocument
    chunks: list[KnowledgeChunk]
