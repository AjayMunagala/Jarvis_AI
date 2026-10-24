"""Database-independent long-term memory repository contract."""

from typing import Protocol, Sequence
from uuid import UUID

from jarvis.memory.models import MemoryPage, MemoryRecord, MemoryUpdate, SourceKind


class MemoryRepository(Protocol):
    def create(self, owner_id: UUID, content: str, category: str | None = None, source_reference: str | None = None) -> MemoryRecord: ...

    def create_document(self, owner_id: UUID, content: str, filename: str) -> tuple[MemoryRecord, bool]:
        """Store one document record; return (record, created). Exact duplicates return the existing record."""
        ...

    def list(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0, category: str | None = None,
        source_kind: SourceKind | None = None,
    ) -> MemoryPage: ...

    def get(self, owner_id: UUID, memory_id: UUID) -> MemoryRecord | None: ...

    def update(self, owner_id: UUID, memory_id: UUID, changes: MemoryUpdate) -> MemoryRecord | None: ...

    def delete(self, owner_id: UUID, memory_id: UUID) -> bool: ...

    def search(self, owner_id: UUID, query: str, *, limit: int = 10, offset: int = 0) -> MemoryPage: ...


class DeadlineAwareMemorySearch(Protocol):
    """Optional retrieval capability for repositories that can enforce deadlines."""

    def search_with_deadline(
        self, owner_id: UUID, query: str, *, deadline: float, limit: int = 10, offset: int = 0
    ) -> MemoryPage: ...


class ContextCandidateSearch(Protocol):
    """Optional chat-context capability: owner-scoped OR-prefix candidate matching.

    ``terms`` are letter/digit stems from ``jarvis.memory.query``. Results are
    unranked candidates; ``jarvis.memory.ranking`` decides the final order.
    """

    def search_context(
        self, owner_id: UUID, terms: Sequence[str], *, deadline: float | None,
        candidate_limit: int, source_kinds: Sequence[SourceKind],
    ) -> tuple[MemoryRecord, ...]: ...


class MemoryPersistenceError(Exception):
    """Sanitized memory repository failure safe to cross the API boundary."""

    def __init__(self) -> None:
        super().__init__("Memory persistence is unavailable.")


class MemoryDeadlineExceeded(TimeoutError):
    """The shared request deadline expired during memory retrieval."""
