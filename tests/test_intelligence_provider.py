import asyncio
import copy
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from fastapi.testclient import TestClient

from jarvis.core.config import Settings
from jarvis.intelligence.base import ProviderError, TransientProviderError
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.intelligence.openai_provider import (
    MAX_COMPLETION_TOKENS, MAX_STRUCTURED_COMPLETION_TOKENS,
    OpenAIProvider, _copy_for_strict_outputs, _strict_response_schema,
    _validate_strict_schema,
)
from jarvis.intelligence.router import build_provider
from jarvis.main import create_app
from jarvis.orchestration.models import (
    AnalysisExpertOutput, RequestContext, ValidationExpertOutput,
)
from jarvis.orchestration.registry import ExpertRegistry, ToolRegistry, ToolRegistryConfig
from jarvis.orchestration.service import OrchestrationService


def openai_provider(transport: httpx.AsyncBaseTransport) -> OpenAIProvider:
    settings = Settings(provider="openai", openai_api_key=SecretStr("test-fixture-only"))
    return OpenAIProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        connect_timeout_seconds=settings.provider_connect_timeout_seconds,
        read_timeout_seconds=settings.provider_read_timeout_seconds,
        transport=transport,
    )


def test_mock_remains_default_and_needs_no_provider_credentials():
    settings = Settings()
    provider = build_provider(settings.provider, settings=settings)
    assert settings.provider == "mock"
    assert isinstance(provider, MockProvider)
    assert provider.is_demo is True


def test_openai_selection_requires_api_key_and_does_not_fall_back_to_mock():
    with pytest.raises(ValidationError, match="JARVIS_OPENAI_API_KEY is required"):
        Settings(provider="openai")
    with pytest.raises(ValueError, match="OpenAI provider configuration is required"):
        build_provider("openai")


def test_openai_configuration_loads_from_environment_without_exposing_key(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER", "openai")
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("JARVIS_OPENAI_MODEL", "test-model")
    settings = Settings(_env_file=None)
    assert settings.provider == "openai"
    assert settings.openai_model == "test-model"
    assert "unit-test-placeholder" not in repr(settings)


def test_openai_secret_is_masked_and_endpoint_must_use_https():
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    assert "unit-test-placeholder" not in repr(settings)
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            provider="openai",
            openai_api_key=SecretStr("unit-test-placeholder"),
            openai_base_url="http://provider.invalid/v1",
        )
    with pytest.raises(ValidationError, match="must not contain credentials"):
        Settings(
            provider="openai",
            openai_api_key=SecretStr("unit-test-placeholder"),
            openai_base_url="https://user:pass@provider.invalid/v1",
        )


def test_openai_adapter_sends_typed_chat_request_and_validates_reply():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization_present"] = bool(request.headers.get("authorization"))
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Validated provider answer."}}]})

    provider = openai_provider(httpx.MockTransport(handler))
    result = asyncio.run(provider.generate_reply("Review this", system_instruction="Check claims."))
    assert result == "Validated provider answer."
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["authorization_present"]
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["max_completion_tokens"] == MAX_COMPLETION_TOKENS == 1024
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "Check claims."},
        {"role": "user", "content": "Review this"},
    ]


def test_chat_contract_identifies_model_provider_and_returns_null_id_without_persistence():
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "Model-backed reply."}}]})
    ))
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 200
    assert response.json() == {
        "reply": "Model-backed reply.",
        "provider": "openai",
        "demo": False,
        "partial": False,
        "conversation_id": None,
    }
    assert len(response.headers["x-jarvis-execution-id"]) == 32


def test_structured_specialist_chat_keeps_public_api_contract():
    structured = {
        "summary": "The proposal has two material trade-offs.",
        "assessment_status": "completed",
        "factors": [{"factor": "Cost", "implications": "The upfront cost is higher."}],
        "supported_conclusions": [],
        "assumptions": [],
        "missing_information": [],
        "recommendations": ["Compare total cost over the full term."],
    }
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(structured)}}]},
        )
    ))
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "Analyze the proposal"})
    assert response.status_code == 200
    assert set(response.json()) == {"reply", "provider", "demo", "partial", "conversation_id"}
    assert response.json()["conversation_id"] is None
    assert response.json()["reply"] == (
        "The proposal has two material trade-offs.\n\nRelevant factors:\n"
        "- Cost: The upfront cost is higher.\n\nRecommendations:\n"
        "- Compare total cost over the full term."
    )
    assert response.json()["provider"] == "openai"
    assert response.json()["demo"] is False
    assert response.headers["x-jarvis-execution-id"]


