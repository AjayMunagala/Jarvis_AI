"""Bounded, best-effort memory retrieval for chat context."""

import logging
from jarvis.memory.base import MemoryRepository, MemoryPersistenceError
from jarvis.memory.models import RetrievedMemory

logger = logging.getLogger(__name__)


def retrieve_context(
    repository: MemoryRepository | None,
    query: str,
    *,
    limit: int,
    execution_id: str,
) -> tuple[RetrievedMemory, ...]:
    """Return ranked context, falling back to an empty tuple on storage errors."""
    if repository is None:
        logger.warning("Memory retrieval unavailable", extra={"execution_id": execution_id})
        return ()
    try:
        page = repository.search(query, limit=limit, offset=0)
        return tuple(
            RetrievedMemory(id=item.id, category=item.category, content=item.content)
            for item in page.items
        )
    except MemoryPersistenceError:
        logger.warning("Memory retrieval failed", extra={"execution_id": execution_id})
        return ()
    except Exception as exc:
        # Third-party adapters may fail unexpectedly; do not log exception details or content.
        error_type = type(exc).__name__
        if not error_type.isidentifier() or len(error_type) > 80:
            error_type = "MemoryRetrievalError"
        logger.warning(
            "Memory retrieval failed",
            extra={"execution_id": execution_id, "error_type": error_type},
        )
        return ()
