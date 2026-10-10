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
    with client() as test_client:
        response = test_client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json() == {
        "reply": "This is a JARVIS demo response, not an AI-generated answer. I received: Hello",
        "provider": "mock",
        "demo": True,
    }


def test_invalid_chat_request_has_clear_error():
    with client() as test_client:
        response = test_client.post("/chat", json={"message": "   "})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "detail": "The request body is invalid."}


def test_provider_failure_is_sanitized(caplog):
    class BrokenProvider:
        name = "broken"
        is_demo = False

        async def generate_reply(self, message):
            raise RuntimeError("secret provider detail")

    with client(provider=BrokenProvider()) as test_client:
        response = test_client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 502
    assert response.json() == {
        "error": "provider_error",
        "detail": "The configured provider failed.",
    }
    assert "secret provider detail" not in response.text
    assert "secret provider detail" not in caplog.text


def test_provider_timeout_is_sanitized():
    import asyncio

    class SlowProvider:
        name = "slow"
        is_demo = False

        async def generate_reply(self, message):
            await asyncio.sleep(0.05)
            return "too late"

    with client(provider=SlowProvider(), request_timeout_seconds=0.01) as test_client:
        response = test_client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 504
    assert response.json() == {
        "error": "provider_timeout",
        "detail": "The configured provider timed out.",
    }


def test_message_limit_uses_validated_configuration():
    with client(max_message_length=3) as test_client:
        response = test_client.post("/chat", json={"message": "four"})
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


def test_request_body_byte_limit_applies_to_streamed_body_without_content_length():
    import asyncio

    from jarvis.core.middleware import RequestBodyLimitMiddleware

    async def exercise_middleware():
        sent = []
        chunks = [
            {"type": "http.request", "body": b"x" * 600, "more_body": True},
            {"type": "http.request", "body": b"y" * 500, "more_body": False},
        ]

        async def app(scope, receive, send):
            while (await receive())["more_body"]:
                pass

        async def receive():
            return chunks.pop(0)

        async def send(message):
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(app, max_body_bytes=1024)
        await middleware(
            {"type": "http", "headers": []},
            receive,
            send,
        )
        return sent

    sent = asyncio.run(exercise_middleware())
    assert sent[0]["status"] == 413
    assert b"request_too_large" in sent[1]["body"]


def test_invalid_configuration_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(request_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(provider="unknown")
    with pytest.raises(ValidationError):
        Settings(max_request_body_bytes=100)


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