def test_malformed_structured_specialist_response_is_sanitized_at_api_boundary():
    raw = '{"summary":"private malformed output"'
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})
    ))
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "Analyze this claim"})
    assert response.status_code == 502
    assert response.json() == {"error": "provider_error", "detail": "The configured provider failed."}
    assert "private malformed output" not in response.text
    assert response.headers["x-jarvis-execution-id"]


def test_structured_output_still_enforces_local_bounds_removed_from_transmitted_schema():
    structured = {
        "summary": "Assessment.", "assessment_status": "completed",
        "factors": [{"factor": "x" * 121, "implications": "y"}],
        "supported_conclusions": [], "assumptions": [], "missing_information": [],
        "recommendations": [],
    }
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(structured)}}]})
    ))
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "Analyze the proposal"})
    assert response.status_code == 502
    assert response.json() == {"error": "provider_error", "detail": "The configured provider failed."}
    assert response.headers["x-jarvis-execution-id"]


def test_validation_finding_is_rendered_in_readable_chat_reply_without_internal_metadata(caplog):
    structured = {
        "summary": "The claim is supported by the supplied evidence.",
        "assessment_status": "completed",
        "findings": [{
            "claim": "The system retains no chat history.", "verdict": "supported",
            "rationale": "The implementation stores chat state only in memory.",
            "evidence": ["The handoff documents in-memory-only chat state."],
        }],
        "assumptions": ["The cited implementation description is current."],
        "missing_information": [], "recommendations": [],
    }
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(structured)}}]})
    ))
    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    caplog.set_level("INFO")
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "Validate claim marker PRIVATE_PROMPT_MARKER"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"reply", "provider", "demo", "partial", "conversation_id"}
    assert body["conversation_id"] is None
    assert "supported" in body["reply"]
    assert "The system retains no chat history." in body["reply"]
    assert "The implementation stores chat state only in memory." in body["reply"]
    assert "The handoff documents in-memory-only chat state." in body["reply"]
    assert "PRIVATE_PROMPT_MARKER" not in caplog.text
    assert "execution_id" not in body
    assert response.headers["x-jarvis-execution-id"]


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_openai_adapter_marks_rate_limit_and_server_errors_transient(status_code):
    provider = openai_provider(httpx.MockTransport(
        lambda request: httpx.Response(status_code, json={"error": {"message": "sensitive provider detail"}})
    ))
    with pytest.raises(TransientProviderError) as error:
        asyncio.run(provider.generate_reply("hello"))
    assert "sensitive provider detail" not in str(error.value)


def test_openai_adapter_does_not_retry_or_expose_authentication_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "sensitive provider detail"}})

    provider = openai_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.generate_reply("hello"))
    assert calls == 1
    assert "sensitive provider detail" not in str(error.value)


def test_chat_does_not_fall_back_to_mock_after_openai_authentication_failure():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "sensitive provider detail"}})

    provider = openai_provider(httpx.MockTransport(handler))
    settings = Settings(
        provider="openai",
        openai_api_key=SecretStr("unit-test-placeholder"),
        max_expert_retries=3,
    )
    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 502
    assert response.json() == {"error": "provider_error", "detail": "The configured provider failed."}
    assert response.headers["x-jarvis-execution-id"]
    assert calls == 1
    assert "sensitive provider detail" not in response.text


def test_openai_adapter_maps_transport_timeouts_to_sanitized_transient_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive transport detail", request=request)

    provider = openai_provider(httpx.MockTransport(handler))
    with pytest.raises(TransientProviderError) as error:
        asyncio.run(provider.generate_reply("hello"))
    assert "sensitive transport detail" not in str(error.value)


