"""Tests for deterministic evidence provenance, not factual verification."""

import asyncio
import json
import logging
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jarvis.core.config import Settings
from jarvis.orchestration.models import (
    AnalysisExpertOutput,
    EvidenceProvenance,
    ExpertOutput,
    ExpertResult,
    RequestContext,
    SupportedConclusion,
    ValidationExpertOutput,
    ValidationFinding,
)
from jarvis.orchestration.registry import ToolRegistry, ToolRegistryConfig
from jarvis.orchestration.service import OrchestrationService
from jarvis.orchestration.verification import verify_evidence_provenance


def _analysis_result(expert_id: str, content: str, evidence: tuple[str, ...], *, step_id: str | None = None):
    analysis = AnalysisExpertOutput(
        summary=content,
        assessment_status="completed",
        factors=(),
        supported_conclusions=(
            SupportedConclusion(conclusion="A conclusion.", evidence=evidence),
        ) if evidence else (),
        assumptions=(),
        missing_information=(),
        recommendations=(),
    )
    return ExpertResult(
        step_id=step_id or expert_id,
        expert_id=expert_id,
        persona="AnalysisExpert",
        status="succeeded",
        output=ExpertOutput(content=content, analysis=analysis),
        attempts=1,
    )


def test_request_evidence_match_is_case_insensitive_and_collapses_whitespace():
    context = RequestContext(message="The   service\n is READY for local use.")
    result = _analysis_result("analysis", "Summary", ("SERVICE is   ready",))

    report = verify_evidence_provenance(context, (result,))

    assert len(report) == 1
    assert report[0].outcome == "source_match"
    assert report[0].source_kind == "user_request"
    assert report[0].source_expert_id is None


def test_nfkc_compatibility_characters_match_normalized_request():
    result = _analysis_result("analysis", "Summary", ("oﬃce",))
    report = verify_evidence_provenance(RequestContext(message="The office is open."), (result,))

    assert report[0].outcome == "source_match"
    assert report[0].source_kind == "user_request"


def test_matching_retains_punctuation_instead_of_using_fuzzy_text():
    result = _analysis_result("analysis", "Summary", ("signal stable",))
    report = verify_evidence_provenance(
        RequestContext(message="The signal, stable and active."), (result,)
    )

    assert report[0].outcome == "unverified"


def test_evidence_can_match_another_experts_summary_but_not_its_own():
    peer = _analysis_result("analysis", "Peer source: Alpha has 42 items.", ())
    target = _analysis_result("validation", "A separate summary.", ("ALPHA has 42 items",))

    report = verify_evidence_provenance(RequestContext(message="Check this."), (peer, target))

    assert report[0].outcome == "source_match"
    assert report[0].source_kind == "expert_output"
    assert report[0].source_expert_id == "analysis"

    self_only = _analysis_result("analysis", "This source says self evidence.", ("self evidence",))
    self_report = verify_evidence_provenance(RequestContext(message="Nothing relevant."), (self_only,))
    assert self_report[0].outcome == "unverified"
    assert self_report[0].source_kind is None


def test_request_takes_precedence_when_request_and_peer_summary_both_match():
    peer = _analysis_result("analysis", "The shared phrase is here.", ())
    target = _analysis_result("validation", "Target summary", ("shared phrase",))

    report = verify_evidence_provenance(
        RequestContext(message="The shared phrase is also in the request."), (peer, target)
    )

    assert report[-1].outcome == "source_match"
    assert report[-1].source_kind == "user_request"
    assert report[-1].source_expert_id is None


def test_retrieved_memory_content_is_not_an_eligible_source():
    from jarvis.memory.models import RetrievedMemory

    phrase = "memory-only-reference-731"
    result = _analysis_result("analysis", "Unrelated summary", (phrase,))
    context = RequestContext(
        message="Check this request.",
        retrieved_memories=(RetrievedMemory(id=uuid4(), content=phrase),),
    )

    report = verify_evidence_provenance(context, (result,))

    assert report[0].outcome == "unverified"
    assert report[0].source_kind is None


def test_nested_peer_evidence_is_not_an_eligible_source():
    peer = ExpertResult.model_construct(
        step_id="analysis", expert_id="analysis", persona="AnalysisExpert", status="succeeded",
        output=ExpertOutput.model_construct(
            content="Peer summary without the phrase.", evidence=("nested-only-reference",),
            confidence=0.5, advisory_only=True, analysis=None, validation=None,
        ),
        error_code=None, failure_type=None, attempts=1,
    )
    target = _analysis_result("validation", "Target summary", ("nested-only-reference",))

    report = verify_evidence_provenance(RequestContext(message="Unrelated."), (peer, target))

    assert report[-1].outcome == "unverified"


