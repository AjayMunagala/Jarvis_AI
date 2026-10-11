"""Database-independent long-term memory repository contract."""

from typing import Protocol
from uuid import UUID

from jarvis.memory.models import MemoryPage, MemoryRecord, MemoryUpdate


class MemoryRepository(Protocol):
    def create(self, content: str, category: str | None = None, source_reference: str | None = None) -> MemoryRecord: ...

    def list(self, *, limit: int = 50, offset: int = 0) -> MemoryPage: ...

    def get(self, memory_id: UUID) -> MemoryRecord | None: ...

    def update(self, memory_id: UUID, changes: MemoryUpdate) -> MemoryRecord | None: ...

    def delete(self, memory_id: UUID) -> bool: ...

    def search(self, query: str, *, limit: int = 10, offset: int = 0) -> MemoryPage: ...


class MemoryPersistenceError(Exception):
    """Sanitized memory repository failure safe to cross the API boundary."""

    def __init__(self) -> None:
        super().__init__("Memory persistence is unavailable.")
