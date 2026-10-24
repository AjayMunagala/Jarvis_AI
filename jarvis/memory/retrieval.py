"""Bounded, best-effort memory and document retrieval for chat context."""

import logging
from time import monotonic, perf_counter
from uuid import UUID

from jarvis.memory.base import MemoryDeadlineExceeded, MemoryRepository
from jarvis.memory.models import MAX_RETRIEVED_RECORDS, MemoryRetrievalResult, RetrievedMemory
from jarvis.memory.query import extract_query_terms
from jarvis.memory.ranking import rank_candidates

logger = logging.getLogger(__name__)

# Upper bound on owner-scoped rows PostgreSQL returns for application ranking.
CONTEXT_CANDIDATE_LIMIT = 40


def _retrieved(item) -> RetrievedMemory:
    return RetrievedMemory(
        id=item.id,
        category=item.category,
        content=item.content,
        source_kind=getattr(item, "source_kind", "memory"),
        source_reference=getattr(item, "source_reference", None),
        updated_at=getattr(item, "updated_at", None),
    )


def retrieve_context(
    repository: MemoryRepository | None,
    query: str,
    *,
    owner_id: UUID,
    limit: int,
    execution_id: str,
    deadline: float | None = None,
    document_limit: int = 0,
) -> MemoryRetrievalResult:
    """Return ranked, owner-scoped records and emit metadata-only diagnostics.

    ``limit`` bounds explicit memories (at most 5); ``document_limit`` bounds
    document records (at most 5). Repositories that implement
    ``search_context`` get OR-prefix candidate matching with deterministic
    application ranking; older repositories keep the legacy ranked search.
    """
    started = perf_counter()
    record_limit = min(max(limit, 0), 5)
    document_record_limit = min(max(document_limit, 0), 5)

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

    def finish(records) -> MemoryRetrievalResult:
        records = tuple(records)[:MAX_RETRIEVED_RECORDS]
        record_outcome("success" if records else "empty", len(records))
        return MemoryRetrievalResult(records=records, outcome="success" if records else "empty")

    if repository is None:
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
    try:
        if deadline is not None and monotonic() >= deadline:
            raise MemoryDeadlineExceeded()
        if record_limit == 0 and document_record_limit == 0:
            return finish(())
        context_search = getattr(repository, "search_context", None)
        if callable(context_search):
            terms = extract_query_terms(query)
            if not terms:
                # No searchable terms is a legitimate empty result, not a failure.
                return finish(())
            source_kinds = tuple(
                kind for kind, kind_limit in (("memory", record_limit), ("document", document_record_limit))
                if kind_limit > 0
            )
            candidates = context_search(
                owner_id, terms, deadline=deadline,
                candidate_limit=CONTEXT_CANDIDATE_LIMIT, source_kinds=source_kinds,
            )
            if deadline is not None and monotonic() >= deadline:
                raise MemoryDeadlineExceeded()
            # Never trust a repository to have applied every filter: re-check
            # owner-independent invariants (kind and count) before ranking.
            candidates = tuple(
                item for item in tuple(candidates)[:CONTEXT_CANDIDATE_LIMIT]
                if getattr(item, "source_kind", "memory") in source_kinds
            )
            ranked = rank_candidates(
                terms, candidates, memory_limit=record_limit, document_limit=document_record_limit,
            )
            return finish(_retrieved(item.record) for item in ranked)

        if record_limit == 0:
            return finish(())
        deadline_search = getattr(repository, "search_with_deadline", None)
        if deadline is not None and callable(deadline_search):
            page = deadline_search(owner_id, query, deadline=deadline, limit=record_limit, offset=0)
        else:
            page = repository.search(owner_id, query, limit=record_limit, offset=0)
        if deadline is not None and monotonic() >= deadline:
            raise MemoryDeadlineExceeded()
        return finish(_retrieved(item) for item in page.items[:record_limit])
    except MemoryDeadlineExceeded:
        record_outcome("fallback", 0)
        raise
    except Exception:
        # Do not include exception types or details: third-party messages may contain private data.
        if deadline is not None and monotonic() >= deadline:
            record_outcome("fallback", 0)
            raise MemoryDeadlineExceeded() from None
        record_outcome("fallback", 0)
        return MemoryRetrievalResult(records=(), outcome="fallback")