def test_transient_openai_error_retries_within_the_orchestrator_boundary():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Recovered response."}}]})

    config = Settings(max_expert_retries=1)
    provider = OpenAIProvider(
        api_key=SecretStr("unit-test-placeholder"),
        base_url=config.openai_base_url,
        model=config.openai_model,
        connect_timeout_seconds=config.provider_connect_timeout_seconds,
        read_timeout_seconds=config.provider_read_timeout_seconds,
        transport=httpx.MockTransport(handler),
    )
    service = OrchestrationService(
        provider,
        config,
        expert_registry=ExpertRegistry.load(),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )
    result = asyncio.run(service.run(RequestContext(message="hello")))
    assert result.reply == "Recovered response."
    assert calls == 2
    assert result.expert_results[0].attempts == 2


@pytest.mark.parametrize("payload", [
    {"choices": []},
    {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": "   "}}]},
])
def test_openai_adapter_rejects_malformed_or_blank_model_output(payload):
    provider = openai_provider(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate_reply("hello"))


@pytest.mark.parametrize(
    ("message", "expert_id", "persona", "instruction_marker"),
    [
        ("Analyze this claim", "analysis", "AnalysisExpert", "relevant factors"),
        ("Validate this claim", "validation", "ValidationExpert", "internally consistent"),
    ],
)
def test_analysis_and_validation_experts_send_distinct_instructions(
    message, expert_id, persona, instruction_marker,
):
    class RecordingProvider:
        name = "recording"
        is_demo = True

        def __init__(self):
            self.requests = []

        async def generate_reply(self, message, *, system_instruction=None):
            self.requests.append((message, system_instruction))
            return "Specialist advisory result."

    provider = RecordingProvider()
    service = OrchestrationService(
        provider,
        Settings(),
        expert_registry=ExpertRegistry.load(),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )
    context = RequestContext(message=message)
    result = asyncio.run(service.run(context))
    assert result.reply == "Specialist advisory result."
    definitions = {item.id: item for item in service.expert_registry.config.experts}
    assert provider.requests == [(message, definitions[expert_id].system_instruction)]
    instruction = provider.requests[0][1]
    assert instruction is not None
    assert instruction_marker in instruction
    assert result.execution_id == context.execution_id
    assert len(result.expert_results) == 1
    assert result.expert_results[0].expert_id == expert_id
    assert result.expert_results[0].persona == persona
    assert result.expert_results[0].status == "succeeded"


def test_analysis_and_validation_instructions_are_meaningfully_distinct():
    registry = ExpertRegistry.load()
    definitions = {expert.id: expert for expert in registry.config.experts}
    analysis = definitions["analysis"].system_instruction
    validation = definitions["validation"].system_instruction
    assert analysis and validation and analysis != validation
    assert "trade-offs" in analysis
    assert "missing information" in validation


def test_configured_specialist_instructions_reach_openai_request_payloads():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        if payload["response_format"]["json_schema"]["name"] == "analysisexpertoutput":
            structured = {
                "summary": "Analysis summary.", "assessment_status": "completed",
                "factors": [], "supported_conclusions": [], "assumptions": [],
                "missing_information": [], "recommendations": [],
            }
        else:
            structured = {
                "summary": "Validation summary.", "assessment_status": "completed",
                "findings": [], "assumptions": [], "missing_information": [],
                "recommendations": [],
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(structured)}}]})

    settings = Settings(provider="openai", openai_api_key=SecretStr("unit-test-placeholder"))
    provider = OpenAIProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        connect_timeout_seconds=settings.provider_connect_timeout_seconds,
        read_timeout_seconds=settings.provider_read_timeout_seconds,
        transport=httpx.MockTransport(handler),
    )
    service = OrchestrationService(
        provider,
        settings,
        expert_registry=ExpertRegistry.load(),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )
    results = [
        asyncio.run(service.run(RequestContext(message=message)))
        for message in ("Analyze the proposal", "Validate the proposal")
    ]
    prompts = [payload["messages"][0]["content"] for payload in captured]
    assert len(captured) == 2
    assert prompts[0] != prompts[1]
    assert "relevant factors" in prompts[0]
    assert "internally consistent" in prompts[1]
    assert [result.expert_results[0].expert_id for result in results] == ["analysis", "validation"]
    assert [result.reply for result in results] == ["Analysis summary.", "Validation summary."]
    for payload in captured:
        schema = payload["response_format"]["json_schema"]
        assert payload["response_format"]["type"] == "json_schema"
        assert schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert payload["max_completion_tokens"] == MAX_STRUCTURED_COMPLETION_TOKENS == 8192
        assert "AnalysisFactor" in schema["schema"]["$defs"] or "ValidationFinding" in schema["schema"]["$defs"]
        assert schema["schema"]["properties"]["assessment_status"]["enum"] == [
            "completed", "limited", "not_evaluated",
        ]
        schema_nodes = list(_all_dicts(schema["schema"]))
        assert not any(
            {"minLength", "maxLength", "minItems", "maxItems", "allOf", "not", "if", "then", "else"}
            .intersection(node)
            for node in schema_nodes
        )
        assert all(
            set(node["required"]) == set(node["properties"])
            and node["additionalProperties"] is False
            for node in schema_nodes if node.get("type") == "object"
        )


