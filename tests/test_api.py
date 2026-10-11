import io
import logging

from fastapi.testclient import TestClient

from jarvis.core.config import Settings
from jarvis.main import create_app


def client(provider=None, **overrides):
    settings = Settings(environment="test", **overrides)
    return TestClient(create_app(settings=settings, provider=provider))


def test_health_and_readiness_include_version():
    with client() as test_client:
        assert test_client.get("/health").json() == {"status": "ok", "version": "0.1.0"}
        assert test_client.get("/ready").json() == {"status": "ready", "version": "0.1.0"}


def test_demo_chat_returns_clear_structured_response():
    marker = "private-message-marker"
    with client() as test_client:
        response = test_client.post("/chat", json={"message": marker})
    assert response.status_code == 200
    assert response.json() == {
        "reply": f"This is a JARVIS demo response, not an AI-generated answer. I received: {marker}",
        "provider": "mock",
        "demo": True,
        "partial": False,
    }
    assert len(response.headers["x-jarvis-execution-id"]) == 32


def test_optional_persistence_saves_user_and_assistant_without_changing_response():
    class Repository:
        def __init__(self):
            self.saved = None

        def save_exchange(self, user_content, assistant_content):
            self.saved = (user_content, assistant_content)

    repository = Repository()
    app = create_app(settings=Settings(environment="test"))
    app.state.conversation_repository = repository
    with TestClient(app) as test_client:
        response = test_client.post("/chat", json={"message": "persist this request"})
    assert response.status_code == 200
    assert set(response.json()) == {"reply", "provider", "demo", "partial"}
    assert repository.saved == (
        "persist this request",
        "This is a JARVIS demo response, not an AI-generated answer. I received: persist this request",
    )


def test_persistence_failure_does_not_report_success_or_leak_database_details(caplog):
    class UnavailableRepository:
        def save_exchange(self, user_content, assistant_content):
            from jarvis.persistence.base import PersistenceError
            raise PersistenceError() from RuntimeError("private database credential")

    app = create_app(settings=Settings(environment="test"))
    app.state.conversation_repository = UnavailableRepository()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.post("/chat", json={"message": "do not lose this"})
    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "detail": "The response could not be saved.",
    }
    assert response.headers["x-jarvis-execution-id"]
    assert "private database credential" not in response.text
    assert "private database credential" not in caplog.text


def test_enabled_persistence_constructs_adapter_without_connecting(monkeypatch):
    from pydantic import SecretStr

    constructed = {}
    sentinel = object()

    def adapter(database_url, *, connect_timeout_seconds):
        constructed.update(url=database_url, timeout=connect_timeout_seconds)
        return sentinel

    monkeypatch.setattr("jarvis.main.PostgresConversationRepository", adapter)
    app = create_app(settings=Settings(
        environment="test",
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis"),
    ))
    assert app.state.conversation_repository is sentinel
    assert constructed == {"url": "postgresql://local/jarvis", "timeout": 3}


def test_chat_returns_partial_status_with_useful_expert_output():
    from jarvis.orchestration.registry import ExpertRegistry, ToolRegistry, ToolRegistryConfig
    from jarvis.orchestration.service import OrchestrationService

    settings = Settings(environment="test")
    app = create_app(settings=settings)

    class PartialProvider:
        name = "mock"
        is_demo = True

        def __init__(self):
            self.calls = 0

        async def generate_reply(self, message, *, system_instruction=None):
            self.calls += 1
            if self.calls == 1:
                return "Useful partial analysis."
            raise RuntimeError("private provider detail")

    provider = PartialProvider()
    registry = ExpertRegistry.load()
    # Reuse the default registry shape with two sequential experts for analysis.
    data = registry.config.model_dump()
    data["intent_experts"]["analysis"] = ("analysis", "validation")
    registry = ExpertRegistry(type(registry.config).model_validate(data))
    app.state.provider = provider
    app.state.orchestration_service = OrchestrationService(
        provider,
        settings,
        expert_registry=registry,
        tool_registry=ToolRegistry(ToolRegistryConfig(version=1, tools=())),
    )

    with TestClient(app) as test_client:
        response = test_client.post("/chat", json={"message": "Please analyze this"})
    assert response.status_code == 200
    assert response.json() == {
        "reply": "Useful partial analysis.",
        "provider": "mock",
        "demo": True,
        "partial": True,
    }


def test_chat_message_is_not_written_to_logs(caplog):
    marker = "do-not-log-this-message"
    with caplog.at_level("INFO", logger="jarvis"):
        with client() as test_client:
            response = test_client.post("/chat", json={"message": marker})
    assert response.status_code == 200
    assert any(
        record.name == "jarvis.main" and record.getMessage() == "JARVIS starting"
        for record in caplog.records
    )
    assert marker not in caplog.text


