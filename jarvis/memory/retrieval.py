"""Bounded, best-effort memory retrieval for chat context."""

import logging
from time import monotonic, perf_counter
from uuid import UUID

from jarvis.memory.base import MemoryDeadlineExceeded, MemoryRepository, MemoryPersistenceError
from jarvis.memory.models import MemoryRetrievalResult, RetrievedMemory

logger = logging.getLogger(__name__)


def retrieve_context(
    repository: MemoryRepository | None,
    query: str,
    *,
    owner_id: UUID,
    limit: int,
    execution_id: str,
    deadline: float | None = None,
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
        if deadline is not None and monotonic() >= deadline:
            raise MemoryDeadlineExceeded()
        if record_limit == 0:
            record_outcome("empty", 0)
            return MemoryRetrievalResult(records=(), outcome="empty")
        deadline_search = getattr(repository, "search_with_deadline", None)
        if deadline is not None and callable(deadline_search):
            page = deadline_search(owner_id, query, deadline=deadline, limit=record_limit, offset=0)
        else:
            page = repository.search(owner_id, query, limit=record_limit, offset=0)
        if deadline is not None and monotonic() >= deadline:
            raise MemoryDeadlineExceeded()
        records = tuple(
            RetrievedMemory(id=item.id, category=item.category, content=item.content)
            for item in page.items[:record_limit]
        )
        record_outcome("success" if records else "empty", len(records))
        return MemoryRetrievalResult(records=records, outcome="success" if records else "empty")
    except MemoryDeadlineExceeded:
        record_outcome("fallback", 0)
        raise
    except MemoryPersistenceError:
        if deadline is not None and monotonic() >= deadline:
            record_outcome("fallback", 0)
            raise MemoryDeadlineExceeded() from None
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
    except Exception:
        # Do not include exception types or details: third-party messages may contain private data.
        if deadline is not None and monotonic() >= deadline:
            record_outcome("fallback", 0)
            raise MemoryDeadlineExceeded() from None
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
