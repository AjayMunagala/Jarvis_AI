"""Database-independent long-term memory repository contract."""

from typing import Protocol
from uuid import UUID

from jarvis.memory.models import MemoryPage, MemoryRecord, MemoryUpdate


class MemoryRepository(Protocol):
    def create(self, owner_id: UUID, content: str, category: str | None = None, source_reference: str | None = None) -> MemoryRecord: ...

    def list(self, owner_id: UUID, *, limit: int = 50, offset: int = 0, category: str | None = None) -> MemoryPage: ...

    def get(self, owner_id: UUID, memory_id: UUID) -> MemoryRecord | None: ...

    def update(self, owner_id: UUID, memory_id: UUID, changes: MemoryUpdate) -> MemoryRecord | None: ...

    def delete(self, owner_id: UUID, memory_id: UUID) -> bool: ...

    def search(self, owner_id: UUID, query: str, *, limit: int = 10, offset: int = 0) -> MemoryPage: ...


class DeadlineAwareMemorySearch(Protocol):
    """Optional retrieval capability for repositories that can enforce deadlines."""

    def search_with_deadline(
        self, owner_id: UUID, query: str, *, deadline: float, limit: int = 10, offset: int = 0
    ) -> MemoryPage: ...


class MemoryPersistenceError(Exception):
    """Sanitized memory repository failure safe to cross the API boundary."""

    def __init__(self) -> None:
        super().__init__("Memory persistence is unavailable.")


class MemoryDeadlineExceeded(TimeoutError):
    """The shared request deadline expired during memory retrieval."""