def test_unmatched_and_unusable_evidence_are_unverified_without_retaining_text():
    context = RequestContext(message="Available source text.")
    valid_but_unmatched = _analysis_result("analysis", "Summary", ("not in any source",))
    malformed_output = ExpertOutput.model_construct(
        content="Summary",
        evidence=("", "   ", "\t\n", None, 7),
        advisory_only=True,
        analysis=None,
        validation=None,
        confidence=0.5,
    )
    malformed_result = ExpertResult.model_construct(
        step_id="malformed",
        expert_id="knowledge",
        persona="KnowledgeExpert",
        status="succeeded",
        output=malformed_output,
        error_code=None,
        failure_type=None,
        attempts=1,
    )

    report = verify_evidence_provenance(context, (valid_but_unmatched, malformed_result))

    assert [item.outcome for item in report] == ["unverified"] * 6
    assert all("evidence" not in item.model_dump() for item in report)
    assert all("source text" not in str(item.model_dump()).casefold() for item in report)


def test_none_evidence_collections_do_not_create_phantom_targets():
    result = ExpertResult.model_construct(
        step_id="analysis", expert_id="analysis", persona="AnalysisExpert", status="succeeded",
        output=ExpertOutput.model_construct(
            content="Summary", evidence=None, confidence=0.5, advisory_only=True,
            analysis=None, validation=None,
        ),
        error_code=None, failure_type=None, attempts=1,
    )

    assert verify_evidence_provenance(RequestContext(message="Request."), (result,)) == ()


def test_explicit_none_evidence_element_remains_unverified():
    result = ExpertResult.model_construct(
        step_id="analysis", expert_id="analysis", persona="AnalysisExpert", status="succeeded",
        output=ExpertOutput.model_construct(
            content="Summary", evidence=(None,), confidence=0.5, advisory_only=True,
            analysis=None, validation=None,
        ),
        error_code=None, failure_type=None, attempts=1,
    )

    report = verify_evidence_provenance(RequestContext(message="Request."), (result,))

    assert len(report) == 1
    assert report[0].outcome == "unverified"


def test_mock_mode_returns_stable_not_evaluated_outcomes():
    result = _analysis_result("analysis", "Summary", ("reference phrase",))
    context = RequestContext(message="reference phrase")

    first = verify_evidence_provenance(context, (result,), enabled=False)
    second = verify_evidence_provenance(context, (result,), enabled=False)

    assert first == second
    assert [item.outcome for item in first] == ["not_evaluated"]
    assert first[0].source_kind is None


def test_malformed_structured_evidence_is_handled_as_unverified():
    validation = ValidationExpertOutput.model_construct(
        summary="Summary",
        assessment_status="completed",
        findings=(ValidationFinding.model_construct(
            claim="A claim",
            verdict="uncertain",
            rationale="Reason",
            evidence=(object(),),
        ),),
        assumptions=(),
        missing_information=(),
        recommendations=(),
    )
    result = ExpertResult.model_construct(
        step_id="validation",
        expert_id="validation",
        persona="ValidationExpert",
        status="succeeded",
        output=ExpertOutput.model_construct(
            content="Summary", evidence=(), confidence=0.5, advisory_only=True,
            analysis=None, validation=validation,
        ),
        error_code=None,
        failure_type=None,
        attempts=1,
    )

    report = verify_evidence_provenance(RequestContext(message="Input"), (result,))

    assert len(report) == 1
    assert report[0].outcome == "unverified"


def test_failed_expert_is_excluded_and_successful_partial_output_still_verifies():
    successful = _analysis_result("analysis", "Summary", ("evidence in request",))
    failed = ExpertResult(
        step_id="failed",
        expert_id="validation",
        persona="ValidationExpert",
        status="failed",
        error_code="expert_failure",
        attempts=1,
    )

    report = verify_evidence_provenance(
        RequestContext(message="This contains evidence in request."), (successful, failed)
    )

    assert len(report) == 1
    assert report[0].expert_id == "analysis"
    assert report[0].outcome == "source_match"


def test_verification_work_budget_fails_closed_to_unverified(monkeypatch):
    import jarvis.orchestration.verification as verification

    monkeypatch.setattr(verification, "MAX_COMPARISON_CHARACTERS", 1)
    result = _analysis_result("analysis", "Summary", ("long evidence",))
    report = verify_evidence_provenance(
        RequestContext(message="A sufficiently long request source."), (result,)
    )

    assert report[0].outcome == "unverified"


