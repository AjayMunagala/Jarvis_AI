from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pydantic import ValidationError

from jarvis.core.config import Settings
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.orchestration.classification import RuleBasedIntentClassifier, classify_safely
from jarvis.orchestration.models import (
    ExecutionPlan,
    ExpertExecutionError,
    ExpertOutput,
    ExpertTimeoutError,
    IntentClassification,
    PlanStep,
    RequestContext,
    RequestDeadlineExceeded,
    TransientExpertError,
)
from jarvis.intelligence.base import TransientProviderError
from jarvis.orchestration.planner import Planner
from jarvis.orchestration.registry import (
    ExpertRegistry,
    ExpertRegistryConfig,
    ToolDefinition,
    ToolRegistry,
    ToolRegistryConfig,
)
from jarvis.orchestration.service import OrchestrationService


def configured_registry(*, parallel: bool = False) -> ExpertRegistry:
    config = ExpertRegistryConfig.model_validate({
        "version": 2,
        "experts": [
            {"id": "analysis", "persona": "AnalysisExpert", "description": "Analyzes requests.", "output_contract": "analysis_v1"},
            {"id": "validation", "persona": "ValidationExpert", "description": "Checks results.", "output_contract": "validation_v1"},
        ],
        "intent_experts": {
            "general_inquiry": ["analysis"],
            "recommendation": ["analysis"],
            "analysis": ["analysis", "validation"],
            "validation": ["validation"],
            "risk_assessment": ["analysis"],
            "compliance": ["analysis"],
            "architecture": ["analysis"],
        },
        "classification_rules": [
            {"intent": "recommendation", "phrases": ["recommend", "suggest", "what should i", "which should"]},
            {"intent": "validation", "phrases": ["validate", "verify", "check whether", "is this correct"]},
            {"intent": "risk_assessment", "phrases": ["risk", "threat", "danger", "what could go wrong"]},
            {"intent": "compliance", "phrases": ["compliance", "regulation", "policy requirement"]},
            {"intent": "architecture", "phrases": ["architecture", "design a system", "module boundary"]},
            {"intent": "analysis", "phrases": ["analyze", "analyse", "compare", "break down"]},
        ],
        "parallel_intents": ["analysis"] if parallel else [],
    })
    return ExpertRegistry(config)


class TrackingProvider:
    name = "tracking"
    is_demo = True

    def __init__(self, replies=None):
        self.replies = list(replies or ["structured mock answer"])
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def generate_reply(self, message: str, *, system_instruction: str | None = None):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                reply = reply()
            return reply
        finally:
            self.active -= 1


def service(provider=None, *, settings=None, classifier=None, registry=None):
    return OrchestrationService(
        provider or MockProvider(),
        settings or Settings(environment="test"),
        classifier=classifier,
        expert_registry=registry,
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )


def test_rule_classifier_routes_known_intents_and_general_fallback():
    classifier = RuleBasedIntentClassifier(ExpertRegistry.load().config.classification_rules)
    expected = {
        "recommendation": ("recommend", "suggest", "what should i", "which should"),
        "validation": ("validate", "verify", "check whether", "is this correct"),
        "risk_assessment": ("risk", "threat", "danger", "what could go wrong"),
        "compliance": ("compliance", "regulation", "policy requirement"),
        "architecture": ("architecture", "design a system", "module boundary"),
        "analysis": ("analyze", "analyse", "compare", "break down"),
    }
    for intent, phrases in expected.items():
        for phrase in phrases:
            assert classifier.classify(RequestContext(message=f"Please {phrase} this")).intent == intent
    assert classifier.classify(RequestContext(message="validate this and recommend an option")).intent == "recommendation"
    assert classifier.classify(RequestContext(message="Please verify whether the architecture has risk and compliance issues")).intent == "validation"
    assert classifier.classify(RequestContext(message="What is the danger here?")).intent == "risk_assessment"
    assert classifier.classify(RequestContext(message="Check the regulation requirements")).intent == "compliance"
    assert classifier.classify(RequestContext(message="Design a system module boundary")).intent == "architecture"
    assert classifier.classify(RequestContext(message="Break down the comparison")).intent == "analysis"
    result = classifier.classify(RequestContext(message="Hello there"))
    assert result.intent == "general_inquiry"
    assert result.confidence > 0


