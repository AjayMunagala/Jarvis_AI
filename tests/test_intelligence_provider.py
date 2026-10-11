import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from fastapi.testclient import TestClient

from jarvis.core.config import Settings
from jarvis.intelligence.base import ProviderError, TransientProviderError
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.intelligence.openai_provider import OpenAIProvider
from jarvis.intelligence.router import build_provider
from jarvis.main import create_app
from jarvis.orchestration.models import RequestContext
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
    assert captured["body"]["max_completion_tokens"] == 1024
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "Check claims."},
        {"role": "user", "content": "Review this"},
    ]


def test_chat_contract_identifies_model_provider_without_changing_response_shape():
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
    }
    assert len(response.headers["x-jarvis-execution-id"]) == 32


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
    {"choices": [{"message": {"content": "   "}}]},
])
def test_openai_adapter_rejects_malformed_or_blank_model_output(payload):
    provider = openai_provider(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate_reply("hello"))


def test_validation_expert_uses_a_distinct_system_instruction():
    class RecordingProvider:
        name = "recording"
        is_demo = True

        def __init__(self):
            self.instructions = []

        async def generate_reply(self, message, *, system_instruction=None):
            self.instructions.append(system_instruction)
            return "Validation result."

    provider = RecordingProvider()
    service = OrchestrationService(
        provider,
        Settings(),
        expert_registry=ExpertRegistry.load(),
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )
    result = asyncio.run(service.run(RequestContext(message="Validate this claim")))
    assert result.reply == "Validation result."
    assert len(provider.instructions) == 1
    assert "cautious validation reviewer" in provider.instructions[0]
