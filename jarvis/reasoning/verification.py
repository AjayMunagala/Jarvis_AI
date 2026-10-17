"""Uncertainty from evidence, and consolidation of independent verifiers.

Rules:

- a claim is ``supported`` only with supporting evidence and no contradicting
  evidence of equal or higher reliability; ``contested`` when credible evidence
  disagrees; ``unsupported`` without supporting evidence;
- a model verifier is an opinion: model agreement alone yields
  ``plausible_unverified``, never ``verified``; any deterministic failure is a
  failure; disagreement between verifiers is ``contested``.
"""

from typing import Iterable, Protocol

from jarvis.reasoning.models import (
    ClaimAssessment, ConsolidatedVerdict, EvidenceItem, UncertaintyAssessment, VerifierVerdict,
)

_RANK = {"verified": 3, "user_stated": 2, "document": 1, "inferred": 0}


class Verifier(Protocol):
    verifier_id: str

    async def verify(self, task: str, answer: str) -> VerifierVerdict: ...


def assess_claims(claim_ids: Iterable[str], evidence: Iterable[EvidenceItem]) -> tuple[tuple[ClaimAssessment, ...],
                                                                                        UncertaintyAssessment]:
    evidence = list(evidence)
    results = []
    for claim in claim_ids:
        support = [item for item in evidence if item.claim_id == claim and item.stance == "supports"]
        against = [item for item in evidence if item.claim_id == claim and item.stance == "contradicts"]
        if not support:
            status = "unsupported"
        elif against and max(_RANK[item.reliability] for item in against) >= max(_RANK[item.reliability] for item in support):
            status = "contested"
        else:
            status = "supported"
        results.append(ClaimAssessment(claim_id=claim, status=status,
                                       supporting=tuple(item.source_ref for item in support),
                                       contradicting=tuple(item.source_ref for item in against)))
    missing = tuple(item.claim_id for item in results if item.status == "unsupported")
    conflicting = tuple(item.claim_id for item in results if item.status == "contested")
    inferred_only = [item.claim_id for item in results if item.status == "supported" and all(
        ev.reliability == "inferred" for ev in evidence if ev.claim_id == item.claim_id and ev.stance == "supports")]
    level = "high" if missing or conflicting else ("medium" if inferred_only else "low")
    notes = tuple(f"{claim} is supported only by inferred evidence" for claim in inferred_only)
    return tuple(results), UncertaintyAssessment(level=level, missing=missing, conflicting=conflicting, notes=notes)


def consolidate(verdicts: Iterable[VerifierVerdict]) -> ConsolidatedVerdict:
    verdicts = tuple(verdicts)
    if not verdicts:
        return ConsolidatedVerdict(status="plausible_unverified", verdicts=(), explanation="No verifier ran.")
    deterministic = [item for item in verdicts if item.kind == "deterministic"]
    models = [item for item in verdicts if item.kind == "model"]
    if any(item.outcome == "disagree" for item in deterministic):
        return ConsolidatedVerdict(status="failed", verdicts=verdicts,
                                   explanation="A deterministic check failed.")
    outcomes = {item.outcome for item in verdicts}
    if "agree" in outcomes and "disagree" in outcomes:
        return ConsolidatedVerdict(status="contested", verdicts=verdicts,
                                   explanation="Verifiers disagree; treat the result as unverified.")
    if deterministic and all(item.outcome == "agree" for item in deterministic) and not any(
            item.outcome == "disagree" for item in models):
        return ConsolidatedVerdict(status="verified", verdicts=verdicts,
                                   explanation="All deterministic checks passed" + (" and model verifiers agree." if models else "."))
    if models and all(item.outcome == "disagree" for item in models):
        return ConsolidatedVerdict(status="contested", verdicts=verdicts, explanation="Model verifiers disagree.")
    return ConsolidatedVerdict(status="plausible_unverified", verdicts=verdicts,
                               explanation="Only model opinions or uncertain checks are available; a second model is "
                                           "not proof of correctness.")