def test_configure_logging_preserves_host_root_handlers():
    from jarvis.core.logging_config import configure_logging

    root = logging.getLogger()
    original_handlers = tuple(root.handlers)
    original_level = root.level
    host_handler = logging.StreamHandler(io.StringIO())
    root.addHandler(host_handler)
    try:
        configure_logging("INFO")
        assert host_handler in root.handlers
        assert tuple(handler for handler in root.handlers if handler is not host_handler) == original_handlers
        assert root.level == original_level
    finally:
        root.removeHandler(host_handler)


def test_invalid_chat_request_has_clear_error():
    with client() as test_client:
        response = test_client.post("/chat", json={"message": "   "})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "detail": "The request body is invalid."}


def test_provider_failure_is_sanitized(caplog):
    class BrokenProvider:
        name = "broken"
        is_demo = False

        async def generate_reply(self, message, *, system_instruction=None):
            raise RuntimeError("secret provider detail")

    with client(provider=BrokenProvider()) as test_client:
        response = test_client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 502
    assert len(response.headers["x-jarvis-execution-id"]) == 32
    assert response.json() == {
        "error": "provider_error",
        "detail": "The configured provider failed.",
    }
    assert "secret provider detail" not in response.text
    assert "Chat provider failed" in caplog.text
    assert any(
        record.getMessage() == "Chat provider failed"
        and getattr(record, "error_type", None) == "RuntimeError"
        and len(getattr(record, "execution_id", "")) == 32
        for record in caplog.records
    )
    assert "secret provider detail" not in caplog.text


def test_provider_timeout_is_sanitized(caplog):
    import asyncio

    class SlowProvider:
        name = "slow"
        is_demo = False

        async def generate_reply(self, message, *, system_instruction=None):
            await asyncio.sleep(0.05)
            return "too late"

    with client(provider=SlowProvider(), request_timeout_seconds=0.01) as test_client:
        response = test_client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 504
    assert len(response.headers["x-jarvis-execution-id"]) == 32
    assert response.json() == {
        "error": "provider_timeout",
        "detail": "The configured provider timed out.",
    }
    assert any(
        record.getMessage() == "Chat provider timed out"
        and getattr(record, "execution_id", None) == response.headers["x-jarvis-execution-id"]
        for record in caplog.records
    )


def test_message_limit_uses_validated_configuration():
    with client(max_message_length=3) as test_client:
        response = test_client.post("/chat", json={"message": "four"})
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_message_limit_accepts_exact_boundary():
    with client(max_message_length=5) as test_client:
        response = test_client.post("/chat", json={"message": "hello"})
    assert response.status_code == 200


def test_schema_maximum_message_length_uses_413():
    with client(max_message_length=20000) as test_client:
        response = test_client.post("/chat", json={"message": "x" * 20001})
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_request_body_byte_limit_is_enforced():
    with client(max_request_body_bytes=1024) as test_client:
        response = test_client.post(
            "/chat",
            content=b'{"message":"hello","padding":"' + b"x" * 1100 + b'"}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {
        "error": "request_too_large",
        "detail": "Request body exceeds the configured size limit.",
    }
    assert response.headers["connection"] == "close"


def test_request_body_byte_limit_applies_to_streamed_body_without_content_length():
    def chunks():
        yield b'{"message":"'
        yield b"x" * 1100
        yield b'"}'

    with client(max_request_body_bytes=1024) as test_client:
        response = test_client.post(
            "/chat",
            content=chunks(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert response.headers["connection"] == "close"


def test_extra_chat_fields_are_rejected():
    with client() as test_client:
        response = test_client.post("/chat", json={"message": "hi", "extra": 123})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_unexpected_error_has_sanitized_500_response(caplog):
    from fastapi.testclient import TestClient

    app = create_app(settings=Settings(environment="test"))

    @app.get("/test-error")
    async def trigger_error():
        raise RuntimeError("private failure detail")

    with caplog.at_level("ERROR", logger="jarvis"):
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.get("/test-error")
    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "detail": "An unexpected server error occurred.",
    }
    assert "Unhandled application error" in caplog.text
    assert "private failure detail" not in caplog.text


def test_provider_router_rejects_unsupported_provider():
    import pytest

    from jarvis.intelligence.router import build_provider

    with pytest.raises(ValueError, match="Unsupported provider"):
        build_provider("unknown")


def test_invalid_configuration_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(request_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(provider="unknown")
    with pytest.raises(ValidationError):
        Settings(max_request_body_bytes=100)
    with pytest.raises(ValidationError):
        Settings(provider_connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(provider_read_timeout_seconds=61)


def test_environment_configuration_is_validated(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER", "mock")
    monkeypatch.setenv("JARVIS_MAX_MESSAGE_LENGTH", "123")
    settings = Settings(_env_file=None)
    assert settings.provider == "mock"
    assert settings.max_message_length == 123

    monkeypatch.setenv("JARVIS_PROVIDER", "not-a-provider")
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
