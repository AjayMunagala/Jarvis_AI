"""Bounded, best-effort memory retrieval for chat context."""

import logging
from time import perf_counter

from jarvis.memory.base import MemoryRepository, MemoryPersistenceError
from jarvis.memory.models import MemoryRetrievalResult, RetrievedMemory

logger = logging.getLogger(__name__)


def retrieve_context(
    repository: MemoryRepository | None,
    query: str,
    *,
    limit: int,
    execution_id: str,
) -> MemoryRetrievalResult:
    """Return bounded ranked context and emit metadata-only outcome diagnostics."""
    started = perf_counter()
    record_limit = min(max(limit, 0), 5)

    def record_outcome(outcome: str, record_count: int) -> None:
        logger.info(
            "Memory retrieval completed",
            extra={
                "execution_id": execution_id,
                "outcome": outcome,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "record_count": record_count,
            },
        )

    if repository is None:
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
    try:
        if record_limit == 0:
            record_outcome("empty", 0)
            return MemoryRetrievalResult(records=(), outcome="empty")
        page = repository.search(query, limit=record_limit, offset=0)
        records = tuple(
            RetrievedMemory(id=item.id, category=item.category, content=item.content)
            for item in page.items[:record_limit]
        )
        record_outcome("success" if records else "empty", len(records))
        return MemoryRetrievalResult(records=records, outcome="success" if records else "empty")
    except MemoryPersistenceError:
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
    except Exception:
        # Do not include exception types or details: third-party messages may contain private data.
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
