"""Validated public API schemas."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_serializer
from uuid import UUID


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # Hard ceiling at the schema boundary; Settings can apply a stricter limit.
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: UUID | None = None


class ChatMemoryContext(BaseModel):
    outcome: Literal["success", "empty", "fallback"]
    memory_ids: list[UUID]


class ChatContextSource(BaseModel):
    """One record supplied to the model; metadata only, never content."""

    ref: str
    source_type: Literal["conversation", "memory", "document"]
    source_id: UUID
    label: str | None = None
    role: Literal["user", "assistant"] | None = None
    timestamp: datetime | None = None
    excerpt_chars: int = Field(ge=0)
    truncated: bool
    # True only when the reply contains this exact "[ref]" marker. It shows
    # what the model cited, not that the cited fact was used correctly.
    cited_in_reply: bool


class ChatContextSummary(BaseModel):
    history_status: Literal["included", "empty", "not_requested", "disabled", "unavailable"]
    retrieval_status: Literal["success", "empty", "fallback", "disabled"]
    sources: list[ChatContextSource]
    omitted_count: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    unknown_citation_count: int = Field(ge=0)


class ChatResponse(BaseModel):
    reply: str
    provider: str
    demo: bool
    partial: bool = False
    conversation_id: UUID | None = None
    memory_context: ChatMemoryContext | None = None
    context: ChatContextSummary | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_context(self, handler):
        # Stateless chat has no context assembly; keep its response shape unchanged.
        data = handler(self)
        if isinstance(data, dict) and data.get("context") is None:
            data.pop("context", None)
        return data


class ConversationHistoryResponse(BaseModel):
    conversation_id: UUID
    messages: list["HistoryMessage"]
    has_more: bool
    next_before_sequence: int | None = None


CONVERSATION_LIST_DEFAULT_LIMIT = 20
CONVERSATION_LIST_MAX_LIMIT = 100
CONVERSATION_LIST_MAX_OFFSET = 1_000_000
CONVERSATION_PREVIEW_MAX_LENGTH = 120


class ConversationSummary(BaseModel):
    """Bounded metadata for one persisted conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    created_at: datetime
    last_activity_at: datetime
    preview: str | None = Field(default=None, max_length=CONVERSATION_PREVIEW_MAX_LENGTH)


class ConversationListResponse(BaseModel):
    """One bounded page of conversation summaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ConversationSummary]
    limit: int = Field(ge=1, le=CONVERSATION_LIST_MAX_LIMIT)
    offset: int = Field(ge=0, le=CONVERSATION_LIST_MAX_OFFSET)
    has_more: bool


class HistoryMessage(BaseModel):
    id: UUID
    conversation_id: UUID
    sequence_number: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    components: dict[str, Literal["ready", "configured", "disabled", "unavailable"]]


class ErrorResponse(BaseModel):
    error: str
    detail: str


class RepoSearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    search_type: Literal["text", "filename", "symbol"] = "text"
    is_regex: bool = False
    case_sensitive: bool = False
    path_filter: str | None = Field(default=None, max_length=255)
    max_results: int = Field(default=50, ge=1, le=200)


class RepoDiagnosticRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    target_files: list[str] | None = Field(default=None, max_length=50)