def test_request_budget_exhaustion_does_not_fall_through_to_matching_peer(monkeypatch):
    import jarvis.orchestration.verification as verification

    peer = _analysis_result("analysis", "Peer says needle.", ())
    target = _analysis_result("validation", "Target summary", ("needle",))
    # Request + evidence normalization consume 24 characters. Five remain,
    # fewer than the six-character request comparison, though the peer matches.
    monkeypatch.setattr(verification, "MAX_COMPARISON_CHARACTERS", 29)

    report = verify_evidence_provenance(
        RequestContext(message="needle"), (peer, target)
    )

    assert report[-1].outcome == "unverified"
    assert report[-1].source_kind is None
    assert report[-1].source_expert_id is None


def test_repeated_expert_ids_are_excluded_as_ambiguous_peer_sources():
    peer = _analysis_result("analysis", "Peer contains duplicate-id phrase.", (), step_id="peer-step")
    target = _analysis_result("analysis", "Different step.", ("duplicate-id phrase",), step_id="target-step")

    report = verify_evidence_provenance(RequestContext(message="Unrelated."), (peer, target))

    assert report[-1].outcome == "unverified"
    assert report[-1].source_expert_id is None


def test_checker_does_not_normalize_sources_when_no_evidence_targets(monkeypatch):
    import jarvis.orchestration.verification as verification

    monkeypatch.setattr(verification, "_normalize", lambda _value: (_ for _ in ()).throw(AssertionError()))
    result = _analysis_result("analysis", "Summary", ())

    assert verify_evidence_provenance(RequestContext(message="Request."), (result,)) == ()


def test_direct_checker_bounds_model_constructed_evidence_collections():
    result = ExpertResult.model_construct(
        step_id="analysis", expert_id="analysis", persona="AnalysisExpert", status="succeeded",
        output=ExpertOutput.model_construct(
            content="Summary", evidence=["untrusted"] * 100_000, confidence=0.5,
            advisory_only=True, analysis=None, validation=None,
        ),
        error_code=None, failure_type=None, attempts=1,
    )

    assert verify_evidence_provenance(RequestContext(message="Request."), (result,)) == ()


def test_fallback_omits_unrepresentable_model_construct_identifiers_safely():
    from jarvis.orchestration.verification import not_evaluated_evidence_provenance

    result = ExpertResult.model_construct(
        step_id=None, expert_id=object(), persona="KnowledgeExpert", status="succeeded",
        output=ExpertOutput.model_construct(
            content="Summary", evidence=("evidence",), confidence=0.5, advisory_only=True,
            analysis=None, validation=None,
        ),
        error_code=None, failure_type=None, attempts=1,
    )

    assert not_evaluated_evidence_provenance((result,)) == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"outcome": "source_match"},
        {"outcome": "source_match", "source_kind": "expert_output"},
        {"outcome": "source_match", "source_kind": "user_request", "source_expert_id": "analysis"},
        {"outcome": "unverified", "source_kind": "user_request"},
        {"outcome": "not_evaluated", "source_expert_id": "analysis"},
        {"outcome": "source_match", "source_kind": "unknown"},
    ],
)
def test_evidence_provenance_rejects_invalid_source_combinations(kwargs):
    with pytest.raises(ValidationError):
        EvidenceProvenance(step_id="step", expert_id="expert", evidence_index=0, **kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("step_id", ""), ("step_id", "has space"), ("step_id", "x" * 81),
        ("expert_id", ""), ("expert_id", "UPPER"), ("expert_id", "exämple"), ("expert_id", "x" * 81),
        ("source_expert_id", ""), ("source_expert_id", "bad id"), ("source_expert_id", "x" * 81),
    ],
)
def test_evidence_provenance_rejects_invalid_identifiers(field, value):
    kwargs = {"step_id": "step", "expert_id": "expert", "evidence_index": 0}
    kwargs[field] = value
    if field == "source_expert_id":
        kwargs.update(outcome="source_match", source_kind="expert_output")
    else:
        kwargs.update(outcome="unverified")
    with pytest.raises(ValidationError):
        EvidenceProvenance(**kwargs)


