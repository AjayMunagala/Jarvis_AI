"""Deterministic application-side ranking for lexical context candidates.

PostgreSQL selects a bounded, owner-scoped candidate set with an OR prefix
query. This module decides relevance order in plain Python so the ordering is
reproducible and testable without a database. The sort key is:

1. coverage: number of distinct query terms present (more is better);
2. phrase hits: adjacent query-term pairs that are adjacent in the record;
3. density: matching tokens divided by sqrt(record significant-token count);
4. most recently updated, then UUID text, as stable tie-breakers.

Records whose coverage is below half of the best candidate's coverage are
dropped as weak matches. Records with identical normalized content are
collapsed, keeping the higher-ranked copy.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, sqrt
from typing import Sequence

from jarvis.memory.models import MemoryRecord
from jarvis.memory.query import is_significant, normalize_text, tokenize

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RankedRecord:
    record: MemoryRecord
    coverage: int
    phrase_hits: int
    density: float


def _matches(token: str, term: str) -> bool:
    return token.startswith(term)


def score_record(terms: Sequence[str], record: MemoryRecord) -> RankedRecord:
    tokens = [token for token in tokenize(record.content) if is_significant(token)]
    matched_terms = [term for term in terms if any(_matches(token, term) for token in tokens)]
    matching_tokens = sum(1 for token in tokens if any(_matches(token, term) for term in terms))
    phrase_hits = 0
    for left, right in zip(terms, terms[1:]):
        if any(
            _matches(first, left) and _matches(second, right)
            for first, second in zip(tokens, tokens[1:])
        ):
            phrase_hits += 1
    density = matching_tokens / sqrt(max(len(tokens), 1))
    return RankedRecord(record, len(matched_terms), phrase_hits, round(density, 9))


def _sort_key(item: RankedRecord):
    updated = item.record.updated_at or _EPOCH
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (-item.coverage, -item.phrase_hits, -item.density, -updated.timestamp(), str(item.record.id))


def content_fingerprint(content: str) -> str:
    return " ".join(normalize_text(content).split())


def rank_candidates(
    terms: Sequence[str],
    candidates: Sequence[MemoryRecord],
    *,
    memory_limit: int,
    document_limit: int,
) -> tuple[RankedRecord, ...]:
    """Rank, filter, de-duplicate, and apply per-source-kind limits."""
    if not terms:
        return ()
    scored = [score_record(terms, record) for record in candidates]
    scored = [item for item in scored if item.coverage > 0]
    if not scored:
        return ()
    best = max(item.coverage for item in scored)
    minimum = max(1, ceil(best / 2))
    ordered = sorted((item for item in scored if item.coverage >= minimum), key=_sort_key)

    limits = {"memory": max(memory_limit, 0), "document": max(document_limit, 0)}
    counts = {"memory": 0, "document": 0}
    seen: set[str] = set()
    selected: list[RankedRecord] = []
    for item in ordered:
        fingerprint = content_fingerprint(item.record.content)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kind = item.record.source_kind
        if counts[kind] >= limits[kind]:
            continue
        counts[kind] += 1
        selected.append(item)
    return tuple(selected)