def _all_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _all_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_dicts(child)


def test_strict_schema_transform_is_recursive_and_preserves_property_names_and_structure():
    source = {
        "type": "object",
        "properties": {
            "maxLength": {"type": "string", "minLength": 2, "maxLength": 8},
            "nested": {"$ref": "#/$defs/nested"},
            "choice": {"anyOf": [{"type": "string", "maxLength": 4}, {"type": "string", "minLength": 1}]},
            "mode": {"type": "string", "enum": ["not", "maxItems"]},
        },
        "required": ["maxLength", "nested", "choice", "mode"],
        "additionalProperties": False,
        "$defs": {
            "nested": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    }
    transformed = _validate_strict_schema(_copy_for_strict_outputs(source))
    assert "maxLength" in transformed["properties"]
    assert transformed["properties"]["maxLength"] == {"type": "string"}
    assert transformed["properties"]["nested"]["$ref"] == "#/$defs/nested"
    assert transformed["properties"]["choice"] == {
        "anyOf": [{"type": "string"}, {"type": "string"}],
    }
    assert transformed["properties"]["mode"]["enum"] == ["not", "maxItems"]
    assert transformed["$defs"]["nested"]["properties"]["items"]["items"] == {"type": "string"}
    assert transformed["$defs"]["nested"]["properties"]["items"] == {
        "type": "array", "items": {"type": "string"},
    }
    assert source["properties"]["maxLength"]["maxLength"] == 8
    assert source["$defs"]["nested"]["properties"]["items"]["maxItems"] == 3


def test_pydantic_bounds_remain_unchanged_and_are_enforced_locally():
    original = AnalysisExpertOutput.model_json_schema()
    original_snapshot = copy.deepcopy(original)
    transmitted = _strict_response_schema(AnalysisExpertOutput)
    assert original == original_snapshot
    assert original["properties"]["summary"]["minLength"] == 1
    assert original["properties"]["summary"]["maxLength"] == 40000
    assert original["properties"]["factors"]["maxItems"] == 8
    assert transmitted["properties"]["summary"] == {
        "title": "Summary", "type": "string",
    }
    with pytest.raises(ValidationError):
        AnalysisExpertOutput.model_validate({
            "summary": "ok", "assessment_status": "completed",
            "factors": [{"factor": "x" * 121, "implications": "y"}],
            "supported_conclusions": [], "assumptions": [], "missing_information": [],
            "recommendations": [],
        })


def test_strict_schema_validator_rejects_untransformed_unsupported_features():
    invalid = {
        "type": "object", "properties": {"value": {"type": "string", "oneOf": []}},
        "required": ["value"], "additionalProperties": False,
    }
    with pytest.raises(ValueError, match="unsupported keyword"):
        _validate_strict_schema(invalid)


def test_provider_fails_sanitized_before_network_for_unrecognized_schema(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    unsupported = {
        "type": "object", "properties": {"value": {"type": "string", "oneOf": []}},
        "required": ["value"], "additionalProperties": False,
    }
    monkeypatch.setattr(AnalysisExpertOutput, "model_json_schema", classmethod(lambda cls, **kwargs: unsupported))
    provider = openai_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.generate_reply("review", response_model=AnalysisExpertOutput))
    assert str(error.value) == "The configured provider request failed."
    assert calls == 0


@pytest.mark.parametrize("response_model,payload", [
    (AnalysisExpertOutput, {
        "summary": "Assessment.", "assessment_status": "completed", "factors": [],
        "supported_conclusions": [], "assumptions": [], "missing_information": [], "recommendations": [],
    }),
    (ValidationExpertOutput, {
        "summary": "Assessment.", "assessment_status": "completed", "findings": [],
        "assumptions": [], "missing_information": [], "recommendations": [],
    }),
])
def test_openai_structured_output_uses_schema_and_returns_json(response_model, payload):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    provider = openai_provider(httpx.MockTransport(handler))
    result = asyncio.run(provider.generate_reply("review", response_model=response_model))
    assert json.loads(result) == payload
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True


def test_openai_structured_unsupported_schema_is_sanitized_and_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "private unsupported schema details"}})

    provider = openai_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.generate_reply("review", response_model=AnalysisExpertOutput))
    assert calls == 1
    assert "private unsupported schema details" not in str(error.value)


