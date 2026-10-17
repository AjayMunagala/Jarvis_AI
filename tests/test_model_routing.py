"""v5.1 model registry, capability matching, routing, fallback and evaluation.

All adapters are deterministic in-process fakes; no live provider is contacted.
"""

import asyncio
import json
from datetime import date

import pytest
from pydantic import ValidationError

from jarvis.intelligence.mock_provider import MockProvider
from jarvis.routing.evaluation import EvaluationHarness, check, load_dataset
from jarvis.routing.mock import ScriptedAdapter
from jarvis.routing.models import Expectation, ModelDescriptor, Pricing, RegistryConfig, TaskRequirements
from jarvis.routing.registry import ModelRegistry, load_policies
from jarvis.routing.requirements import infer_requirements
from jarvis.routing.router import ModelRouter


def descriptor(key, provider, **overrides):
    values = dict(model_key=key, provider=provider, model_id=key, display_name=key, task_types=("general",),
                  input_modalities=("text",), locality="remote", capability_source="operator_declared",
                  structured_output=True)
    values.update(overrides)
    return ModelDescriptor(**values)


def registry(*models):
    return ModelRegistry(RegistryConfig(version=1, models=models))


FLEET = registry(
    descriptor("coder", "acme", task_types=("coding", "reasoning"), context_tokens=128_000),
    descriptor("generalist", "beta", context_tokens=32_000),
    descriptor("local-small", "local", locality="local", context_tokens=8_000, structured_output=False),
    descriptor("vision", "acme", task_types=("vision",), input_modalities=("text", "image"), context_tokens=64_000),
    descriptor("demo", "mock", locality="local", is_demo=True, structured_output=False),
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        def step():
            self.now += seconds
        return step


def router(adapters=None, **kwargs):
    adapters = adapters if adapters is not None else {
        name: ScriptedAdapter(name, lambda message, name=name: f"{name} reply")
        for name in ("acme", "beta", "local", "mock")}
    return ModelRouter(FLEET, load_policies(), adapters, **kwargs)


# --------------------------------------------------------------- registry

def test_packaged_registry_declares_only_known_capabilities():
    loaded = ModelRegistry.load(openai_model="example-model")
    models = {item.model_key: item for item in loaded.models()}
    assert set(models) == {"mock-deterministic", "openai-compatible-configured"}
    assert models["openai-compatible-configured"].model_id == "example-model"
    for item in models.values():
        assert item.pricing is None and item.context_tokens is None and item.input_modalities == ("text",)
        assert item.task_types == ("general",) and item.capability_source == "adapter_contract"
    assert models["mock-deterministic"].is_demo and models["mock-deterministic"].structured_output is False
    status = {item.model.model_key: item for item in loaded.status({"mock": MockProvider()})}
    assert status["mock-deterministic"].available is True
    assert status["openai-compatible-configured"].available is False
    assert "JARVIS_OPENAI_API_KEY" in status["openai-compatible-configured"].reason


@pytest.mark.parametrize("change", [
    {"input_modalities": ("image",)}, {"model_key": "Bad Key"}, {"context_tokens": 0}, {"task_types": ()},
    {"capability_source": "guess"}, {"extra": 1},
])
def test_registry_validation_rejects_invalid_descriptors(change):
    with pytest.raises(ValidationError):
        descriptor("x", "acme", **change)


def test_registry_rejects_duplicates_and_pricing_requires_source():
    with pytest.raises(ValidationError):
        RegistryConfig(version=1, models=(descriptor("a", "x"), descriptor("a", "y")))
    with pytest.raises(ValidationError):
        Pricing(input_per_million_tokens=1, output_per_million_tokens=2, source="", as_of=date(2026, 1, 1))


# --------------------------------------------------------------- matching

@pytest.mark.parametrize("text, task", [
    ("Debug this Python traceback in my function", "coding"),
    ("Classify these tickets by sentiment", "classification"),
    ("Extract the invoice fields as JSON", "structured_extraction"),
    ("Summarize the key points of the meeting", "summarization"),
    ("Translate this paragraph in French", "multilingual"),
    ("Compare the trade-offs of two designs", "reasoning"),
    ("hello there", "general"),
])
def test_capability_matching_is_deterministic(text, task):
    requirements, signals = infer_requirements(text)
    assert requirements.task_type == task
    assert (signals == ()) == (task == "general")


def test_matching_handles_images_long_context_and_structure():
    requirements, _ = infer_requirements("What is in this?", modalities=("image",))
    assert requirements.task_type == "vision" and requirements.modalities == ("text", "image")
    requirements, signals = infer_requirements("Summarize", estimated_input_tokens=50_000)
    assert requirements.task_type == "long_context_summarization" and requirements.min_context_tokens == 50_000
    assert infer_requirements("extract the dates")[0].needs_structured_output is True


# ---------------------------------------------------------------- routing

def test_specialized_model_wins_and_every_exclusion_is_explained():
    decision = router().decide(TaskRequirements(task_type="coding", needs_structured_output=True))
    assert decision.selected == "coder" and decision.ranked[0].factors["suitability"] == 1.0
    excluded = {item.model_key: item.reasons for item in decision.excluded}
    assert "local-small" in excluded and "structured output support is not declared" in excluded["local-small"]
    assert decision.fallbacks and decision.demo is False


def test_privacy_local_only_never_selects_remote_and_private_policy():
    for decision in (router().decide(TaskRequirements(privacy="local_only")),
                     router().decide(TaskRequirements(), policy="private")):
        assert decision.selected == "local-small"
        assert all(FLEET.get(key).locality == "local" for key in (decision.selected, *decision.fallbacks))


def test_modality_and_context_requirements_are_hard_filters():
    vision = router().decide(TaskRequirements(task_type="vision", modalities=("text", "image")))
    assert vision.selected == "vision" and vision.fallbacks == ()
    big = router().decide(TaskRequirements(task_type="coding", min_context_tokens=100_000))
    assert big.selected == "coder"
    reasons = {item.model_key: item.reasons for item in big.excluded}
    assert "context capacity is unknown" in reasons["demo"]
    none = router().decide(TaskRequirements(min_context_tokens=1_000_000))
    assert none.selected is None and "No registered model" in none.explanation


def test_demo_is_last_resort_and_can_be_disallowed():
    only_demo = router(adapters={"mock": MockProvider()})
    decision = only_demo.decide(TaskRequirements())
    assert decision.selected == "demo" and decision.demo is True and "not AI-generated" in decision.explanation
    strict = load_policies().policies[0].model_copy(update={"allow_demo": False})
    assert only_demo.decide(TaskRequirements(), policy=strict).selected is None
    ranked = router().decide(TaskRequirements()).ranked
    assert ranked[-1].model_key == "demo"


def test_preferences_and_disabled_models():
    policy = load_policies().policies[0].model_copy(update={"preferred_providers": ("beta",)})
    assert router().decide(TaskRequirements(), policy=policy).selected == "generalist"
    assert router(disabled=frozenset({"coder"})).decide(TaskRequirements(task_type="coding")).selected != "coder"
    with pytest.raises(KeyError):
        router().decide(TaskRequirements(), policy="nonexistent")


def test_evidence_changes_ranking_only_when_explicitly_accepted_and_never_from_simulation():
    clock = Clock()
    adapters = {"acme": ScriptedAdapter("acme", lambda m: "I do not know.", advance=clock.advance(2.0)),
                "beta": ScriptedAdapter("beta", lambda m: "391", advance=clock.advance(0.01)),
                "local": ScriptedAdapter("local", lambda m: "x"), "mock": MockProvider()}
    routing = router(adapters=adapters)
    before = routing.decide(TaskRequirements(task_type="reasoning")).selected
    dataset, digest = load_dataset("routing_baseline")
    single = dataset.model_copy(update={"cases": dataset.cases[:1]})
    report = asyncio.run(EvaluationHarness(FLEET, adapters, clock=clock).run(single, digest, ("coder", "generalist")))
    assert routing.accept_evidence(report) == ()  # scripted adapters are simulated
    assert routing.decide(TaskRequirements(task_type="reasoning")).selected == before == "coder"
    assert routing.accept_evidence(report, allow_simulated=True) == ("coder", "generalist")
    assert routing.decide(TaskRequirements(task_type="reasoning")).selected == "generalist"
    assert load_policies() == routing.policies  # policies themselves never change
    slow = routing.decide(TaskRequirements(task_type="reasoning", max_latency_ms=500))
    assert "coder" in {item.model_key for item in slow.excluded}


# --------------------------------------------------------------- fallback

def test_fallback_is_bounded_and_stays_within_eligible_models():
    adapters = {"acme": ScriptedAdapter("acme", fail_with="transient"), "beta": ScriptedAdapter("beta", lambda m: "ok"),
                "local": ScriptedAdapter("local", fail_with="error"), "mock": MockProvider()}
    routing = router(adapters=adapters)
    decision = routing.decide(TaskRequirements(task_type="coding"))
    assert (decision.selected, *decision.fallbacks) == ("coder", "local-small", "generalist")
    reply = asyncio.run(routing.execute(decision, "fix this bug"))
    assert reply.status == "succeeded" and reply.model_key == "generalist"
    assert [item.error_code for item in reply.attempts] == ["transient_provider_error", "provider_error", None]
    adapters["local"].fail_with = None
    private = routing.decide(TaskRequirements(privacy="local_only"))
    adapters["local"].fail_with = "crash"
    failed = asyncio.run(routing.execute(private, "secret prompt text"))
    # Only local models are tried; the local demo provider is the last resort.
    assert all(FLEET.get(a.model_key).locality == "local" for a in failed.attempts)
    assert failed.attempts[0].error_code == "adapter_error" and failed.model_key == "demo" and failed.demo is True
    assert "sk-test" not in failed.model_dump_json()
    assert not adapters["acme"].calls[1:]  # remote adapters never called for the local-only request


def test_max_fallbacks_and_timeouts():
    async def slow(message, **kwargs):
        await asyncio.sleep(1)
        return "late"

    adapters = {name: ScriptedAdapter(name, fail_with="transient") for name in ("acme", "beta", "local")}
    adapters["mock"] = MockProvider()
    adapters["acme"].generate_reply = slow
    policy = load_policies().policies[0].model_copy(update={"max_fallbacks": 1})
    routing = router(adapters=adapters, attempt_timeout_seconds=0.05)
    decision = routing.decide(TaskRequirements(task_type="coding"), policy=policy)
    reply = asyncio.run(routing.execute(decision, "x"))
    assert reply.status == "failed" and len(reply.attempts) == 2 and reply.attempts[0].outcome == "timeout"


def test_no_eligible_model_and_observability_records_no_text():
    routing = router()
    decision = routing.decide(TaskRequirements(min_context_tokens=9_000_000))
    reply = asyncio.run(routing.execute(decision, "my private prompt"))
    assert reply.status == "no_eligible_model" and reply.attempts == ()
    ok = asyncio.run(routing.execute(routing.decide(TaskRequirements()), "another private prompt"))
    assert ok.status == "succeeded"
    records = routing.recent()
    assert len(records) == 2 and records[0].prompt_chars == len("another private prompt")
    dumped = json.dumps([item.model_dump() for item in records])
    assert "private prompt" not in dumped and "reply" not in dumped and len(records[0].prompt_sha256) == 64


def test_structured_schema_only_sent_to_models_declaring_support():
    from pydantic import BaseModel

    class Out(BaseModel):
        value: int

    adapters = {"acme": ScriptedAdapter("acme", fail_with="error"), "beta": ScriptedAdapter("beta", fail_with="error"),
                "local": ScriptedAdapter("local", lambda m: "plain"), "mock": MockProvider()}
    routing = router(adapters=adapters)
    asyncio.run(routing.execute(routing.decide(TaskRequirements(task_type="coding")), "x", response_model=Out))
    assert adapters["acme"].calls[0]["structured"] is True
    assert adapters["local"].calls[0]["structured"] is False


# ------------------------------------------------------------- evaluation

def test_expectation_checks():
    assert check(Expectation(kind="number", number=391), "The answer is 391.") == (True, None)
    assert check(Expectation(kind="exact", value="ok"), " ok ") == (True, None)
    fields = Expectation(kind="json_fields", fields={"port": "integer"}, field_values={"port": 8173})
    assert check(fields, '{"port": 8173}') == (True, True)
    assert check(fields, '{"port": "8173"}') == (False, False)
    assert check(fields, "not json") == (False, False)
    assert check(Expectation(kind="json_fields", fields={"flag": "integer"}), '{"flag": true}') == (False, False)
    with pytest.raises(ValidationError):
        Expectation(kind="number")


def test_evaluation_is_repeatable_and_reports_honestly():
    clock = Clock()
    answers = {
        "What is 17 multiplied by 23? Reply with the number only.": "391",
        "Classify the sentiment of 'The build finally passed, great work!' as positive, negative or neutral. Reply with one word.": "Positive",
        "In Python, which built-in function returns the number of items in a list? Reply with the function name.": "len",
    }
    good = ScriptedAdapter("acme", lambda m: answers.get(m, '{"service": "api", "port": 8173}'), advance=clock.advance(0.2))
    adapters = {"acme": good, "beta": ScriptedAdapter("beta", fail_with="transient"), "mock": MockProvider()}
    dataset, digest = load_dataset("routing_baseline")
    harness = EvaluationHarness(FLEET, adapters, clock=clock)
    first = asyncio.run(harness.run(dataset, digest, ("coder", "generalist", "demo"), repeats=2))
    second = asyncio.run(harness.run(dataset, digest, ("coder", "generalist", "demo"), repeats=2))
    strip = lambda report: [item.model_dump(exclude={"report_id"}) for item in report.summaries]  # noqa: E731
    assert strip(first) == strip(second)
    summary = {item.model_key: item for item in first.summaries}
    assert summary["coder"].runs == 10 and summary["coder"].correctness_rate == 0.8  # summary-1 answer is wrong
    assert summary["coder"].structured_validity_rate == 1.0 and summary["coder"].p50_latency_ms == 200.0
    assert summary["generalist"].failure_rate == 1.0 and summary["generalist"].p50_latency_ms is None
    assert summary["demo"].simulated is True and summary["demo"].correctness_rate == 0.0
    assert all(item.estimated_cost_usd is None and "Not measurable" in item.cost_note for item in first.summaries)
    assert any("Simulated" in note for note in first.notes) and len(first.dataset_sha256) == 64


def test_evaluation_cost_uses_operator_pricing_and_limits_inputs():
    priced = registry(descriptor("priced", "acme", pricing=Pricing(
        input_per_million_tokens=1, output_per_million_tokens=3, source="operator invoice", as_of=date(2026, 9, 1))))
    dataset, digest = load_dataset("routing_baseline")
    harness = EvaluationHarness(priced, {"acme": ScriptedAdapter("acme", lambda m: "391")})
    report = asyncio.run(harness.run(dataset, digest, ("priced",)))
    assert report.summaries[0].estimated_cost_usd > 0 and "operator invoice" in report.summaries[0].cost_note
    with pytest.raises(ValueError):
        asyncio.run(harness.run(dataset, digest, ("priced",), repeats=9))
    with pytest.raises(ValueError):
        asyncio.run(harness.run(dataset, digest, ("unknown",)))
