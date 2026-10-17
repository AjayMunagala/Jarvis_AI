"""Adapters from existing JARVIS records to ``MemoryCandidate``.

These read the current contracts (``MemoryRecord``, ``HistoryEntry``) as they
are; nothing about their storage changes.
"""

from typing import Iterable
from uuid import UUID

from jarvis.contextual.models import MemoryCandidate
from jarvis.memory.context import HistoryEntry
from jarvis.memory.models import MemoryRecord


def from_memory_records(owner_id: UUID, records: Iterable[MemoryRecord], *,
                        project_id: str | None = None) -> list[MemoryCandidate]:
    """Explicit memories -> long_term (user_stated); documents -> retrieved_knowledge (document).

    Repositories only return the authenticated owner's records, so ``owner_id``
    is the caller's identity, not a value read from the record.
    """
    candidates = []
    for record in records:
        document = record.source_kind == "document"
        candidates.append(MemoryCandidate(
            candidate_id=str(record.id), owner_id=owner_id,
            category="retrieved_knowledge" if document else "long_term", content=record.content,
            source_ref=f"{record.source_kind}:{record.id}", project_id=project_id, updated_at=record.updated_at,
            reliability="document" if document else "user_stated",
        ))
    return candidates


def from_history(owner_id: UUID, entries: Iterable[HistoryEntry]) -> list[MemoryCandidate]:
    return [MemoryCandidate(candidate_id=str(entry.id), owner_id=owner_id, category="conversation",
                            content=entry.content, source_ref=f"conversation:{entry.id}", updated_at=entry.created_at,
                            reliability="user_stated" if entry.role == "user" else "inferred")
            for entry in entries]
