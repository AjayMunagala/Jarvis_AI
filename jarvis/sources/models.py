"""Contracts for opt-in event sources (v7.3): RSS/Atom feeds and watched local folders.

Item text (titles, summaries, file names) comes from outside JARVIS and is untrusted: it is stored and returned
as plain text, never executed, and fenced before it is shown to a model (``service.fence_item_for_model``).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceKind = Literal["rss", "folder"]
Importance = Literal["low", "normal", "high"]  # never "urgent": a feed item must not bypass quiet hours
PollStatus = Literal["ok", "failed", "skipped"]

MAX_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 100


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _clean_keywords(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value).split())
        if not text:
            raise ValueError("Keywords must not be empty.")
        if len(text) > MAX_KEYWORD_LENGTH:
            raise ValueError(f"Each keyword must be at most {MAX_KEYWORD_LENGTH} characters.")
        if text.casefold() not in seen:
            seen.add(text.casefold())
            cleaned.append(text)
    return cleaned


class SourceConfig(StrictSchema):
    """``url`` for an RSS/Atom source; ``path`` (and optional ``recursive_depth``) for a folder source."""

    url: str | None = Field(default=None, min_length=1, max_length=2000)
    path: str | None = Field(default=None, min_length=1, max_length=1000)
    recursive_depth: int = Field(default=0, ge=0, le=3)


class _Filters(StrictSchema):
    include_keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        return _clean_keywords(value)


class SourceCreateRequest(_Filters):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=120)
    config: SourceConfig
    enabled: bool = True
    poll_interval_minutes: int = Field(default=60, ge=15, le=1440)
    importance: Importance = "normal"
    max_items_per_poll: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _config_matches_kind(self) -> "SourceCreateRequest":
        if self.kind == "rss":
            if self.config.url is None or self.config.path is not None or self.config.recursive_depth:
                raise ValueError("An rss source needs config.url only.")
        elif self.config.path is None or self.config.url is not None:
            raise ValueError("A folder source needs config.path (and optionally config.recursive_depth).")
        return self


class SourceUpdateRequest(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    poll_interval_minutes: int | None = Field(default=None, ge=15, le=1440)
    importance: Importance | None = None
    max_items_per_poll: int | None = Field(default=None, ge=1, le=20)
    include_keywords: list[str] | None = Field(default=None, max_length=MAX_KEYWORDS)
    exclude_keywords: list[str] | None = Field(default=None, max_length=MAX_KEYWORDS)

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def _keywords(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_keywords(value)


class EventSource(_Filters):
    id: UUID
    owner_id: UUID
    kind: SourceKind
    name: str = Field(min_length=1, max_length=120)
    config: SourceConfig
    enabled: bool
    poll_interval_minutes: int = Field(ge=15, le=1440)
    importance: Importance
    max_items_per_poll: int = Field(ge=1, le=20)
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=200)
    backoff_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EventSourcePage(StrictSchema):
    items: list[EventSource]
    total: int = Field(ge=0)


class SourceItem(StrictSchema):
    """One seen item. ``title``/``summary``/``link`` are untrusted plain text from the source."""

    source_id: UUID
    owner_id: UUID
    item_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=300)
    link: str | None = Field(default=None, max_length=2000)
    published_at: datetime | None = None
    summary: str | None = Field(default=None, max_length=1000)
    matched: bool
    first_seen_at: datetime
    last_seen_at: datetime


class SourceItemPage(StrictSchema):
    items: list[SourceItem]
    untrusted: bool = True
    note: str = "Titles, links and summaries come from the source and are untrusted plain text."


class PollResult(StrictSchema):
    source_id: UUID
    status: PollStatus
    trigger: Literal["manual", "scheduled"]
    baseline: bool = False  # first successful poll: existing items were recorded as seen without notifying
    fetched: int = Field(default=0, ge=0)
    new: int = Field(default=0, ge=0)
    matched: int = Field(default=0, ge=0)
    notified: int = Field(default=0, ge=0)
    held: int = Field(default=0, ge=0)
    suppressed: int = Field(default=0, ge=0)  # dropped by the anti-fatigue throttle or already held
    summarized: bool = False
    consecutive_failures: int = Field(default=0, ge=0)
    disabled: bool = False
    error: str | None = Field(default=None, max_length=200)