def test_classifier_exception_falls_back_to_general_inquiry_zero_confidence():
    class BrokenClassifier:
        def classify(self, context):
            raise RuntimeError("private classifier detail")

    result = classify_safely(BrokenClassifier(), RequestContext(message="hello"))
    assert result == IntentClassification(intent="general_inquiry", confidence=0.0, rationale_code="classifier_fallback")


def test_registry_loads_configured_personas_and_selects_by_intent():
    registry = ExpertRegistry.load()
    assert {item.persona for item in registry.config.experts} == {
        "RecommendationExpert", "AnalysisExpert", "ValidationExpert", "RiskExpert",
        "ComplianceExpert", "ArchitectureExpert", "KnowledgeExpert",
    }
    assert registry.select("general_inquiry")[0].id == "knowledge"
    assert registry.select("architecture")[0].persona == "ArchitectureExpert"
    assert [rule.intent for rule in registry.config.classification_rules] == [
        "recommendation", "validation", "risk_assessment", "compliance", "architecture", "analysis"
    ]
    assert registry.select("analysis")[0].output_contract == "analysis_v1"
    assert registry.select("validation")[0].output_contract == "validation_v1"


def test_registry_rejects_invalid_and_unavailable_expert_config(tmp_path):
    config_path = tmp_path / "experts.json"
    config_path.write_text(json.dumps({"version": 2, "experts": [], "intent_experts": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid expert registry"):
        ExpertRegistry.load(config_path)

    with pytest.raises(ValueError, match="unavailable expert"):
        ExpertRegistry(ExpertRegistryConfig.model_validate({
            "version": 2,
            "experts": [{"id": "analysis", "persona": "AnalysisExpert", "description": "Analyze."}],
            "intent_experts": {key: ["missing"] for key in (
                "general_inquiry", "recommendation", "analysis", "validation", "risk_assessment", "compliance", "architecture"
            )},
            "classification_rules": [
                {"intent": intent, "phrases": [f"phrase {intent}"]}
                for intent in ("recommendation", "validation", "risk_assessment", "compliance", "architecture", "analysis")
            ],
        }))


def test_registry_file_rejects_duplicate_json_keys(tmp_path):
    config_path = tmp_path / "experts.json"
    config_path.write_text('{"version": 2, "version": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        ExpertRegistry.load(config_path)


def test_registry_requires_classification_rules_and_rejects_malformed_phrases():
    data = ExpertRegistry.load().config.model_dump(mode="json")
    data.pop("classification_rules")
    with pytest.raises(ValidationError):
        ExpertRegistryConfig.model_validate(data)

    data = ExpertRegistry.load().config.model_dump(mode="json")
    data["classification_rules"][0]["phrases"][0] = "  "
    with pytest.raises(ValidationError):
        ExpertRegistryConfig.model_validate(data)


def test_registry_rejects_duplicate_expert_ids_and_classification_phrases():
    registry = configured_registry()
    data = registry.config.model_dump(mode="json")
    data["experts"].append(dict(data["experts"][0]))
    with pytest.raises(ValueError, match="identifiers must be unique"):
        ExpertRegistry(ExpertRegistryConfig.model_validate(data))

    data = registry.config.model_dump(mode="json")
    data["classification_rules"][1]["phrases"][0] = data["classification_rules"][0]["phrases"][0]
    with pytest.raises(ValueError, match="phrases must be unique"):
        ExpertRegistry(ExpertRegistryConfig.model_validate(data))


def test_registry_rejects_unknown_intent_references_and_duplicate_rule_intents():
    registry = configured_registry()
    data = registry.config.model_dump(mode="json")
    data["intent_experts"]["unknown_intent"] = ["analysis"]
    with pytest.raises(ValidationError):
        ExpertRegistryConfig.model_validate(data)

    data = registry.config.model_dump(mode="json")
    data["classification_rules"][1]["intent"] = data["classification_rules"][0]["intent"]
    with pytest.raises(ValueError, match="exactly one phrase rule"):
        ExpertRegistry(ExpertRegistryConfig.model_validate(data))


def test_registry_requires_compatible_allowlisted_output_contracts():
    registry = configured_registry()
    data = registry.config.model_dump(mode="json")
    data["experts"][0]["output_contract"] = "not_a_contract"
    with pytest.raises(ValidationError):
        ExpertRegistryConfig.model_validate(data)

    data = registry.config.model_dump(mode="json")
    data["experts"][0].pop("output_contract")
    with pytest.raises(ValueError, match="requires output contract"):
        ExpertRegistry(ExpertRegistryConfig.model_validate(data))

    data = registry.config.model_dump(mode="json")
    data["experts"][0]["output_contract"] = "validation_v1"
    with pytest.raises(ValueError, match="incompatible with persona"):
        ExpertRegistry(ExpertRegistryConfig.model_validate(data))


def test_output_contract_resolution_uses_only_fixed_model_allowlist():
    from jarvis.orchestration.experts import OUTPUT_CONTRACTS, _resolve_output_contract
    from jarvis.orchestration.models import AnalysisExpertOutput, ValidationExpertOutput

    assert OUTPUT_CONTRACTS == {
        "analysis_v1": AnalysisExpertOutput,
        "validation_v1": ValidationExpertOutput,
    }
    assert _resolve_output_contract("analysis_v1") is AnalysisExpertOutput
    assert _resolve_output_contract("validation_v1") is ValidationExpertOutput
    assert _resolve_output_contract(None) is None


def test_expert_protocol_declares_narrow_supported_output_types():
    from typing import get_type_hints

    from jarvis.orchestration.experts import Expert
    from jarvis.orchestration.models import ExpertExecutionOutput

    assert get_type_hints(Expert.execute)["return"] is ExpertExecutionOutput


def test_planner_outputs_valid_sequential_registered_plan():
    registry = configured_registry()
    context = RequestContext(message="compare these options")
    intent = IntentClassification(intent="analysis", confidence=0.8)
    plan = Planner().create_plan(context, intent, registry)
    assert plan.execution_id == context.execution_id
    assert [step.expert_id for step in plan.steps] == ["analysis", "validation"]
    assert [step.round_number for step in plan.steps] == [1, 2]
    assert plan.allow_parallel is False
    assert plan.max_concurrency == 1
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate({**plan.model_dump(), "max_rounds": 6})


def test_planner_stays_within_five_rounds_and_respects_a_smaller_budget():
    definitions = [
        {"id": f"expert_{i}", "persona": "AnalysisExpert", "description": "Analyzes.", "output_contract": "analysis_v1"}
        for i in range(5)
    ]
    ids = [definition["id"] for definition in definitions]
    registry = ExpertRegistry(ExpertRegistryConfig.model_validate({
        "version": 2,
        "experts": definitions,
        "intent_experts": {intent: ids for intent in (
            "general_inquiry", "recommendation", "analysis", "validation", "risk_assessment", "compliance", "architecture"
        )},
        "classification_rules": [
            {"intent": intent, "phrases": [f"phrase {intent}"]}
            for intent in ("recommendation", "validation", "risk_assessment", "compliance", "architecture", "analysis")
        ],
    }))
    planner = Planner()
    context = RequestContext(message="analyze")
    intent = IntentClassification(intent="analysis", confidence=0.8)
    plan = planner.create_plan(context, intent, registry, max_rounds=5)
    assert plan.max_rounds == 5
    assert len(plan.steps) == 5
    limited = planner.create_plan(context, intent, registry, max_rounds=2)
    assert limited.max_rounds == 2
    assert len(limited.steps) == 2


def test_explicit_parallel_plan_is_concurrency_bounded():
    registry = configured_registry(parallel=True)
    plan = Planner().create_plan(
        RequestContext(message="compare options"),
        IntentClassification(intent="analysis", confidence=0.8),
        registry,
        max_concurrency=1,
    )
    assert plan.allow_parallel is True
    assert plan.max_concurrency == 1
    provider = TrackingProvider(["one", "two"])
    result = asyncio.run(service(
        provider,
        settings=Settings(environment="test", max_parallel_experts=1),
        registry=registry,
    ).run(RequestContext(message="compare options")))
    assert result.reply == "one\n\ntwo"
    assert provider.max_active <= 1


def test_sequential_plan_does_not_overlap_expert_calls():
    provider = TrackingProvider(["first", "second"])
    result = asyncio.run(service(provider, registry=configured_registry()).run(RequestContext(message="compare options")))
    assert provider.calls == 2
    assert provider.max_active == 1
    assert result.reply == "first\n\nsecond"
    assert result.partial is False
    assert result.audit_events[-1].outcome == "succeeded"


def test_expert_retries_only_explicit_transient_failures_with_a_bound():
    provider = TrackingProvider([TransientExpertError(), "recovered"])
    result = asyncio.run(service(provider).run(RequestContext(message="hello")))
    assert provider.calls == 2
    assert result.expert_results[0].attempts == 2
    assert result.reply == "recovered"


def test_expert_retries_explicit_transient_provider_failures():
    provider = TrackingProvider([TransientProviderError(), "recovered"])
    result = asyncio.run(service(provider).run(RequestContext(message="hello")))
    assert provider.calls == 2
    assert result.reply == "recovered"


def test_transient_retry_backoff_is_exponential_and_bounded(monkeypatch):
    provider = TrackingProvider([TransientExpertError(), TransientExpertError(), TransientExpertError()])
    settings = Settings(environment="test", max_expert_retries=2)
    delays = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay, *args, **kwargs):
        if delay > 0:
            delays.append(delay)
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    with pytest.raises(ExpertExecutionError):
        asyncio.run(service(provider, settings=settings).run(RequestContext(message="hello")))
    assert provider.calls == 3
    assert delays == [0.05, 0.1]


def test_retry_backoff_is_clipped_to_remaining_request_deadline(monkeypatch):
    import jarvis.orchestration.experts as experts_module

    provider = TrackingProvider([TransientExpertError(), "recovered"])
    clock_values = iter([0.0, 0.0, 0.09, 0.09])
    monkeypatch.setattr(experts_module, "monotonic", lambda: next(clock_values))
    delays = []

    async def capture_sleep(delay):
        if delay > 0:
            delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    result = asyncio.run(service(
        provider,
        settings=Settings(environment="test", request_timeout_seconds=0.1, max_expert_retries=1),
    ).run(RequestContext(message="hello")))
    assert provider.calls == 2
    assert result.reply == "recovered"
    assert delays == pytest.approx([0.01])


def test_provider_timeout_is_reported_as_sanitized_orchestration_timeout():
    class SlowProvider:
        name = "slow"
        is_demo = True

        async def generate_reply(self, message, *, system_instruction=None):
            await asyncio.sleep(0.05)
            return "late"

    orchestrator = service(SlowProvider(), settings=Settings(environment="test", request_timeout_seconds=0.01))
    with pytest.raises(ExpertTimeoutError):
        asyncio.run(orchestrator.run(RequestContext(message="hello")))


def test_orchestration_forwards_the_absolute_request_deadline_to_expert_execution():
    orchestrator = service(TrackingProvider())
    captured = {}
    from time import monotonic
    deadline = monotonic() + 5
    delegate = orchestrator._executor

    class CapturingExecutor:
        async def execute_plan(self, plan, context, *, deadline=None):
            captured["deadline"] = deadline
            return await delegate.execute_plan(plan, context, deadline=deadline)

    orchestrator._executor = CapturingExecutor()
    asyncio.run(orchestrator.run(RequestContext(message="hello"), deadline=deadline))
    assert captured["deadline"] == deadline


def test_expired_request_deadline_is_rejected_before_expert_execution(monkeypatch):
    import jarvis.orchestration.service as service_module

    provider = TrackingProvider()
    orchestrator = service(provider)
    monkeypatch.setattr(service_module, "monotonic", lambda: 10.0)
    with pytest.raises(RequestDeadlineExceeded):
        asyncio.run(orchestrator.run(RequestContext(message="hello"), deadline=9.0))
    assert provider.calls == 0


def test_parallel_timeout_cancels_active_provider_coroutines():
    started = asyncio.Event()

    class WaitingProvider:
        name = "waiting"
        is_demo = True

        def __init__(self):
            self.calls = 0
            self.cancelled = 0

        async def generate_reply(self, message, *, system_instruction=None):
            self.calls += 1
            if self.calls == 2:
                started.set()
            await started.wait()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

    provider = WaitingProvider()
    orchestrator = service(
        provider,
        settings=Settings(environment="test", request_timeout_seconds=0.03, max_parallel_experts=2),
        registry=configured_registry(parallel=True),
    )
    with pytest.raises(ExpertTimeoutError):
        asyncio.run(orchestrator.run(RequestContext(message="compare options")))
    assert provider.calls == 2
    assert provider.cancelled == 2


def test_partial_results_survive_individual_expert_failure():
    provider = TrackingProvider(["useful result", RuntimeError("do not log this")])
    result = asyncio.run(service(provider, registry=configured_registry()).run(RequestContext(message="compare options")))
    assert result.reply == "useful result"
    assert result.partial is True
    assert [item.status for item in result.expert_results] == ["succeeded", "failed"]


def test_failed_dependency_is_skipped_while_independent_output_is_kept():
    from jarvis.orchestration.models import ExecutionPlan

    provider = TrackingProvider(["useful", RuntimeError("private detail"), "independent result"])
    registry = configured_registry()
    context = RequestContext(message="hello")
    plan = ExecutionPlan(
        execution_id=context.execution_id,
        goal=context.message,
        intent="general_inquiry",
        steps=(
            PlanStep(step_id="useful", expert_id="analysis", round_number=1, expected_evidence="a response"),
            PlanStep(step_id="failed_parent", expert_id="validation", round_number=1, expected_evidence="a check"),
            PlanStep(step_id="blocked_child", expert_id="validation", round_number=2, depends_on=("failed_parent",), expected_evidence="a dependent check"),
            PlanStep(step_id="independent", expert_id="validation", round_number=2, expected_evidence="an independent response"),
        ),
        max_rounds=2,
        stop_condition="Complete configured steps.",
    )
    orchestrator = service(provider, registry=registry)
    results, _ = asyncio.run(orchestrator._executor.execute_plan(plan, context))
    assert [item.status for item in results] == ["succeeded", "failed", "failed", "succeeded"]
    assert results[2].failure_type == "DependencyNotSatisfied"
    assert results[2].error_code == "expert_failure"
    assert provider.calls == 3


def test_unknown_expert_in_blocked_dependency_does_not_raise_key_error():
    from jarvis.orchestration.models import ExecutionPlan

    context = RequestContext(message="hello")
    plan = ExecutionPlan(
        execution_id=context.execution_id,
        goal=context.message,
        intent="general_inquiry",
        steps=(
            PlanStep(step_id="failed_parent", expert_id="analysis", round_number=1, expected_evidence="a response"),
            PlanStep(step_id="missing_child", expert_id="missing", round_number=2, depends_on=("failed_parent",), expected_evidence="a response"),
        ),
        max_rounds=2,
        stop_condition="Complete configured steps.",
    )
    orchestrator = service(TrackingProvider([RuntimeError("private detail")]))
    with pytest.raises(ExpertExecutionError):
        asyncio.run(orchestrator._executor.execute_plan(plan, context))


def test_malformed_provider_output_is_rejected_and_never_synthesized():
    with pytest.raises(ExpertExecutionError):
        asyncio.run(service(TrackingProvider([{"content": "spoofed"}])).run(RequestContext(message="hello")))
    with pytest.raises(ValidationError):
        ExpertOutput.model_validate({"content": "action", "advisory_only": False})


@pytest.mark.parametrize("content", ["   ", "\t\n  "])
def test_expert_output_rejects_empty_or_whitespace_only_content(content):
    with pytest.raises(ValidationError):
        ExpertOutput.model_validate({"content": content})


def test_expert_output_preserves_valid_content():
    output = ExpertOutput.model_validate({"content": "  useful text\n"})
    assert output.content == "  useful text\n"


def test_synthesized_expert_output_is_truncated_with_clear_marker():
    from jarvis.orchestration.models import MAX_SYNTHESIZED_REPLY_LENGTH

    provider = TrackingProvider(["a" * 25000, "b" * 25000])
    result = asyncio.run(service(
        provider,
        registry=configured_registry(),
    ).run(RequestContext(message="Please analyze this")))
    assert len(result.reply) <= MAX_SYNTHESIZED_REPLY_LENGTH
    assert result.reply.startswith("a" * 25000 + "\n\n")
    assert result.reply.endswith("[Response truncated because combined expert output exceeded the 40,000-character limit.]")
    assert result.partial is False


def test_expert_failure_logs_and_audit_metadata_never_include_private_text(caplog):
    marker = "private-user-message-and-secret"
    caplog.set_level(logging.WARNING, logger="jarvis.orchestration")
    with pytest.raises(ExpertExecutionError):
        asyncio.run(service(TrackingProvider([RuntimeError(marker)])).run(RequestContext(message=marker)))
    assert marker not in caplog.text


def test_execution_logs_trace_metadata_without_request_or_provider_content(caplog):
    marker = "do-not-log-this-user-message"
    caplog.set_level(logging.INFO, logger="jarvis.orchestration")
    result = asyncio.run(service(TrackingProvider(["private provider output"])).run(RequestContext(message=marker)))
    record = next(record for record in caplog.records if record.getMessage() == "Orchestration completed")
    assert record.execution_id == result.execution_id
    assert record.intent == "general_inquiry"
    assert record.expert_outcomes == [{"expert_id": "knowledge", "status": "succeeded", "attempts": 1, "error_code": None}]
    assert marker not in caplog.text
    assert "private provider output" not in caplog.text


def test_tool_registry_is_data_only_and_rejects_state_changing_tools():
    empty = ToolRegistry.load()
    assert empty.execution_enabled is False
    assert empty.get("anything") is None
    assert not hasattr(empty, "execute")
    tool = ToolDefinition(
        id="unsafe", version="1", description="Unsafe", read_only=False, executable=True,
        risk="high", input_schema={"type": "object"}, output_schema={"type": "object"},
    )
    with pytest.raises(ValueError, match="read-only"):
        ToolRegistry(ToolRegistryConfig(version=1, tools=(tool,)))


def test_tool_registry_rejects_read_only_tool_with_non_low_risk():
    tool = ToolDefinition(
        id="medium_risk_read", version="1", description="Read-only but elevated risk",
        read_only=True, executable=False, risk="medium",
        input_schema={"type": "object"}, output_schema={"type": "object"},
    )
    with pytest.raises(ValueError, match="low-risk read-only"):
        ToolRegistry(ToolRegistryConfig(version=1, tools=(tool,)))


def test_settings_bound_expert_retries_rounds_and_parallelism():
    with pytest.raises(ValidationError):
        Settings(max_tool_rounds=6)
    with pytest.raises(ValidationError):
        Settings(max_expert_retries=4)
    with pytest.raises(ValidationError):
        Settings(max_parallel_experts=4)
