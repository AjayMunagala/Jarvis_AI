"""Opt-in event sources (v7.3): RSS/Atom feeds and watched local folders."""

from jarvis.sources.models import (
    EventSource, EventSourcePage, PollResult, SourceCreateRequest, SourceItem, SourceItemPage, SourceUpdateRequest,
)
from jarvis.sources.service import SourceConflict, SourceInvalid, SourceNotFound, SourcesService
from jarvis.sources.stores import MemorySourceStore, PostgresSourceStore, SourceStorageError

__all__ = [
    "EventSource", "EventSourcePage", "MemorySourceStore", "PollResult", "PostgresSourceStore", "SourceConflict",
    "SourceCreateRequest", "SourceInvalid", "SourceItem", "SourceItemPage", "SourceNotFound", "SourceStorageError",
    "SourceUpdateRequest", "SourcesService",
]
