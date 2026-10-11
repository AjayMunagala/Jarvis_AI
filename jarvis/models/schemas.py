"""Validated public API schemas."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # Hard ceiling at the schema boundary; Settings can apply a stricter limit.
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: UUID | None = None


class ChatResponse(BaseModel):
    reply: str
    provider: str
    demo: bool
    partial: bool = False
    conversation_id: UUID | None = None


class ConversationHistoryResponse(BaseModel):
    conversation_id: UUID
    messages: list["HistoryMessage"]
    has_more: bool
    next_before_sequence: int | None = None


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


class ErrorResponse(BaseModel):
    error: str
    detail: str
