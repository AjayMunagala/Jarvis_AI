"""Validated, vendor-neutral provider response contract."""

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class ProviderReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: StrictStr = Field(min_length=1, max_length=40000)

    @field_validator("content")
    @classmethod
    def require_nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Provider content must not be blank.")
        return value


class _CompletionMessage(BaseModel):
    content: str | None
    refusal: str | None = None


class _CompletionChoice(BaseModel):
    message: _CompletionMessage
    finish_reason: str | None = None


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choices: list[_CompletionChoice] = Field(min_length=1)
