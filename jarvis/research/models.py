"""Domain models and schemas for research and document intelligence (v3.4)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Citation(StrictSchema):
    source: str = Field(min_length=1, max_length=500)
    reference: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1000)


class ParsedDocument(StrictSchema):
    id: UUID
    owner_id: UUID
    filename: str = Field(min_length=1, max_length=300)
    content_type: str = Field(min_length=1, max_length=100)
    file_size_bytes: int = Field(ge=0)
    page_count: int = Field(ge=1)
    extracted_text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ResearchSynthesis(StrictSchema):
    id: UUID
    owner_id: UUID
    topic: str = Field(min_length=1, max_length=300)
    source_urls: list[str] = Field(default_factory=list)
    source_document_ids: list[UUID] = Field(default_factory=list)
    synthesis: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime


# ------------------------------------------------------------- Requests

class DocumentParseRequest(StrictSchema):
    filename: str = Field(min_length=1, max_length=300)
    content_type: str = Field(default="text/plain", max_length=100)
    text_content: str | None = None
    data_base64: str | None = None


class ResearchSynthesizeRequest(StrictSchema):
    topic: str = Field(min_length=1, max_length=300)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)
    web_urls: list[str] = Field(default_factory=list, max_length=10)


# ------------------------------------------------------------- Pages

class ParsedDocumentPage(StrictSchema):
    items: list[ParsedDocument]
    total: int = Field(ge=0)


class ResearchSynthesisPage(StrictSchema):
    items: list[ResearchSynthesis]
    total: int = Field(ge=0)
