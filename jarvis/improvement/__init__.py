"""Controlled Self-Improvement & Hardening (v3.9)."""

from jarvis.improvement.immutable import (
    ImmutableSecurityFileError,
    assert_mutable_target,
)
from jarvis.improvement.models import (
    BenchmarkMetric,
    BenchmarkSuiteResult,
    PromptMutationProposal,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalPage,
)
from jarvis.improvement.service import ImprovementService, generate_unified_diff
from jarvis.improvement.stores import (
    ImprovementStore,
    MemoryImprovementStore,
)

__all__ = [
    "BenchmarkMetric",
    "BenchmarkSuiteResult",
    "ImmutableSecurityFileError",
    "ImprovementService",
    "ImprovementStore",
    "MemoryImprovementStore",
    "PromptMutationProposal",
    "ProposalCreateRequest",
    "ProposalDecisionRequest",
    "ProposalPage",
    "assert_mutable_target",
    "generate_unified_diff",
]
