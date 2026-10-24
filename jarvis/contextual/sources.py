"""Adapter boundaries for memory candidates and similarity scoring.

Implemented today:

- ``RepositoryCandidateSource`` reads the authenticated owner's records
  through the existing ``MemoryRepository.search`` (lexical, PostgreSQL) and
  maps them with ``from_memory_records``. It never writes.
- ``LexicalSimilarity`` (in ``selection``) is the default scorer.
- ``checked_similarity`` wraps any scorer so that a non-numeric, NaN or
  out-of-range score is treated as no match (0.0) and counted, instead of
  distorting ranking.

Proposed for the knowledge workstream (not implemented here): a semantic
candidate source and embedding scorer implementing these protocols. Nothing
in this module claims pgvector or embedding support.
"""

import math
from typing import Protocol, Sequence
from uuid import UUID

from jarvis.contextual.adapters import from_memory_records
from jarvis.contextual.models import MemoryCandidate

MAX_CANDIDATES = 50


class CandidateSource(Protocol):
    name: str

    def candidates(self, owner_id: UUID, query: str, *, limit: int) -> Sequence[MemoryCandidate]: ...


class RepositoryCandidateSource:
    name = "memory-repository (lexical)"

    def __init__(self, repository):
        self.repository = repository

    def candidates(self, owner_id, query, *, limit):
        page = self.repository.search(owner_id, query[:200], limit=max(1, min(limit, MAX_CANDIDATES)), offset=0)
        return from_memory_records(owner_id, page.items)


class checked_similarity:  # noqa: N801 - used like a function wrapper
    """Validate a scorer's output: finite numbers in [0, 1] only."""

    def __init__(self, scorer):
        self.scorer = scorer
        self.name = getattr(scorer, "name", "custom")
        self.invalid = 0

    def __call__(self, query: str, content: str) -> float:
        try:
            value = self.scorer(query, content)
        except Exception:  # noqa: BLE001 - a failing scorer must not break context assembly
            self.invalid += 1
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not 0.0 <= value <= 1.0:
            self.invalid += 1
            return 0.0
        return float(value)


def validate_candidates(owner_id: UUID, items: Sequence[object]) -> tuple[list[MemoryCandidate], int]:
    """Keep only well-formed candidates of this owner; return (kept, rejected count)."""
    kept, rejected = [], 0
    for item in list(items)[:MAX_CANDIDATES * 4]:
        try:
            candidate = item if isinstance(item, MemoryCandidate) else MemoryCandidate.model_validate(item)
        except Exception:  # noqa: BLE001
            rejected += 1
            continue
        if candidate.owner_id != owner_id:
            rejected += 1
            continue
        kept.append(candidate)
    return kept, rejected
