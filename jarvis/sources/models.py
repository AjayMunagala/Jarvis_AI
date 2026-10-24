"""Contracts for opt-in event sources (v7.3): RSS/Atom feeds and watched local folders.

Item text (titles, summaries, file names) comes from outside JARVIS and is untrusted: it is stored and returned
as plain text and never executed. No item text reaches a model in v7.3; ``service.fence_item_for_model`` is
available (and tested) to fence it for the first feature that does.
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
    # v7.3 review: every poll sends at most one notification per source; this is how many titles a summary lists.
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
    last_notified_at: datetime | None = None  # the per-source anti-fatigue throttle survives restarts
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
    # Folder files: ``version`` is the size and mtime last observed, ``settled_version`` the version already
    # reported (or recorded at the first poll); a new or changed file is reported once it is unchanged across two
    # polls, so a file that is still being written is not reported half-done. Feed items have no version.
    version: str | None = Field(default=None, max_length=100)
    settled_version: str | None = Field(default=None, max_length=100)
    notified_at: datetime | None = None


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
    truncated: bool = False  # the feed or folder had more entries than one poll reads
    new: int = Field(default=0, ge=0)
    changed: int = Field(default=0, ge=0)  # folder files whose size or mtime changed (reported once settled)
    pending: int = Field(default=0, ge=0)  # folder files waiting to be unchanged across two polls
    matched: int = Field(default=0, ge=0)
    # At most one notification per poll and source: the item itself, or a summary of several.
    notified: int = Field(default=0, ge=0, le=1)
    held: int = Field(default=0, ge=0, le=1)  # stored until quiet hours end
    suppressed: int = Field(default=0, ge=0, le=1)  # dropped by this source's throttle or an identical held one
    summarized: bool = False
    consecutive_failures: int = Field(default=0, ge=0)
    disabled: bool = False
    error: str | None = Field(default=None, max_length=200)
