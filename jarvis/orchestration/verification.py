"""Deterministic evidence provenance checks (not claim or fact verification)."""

import unicodedata
from collections import Counter

from jarvis.orchestration.models import (
    EvidenceProvenance,
    ExpertResult,
    RequestContext,
)

# Contracts bound normal orchestration inputs. These independent limits also
# protect direct checker callers and model_construct-created objects.
MAX_EVIDENCE_TEXT_LENGTH = 40_000
MAX_SOURCE_TEXT_LENGTH = 40_000
MAX_PROVENANCE_RESULTS = 25
MAX_PROVENANCE_TARGETS = 1_250
MAX_EVIDENCE_ITEMS_PER_COLLECTION = 50
MAX_COMPARISON_CHARACTERS = 10_000_000


def _normalize(value: str) -> str:
    """Apply NFKC, case folding, and whitespace collapsing; retain punctuation."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _sequence_items(value: object) -> list[object]:
    """None means no collection; malformed explicit scalar items stay visible."""
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        # Copy only a bounded prefix. An overlarge collection is rejected by
        # the target collector without materializing the caller's full list.
        return list(value[: MAX_EVIDENCE_ITEMS_PER_COLLECTION + 1])
    return [value]


def _evidence_texts(result: ExpertResult) -> tuple[object, ...]:
    """Extract structured evidence fields; never treat evidence as a source."""
    output = getattr(result, "output", None)
    if output is None:
        return ()

    evidence = _sequence_items(getattr(output, "evidence", None))
    if len(evidence) > MAX_EVIDENCE_ITEMS_PER_COLLECTION:
        return tuple(evidence)
    analysis = getattr(output, "analysis", None)
    if analysis is not None:
        for conclusion in _sequence_items(getattr(analysis, "supported_conclusions", None)):
            evidence.extend(_sequence_items(getattr(conclusion, "evidence", None)))
            if len(evidence) > MAX_EVIDENCE_ITEMS_PER_COLLECTION:
                return tuple(evidence[: MAX_EVIDENCE_ITEMS_PER_COLLECTION + 1])
    validation = getattr(output, "validation", None)
    if validation is not None:
        for finding in _sequence_items(getattr(validation, "findings", None)):
            evidence.extend(_sequence_items(getattr(finding, "evidence", None)))
            if len(evidence) > MAX_EVIDENCE_ITEMS_PER_COLLECTION:
                return tuple(evidence[: MAX_EVIDENCE_ITEMS_PER_COLLECTION + 1])
    return tuple(evidence)


def _identifier_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 80
        and all("a" <= character <= "z" or "0" <= character <= "9" or character in "_-" for character in value)
    )


def _targets(results: object) -> list[tuple[object, int, object]]:
    """Collect bounded, representable targets without trusting constructed models."""
    if not isinstance(results, (tuple, list)) or len(results) > MAX_PROVENANCE_RESULTS:
        return []
    targets: list[tuple[object, int, object]] = []
    for result in results:
        try:
            if result.status != "succeeded" or result.output is None:
                continue
            step_id, expert_id = result.step_id, result.expert_id
            evidence_items = _evidence_texts(result)
        except Exception:
            continue
        if not _identifier_is_valid(step_id) or not _identifier_is_valid(expert_id):
            # Such an outcome cannot satisfy EvidenceProvenance's identifier contract.
            continue
        if len(evidence_items) > 50 or len(targets) + len(evidence_items) > MAX_PROVENANCE_TARGETS:
            return []
        targets.extend((result, index, item) for index, item in enumerate(evidence_items))
    return targets


def _build_outcomes(targets, outcome: str) -> tuple[EvidenceProvenance, ...]:
    """Build results defensively; malformed internal objects cannot break fallback."""
    outcomes: list[EvidenceProvenance] = []
    for result, index, _ in targets:
        try:
            outcomes.append(EvidenceProvenance(
                step_id=result.step_id,
                expert_id=result.expert_id,
                evidence_index=index,
                outcome=outcome,
            ))
        except Exception:
            # Internal malformed identifiers are not representable in the report.
            continue
    return tuple(outcomes)


def _normalize_with_cost(raw: str, budget: int) -> tuple[str, int] | None:
    # Check the raw input cost before normalization so exhausted callers do not
    # keep normalizing later evidence or sources. Both input and output count.
    if len(raw) > budget:
        return None
    normalized = _normalize(raw)
    cost = len(raw) + len(normalized)
    return (normalized, cost) if cost <= budget else None


def verify_evidence_provenance(
    context: RequestContext,
    results: tuple[ExpertResult, ...],
    *,
    enabled: bool = True,
) -> tuple[EvidenceProvenance, ...]:
    """Locate evidence in the request or another expert's output summary.

    Matching uses Unicode NFKC, case folding, collapsed whitespace, retained
    punctuation, and exact normalized substring matching. Request text has
    precedence over peer summaries. If the request cannot be normalized or
    compared within the shared work budget, evidence remains ``unverified``;
    the checker never falls through to a peer source. Peer summaries use plan
    result order, but duplicate expert identifiers are excluded as ambiguous.

    The budget includes evidence/source normalization and substring comparison.
    Direct callers are also bounded by source size, result count, and target
    count. No text is included in returned provenance records.
    """
    targets = _targets(results)
    if not targets:
        return ()
    if not enabled:
        return _build_outcomes(targets, "not_evaluated")

    remaining_work = MAX_COMPARISON_CHARACTERS
    try:
        request_text = context.message
    except Exception:
        return _build_outcomes(targets, "unverified")
    if not isinstance(request_text, str) or len(request_text) > MAX_SOURCE_TEXT_LENGTH:
        return _build_outcomes(targets, "unverified")

    normalized_request = _normalize_with_cost(request_text, remaining_work)
    if normalized_request is None:
        return _build_outcomes(targets, "unverified")
    request_source, request_cost = normalized_request
    remaining_work -= request_cost

    # A repeated expert ID has ambiguous ownership and is not a peer source.
    successful_ids = []
    for candidate in results:
        try:
            if candidate.status == "succeeded" and candidate.output is not None:
                successful_ids.append(candidate.expert_id)
        except Exception:
            continue
    id_counts = Counter(item for item in successful_ids if _identifier_is_valid(item))
    peer_results = [
        candidate for candidate in results
        if getattr(candidate, "status", None) == "succeeded"
        and getattr(candidate, "output", None) is not None
        and _identifier_is_valid(getattr(candidate, "expert_id", None))
        and id_counts[getattr(candidate, "expert_id", None)] == 1
    ]
    normalized_peers: dict[int, tuple[str, str] | None] = {}

    outcomes: list[EvidenceProvenance] = []
    for target_index, (result, index, raw_evidence) in enumerate(targets):
        if not isinstance(raw_evidence, str) or not raw_evidence.strip() or len(raw_evidence) > MAX_EVIDENCE_TEXT_LENGTH:
            outcomes.extend(_build_outcomes(targets[target_index:target_index + 1], "unverified"))
            continue
        normalized_item = _normalize_with_cost(raw_evidence, remaining_work)
        if normalized_item is None:
            outcomes.extend(_build_outcomes(targets[target_index:], "unverified"))
            break
        normalized_evidence, evidence_cost = normalized_item
        remaining_work -= evidence_cost
        if not normalized_evidence:
            outcomes.extend(_build_outcomes(targets[target_index:target_index + 1], "unverified"))
            continue

        # The request is always evaluated first. If this comparison cannot fit,
        # fail closed for this and later evidence; do not attribute to a peer.
        if len(request_source) > remaining_work:
            outcomes.extend(_build_outcomes(targets[target_index:], "unverified"))
            break
        remaining_work -= len(request_source)
        if normalized_evidence in request_source:
            outcomes.append(EvidenceProvenance(
                step_id=result.step_id,
                expert_id=result.expert_id,
                evidence_index=index,
                outcome="source_match",
                source_kind="user_request",
            ))
            continue

        matched_peer: str | None = None
        budget_exhausted = False
        for peer in peer_results:
            peer_id = peer.expert_id
            if peer_id == result.expert_id:
                continue
            key = id(peer)
            if key not in normalized_peers:
                try:
                    raw_source = peer.output.content
                except Exception:
                    normalized_peers[key] = None
                    continue
                if not isinstance(raw_source, str) or len(raw_source) > MAX_SOURCE_TEXT_LENGTH:
                    normalized_peers[key] = None
                    continue
                normalized_source = _normalize_with_cost(raw_source, remaining_work)
                if normalized_source is None:
                    budget_exhausted = True
                    break
                peer_text, cost = normalized_source
                remaining_work -= cost
                normalized_peers[key] = (peer_id, peer_text)
            source = normalized_peers[key]
            if source is None or not source[1]:
                continue
            if len(source[1]) > remaining_work:
                budget_exhausted = True
                break
            remaining_work -= len(source[1])
            if normalized_evidence in source[1]:
                matched_peer = source[0]
                break

        if matched_peer is not None:
            outcomes.append(EvidenceProvenance(
                step_id=result.step_id,
                expert_id=result.expert_id,
                evidence_index=index,
                outcome="source_match",
                source_kind="expert_output",
                source_expert_id=matched_peer,
            ))
        else:
            outcomes.extend(_build_outcomes(targets[target_index:target_index + 1], "unverified"))
            if budget_exhausted or remaining_work <= 0:
                outcomes.extend(_build_outcomes(targets[target_index + 1:], "unverified"))
                break

    return tuple(outcomes)


def not_evaluated_evidence_provenance(
    results: tuple[ExpertResult, ...],
) -> tuple[EvidenceProvenance, ...]:
    """Build a best-effort fallback report; malformed internal data is omitted."""
    try:
        targets = _targets(results)
        return _build_outcomes(targets, "not_evaluated")
    except Exception:
        return ()
