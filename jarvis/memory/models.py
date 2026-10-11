"""Validated contracts for explicitly managed long-term memories."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


MAX_MEMORY_CONTENT_LENGTH = 4000
MAX_MEMORY_CATEGORY_LENGTH = 80
MAX_MEMORY_SOURCE_LENGTH = 512


class MemoryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reject_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("Memory values must not be blank.")
    return value


class MemoryCreate(MemoryContract):
    content: StrictStr = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    category: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_CATEGORY_LENGTH)
    source_reference: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_SOURCE_LENGTH)

    @field_validator("content", "category", "source_reference")
    @classmethod
    def validate_nonblank(cls, value):
        return _reject_blank(value)


class MemoryUpdate(MemoryContract):
    content: StrictStr | None = Field(default=None, min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    category: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_CATEGORY_LENGTH)
    source_reference: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_SOURCE_LENGTH)

    @field_validator("content", "category", "source_reference")
    @classmethod
    def validate_nonblank(cls, value):
        return _reject_blank(value)

    @model_validator(mode="after")
    def require_changes(self) -> "MemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one memory field must be supplied.")
        if "content" in self.model_fields_set and self.content is None:
            raise ValueError("Memory content cannot be cleared.")
        return self


class MemorySearchRequest(MemoryContract):
    query: StrictStr = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Search query must not be blank.")
        return value


class MemoryRecord(MemoryContract):
    id: UUID
    content: StrictStr = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    category: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_CATEGORY_LENGTH)
    source_reference: StrictStr | None = Field(default=None, max_length=MAX_MEMORY_SOURCE_LENGTH)
    created_at: datetime
    updated_at: datetime

    @field_validator("content", "category", "source_reference")
    @classmethod
    def validate_nonblank(cls, value):
        return _reject_blank(value)


class MemoryPage(MemoryContract):
    items: tuple[MemoryRecord, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    has_more: bool


class RetrievedMemory(MemoryContract):
    """Minimal memory fields allowed into a model request."""

    id: UUID
    category: StrictStr | None = None
    content: StrictStr = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