def test_openai_structured_truncation_is_sanitized_permanent_failure():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})

    provider = openai_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate_reply("review", response_model=AnalysisExpertOutput))
    assert calls == 1


@pytest.mark.parametrize("payload", [
    {"summary": " ", "assessment_status": "completed", "factors": [], "supported_conclusions": [], "assumptions": [], "missing_information": [], "recommendations": []},
    {"summary": "ok", "assessment_status": "unknown", "factors": [], "supported_conclusions": [], "assumptions": [], "missing_information": [], "recommendations": []},
    {"summary": "ok", "assessment_status": "completed", "factors": [{"factor": "x", "implications": "y"}], "supported_conclusions": [{"conclusion": "claim", "evidence": []}], "assumptions": [], "missing_information": [], "recommendations": []},
])
def test_analysis_output_contract_rejects_blank_invalid_or_unsupported_content(payload):
    with pytest.raises(ValidationError):
        AnalysisExpertOutput.model_validate(payload)


@pytest.mark.parametrize("payload", [
    {"summary": "ok", "assessment_status": "completed", "findings": [{"claim": "x", "verdict": "supported", "rationale": "y", "evidence": []}], "assumptions": [], "missing_information": [], "recommendations": []},
    {"summary": "ok", "assessment_status": "completed", "findings": [{"claim": "x", "verdict": "uncertain", "rationale": "y", "evidence": ["   "]}], "assumptions": [], "missing_information": [], "recommendations": []},
])
def test_validation_output_requires_evidence_for_certain_verdicts_and_nonblank_evidence(payload):
    with pytest.raises(ValidationError):
        ValidationExpertOutput.model_validate(payload)


def test_mock_analysis_contract_is_explicitly_not_evaluated_and_keeps_legacy_reply():
    service = OrchestrationService(
        MockProvider(), Settings(), expert_registry=ExpertRegistry.load(),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )
    context = RequestContext(message="Analyze this claim")
    result = asyncio.run(service.run(context))
    assert result.reply == "This is a JARVIS demo response, not an AI-generated answer. I received: Analyze this claim"
    expert_output = result.expert_results[0].output
    assert expert_output.analysis.assessment_status == "not_evaluated"
    assert expert_output.analysis.factors == ()


def test_mock_provider_remains_deterministic_and_ignores_specialist_instruction():
    provider = MockProvider()
    first = asyncio.run(provider.generate_reply("same request", system_instruction="Analyze trade-offs."))
    second = asyncio.run(provider.generate_reply("same request", system_instruction="Check completeness."))
    assert first == second
    assert "not an AI-generated answer" in first


@pytest.mark.parametrize("payload", [
    {"choices": [{"message": {"content": None, "refusal": "Sensitive refusal detail"}}]},
    {"choices": [{"message": {"content": "Unexpected text", "refusal": "Sensitive refusal detail"}}]},
    {"choices": [{"message": {"content": "Unexpected tool call"}, "finish_reason": "tool_calls"}]},
    {"choices": [{"message": {"content": "Filtered response"}, "finish_reason": "content_filter"}]},
])
def test_openai_adapter_rejects_refusal_without_exposing_provider_text(payload):
    provider = openai_provider(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.generate_reply("hello"))
    assert "Sensitive refusal detail" not in str(error.value)
