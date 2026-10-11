"""Validated public API schemas."""

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # Hard ceiling at the schema boundary; Settings can apply a stricter limit.
    message: str = Field(min_length=1, max_length=20000)


class ChatResponse(BaseModel):
    reply: str
    provider: str
    demo: bool
    partial: bool = False


class HealthResponse(BaseModel):
    status: str
    version: str


class ErrorResponse(BaseModel):
    error: str
    detail: str