def test_overlong_evidence_is_unverified_without_normalizing_or_searching_it():
    import jarvis.orchestration.verification as verification

    result = _analysis_result(
        "analysis", "Summary", ("x" * (verification.MAX_EVIDENCE_TEXT_LENGTH + 1),)
    )
    report = verify_evidence_provenance(RequestContext(message="x" * 100), (result,))

    assert report[0].outcome == "unverified"


class _StructuredProvider:
    name = "test-structured"
    is_demo = False

    def __init__(self, replies):
        self.replies = iter(replies)

    async def generate_reply(self, message, *, system_instruction=None, response_model=None):
        return json.dumps(next(self.replies))


def _service(provider):
    return OrchestrationService(
        provider,
        Settings(environment="test"),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )


def test_service_records_private_safe_provenance_counts_and_preserves_reply(caplog):
    private_evidence = "PRIVATE-CLAIM-EVIDENCE-9182"
    payload = {
        "summary": "Analysis result",
        "assessment_status": "completed",
        "factors": [],
        "supported_conclusions": [
            {"conclusion": "The request includes a private reference.", "evidence": [private_evidence]},
            {"conclusion": "Another claim.", "evidence": ["not present in sources"]},
        ],
        "assumptions": [],
        "missing_information": [],
        "recommendations": [],
    }
    caplog.set_level(logging.INFO, logger="jarvis.orchestration")
    result = asyncio.run(_service(_StructuredProvider([payload])).run(
        RequestContext(message=f"Analyze this. {private_evidence}")
    ))

    record = next(record for record in caplog.records if record.getMessage() == "Orchestration completed")
    assert result.reply.startswith("Analysis result")
    assert [item.outcome for item in result.evidence_provenance] == ["source_match", "unverified"]
    assert record.source_match_count == 1
    assert record.unverified_count == 1
    assert record.not_evaluated_count == 0
    assert private_evidence not in caplog.text
    assert "not present in sources" not in caplog.text


def test_json_log_formatter_emits_only_bounded_provenance_counts():
    from jarvis.core.logging_config import JsonFormatter

    record = logging.LogRecord(
        "jarvis.orchestration.service", logging.INFO, __file__, 1,
        "Orchestration completed", (), None,
    )
    record.execution_id = "execution-123"
    record.source_match_count = 1
    record.unverified_count = 2
    record.not_evaluated_count = 0
    record.private_evidence = "SECRET-EVIDENCE-9981"
    payload = json.loads(JsonFormatter().format(record))

    assert payload["source_match_count"] == 1
    assert payload["unverified_count"] == 2
    assert payload["not_evaluated_count"] == 0
    assert "private_evidence" not in payload
    assert "SECRET-EVIDENCE-9981" not in json.dumps(payload)


def test_verification_failure_does_not_discard_successful_or_partial_results(monkeypatch, caplog):
    import jarvis.orchestration.service as service_module

    def fail_verification(*args, **kwargs):
        raise RuntimeError("private evidence and request")

    monkeypatch.setattr(service_module, "verify_evidence_provenance", fail_verification)
    caplog.set_level(logging.WARNING, logger="jarvis.orchestration")
    payload = {
        "summary": "Useful response",
        "assessment_status": "completed",
        "factors": [],
        "supported_conclusions": [
            {"conclusion": "A claim.", "evidence": ["unmatched private evidence"]},
        ],
        "assumptions": [],
        "missing_information": [],
        "recommendations": [],
    }
    result = asyncio.run(_service(_StructuredProvider([payload])).run(
        RequestContext(message="Analyze private request text")
    ))

    assert result.reply.startswith("Useful response")
    assert result.partial is False
    assert [item.outcome for item in result.evidence_provenance] == ["not_evaluated"]
    assert "private evidence and request" not in caplog.text
    assert "unmatched private evidence" not in caplog.text
    assert "Analyze private request text" not in caplog.text


def test_fallback_failure_does_not_discard_synthesized_reply(monkeypatch):
    import jarvis.orchestration.service as service_module

    monkeypatch.setattr(service_module, "verify_evidence_provenance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(service_module, "not_evaluated_evidence_provenance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    payload = {
        "summary": "Useful response despite malformed internal diagnostics",
        "assessment_status": "completed", "factors": [],
        "supported_conclusions": [{"conclusion": "Claim", "evidence": ["private evidence"]}],
        "assumptions": [], "missing_information": [], "recommendations": [],
    }

    result = asyncio.run(_service(_StructuredProvider([payload])).run(
        RequestContext(message="Analyze this request")
    ))

    assert result.reply.startswith("Useful response despite malformed")
    assert result.evidence_provenance == ()
