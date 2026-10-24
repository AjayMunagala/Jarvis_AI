import io
import logging
from uuid import UUID, uuid4
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jarvis.core.config import Settings
from jarvis.main import create_app

TEST_AUTH_TOKEN = "test-only-token-with-at-least-32-characters"
TEST_OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_AUTH_TOKEN}"}


def persistent_settings(**overrides):
    from pydantic import SecretStr
    return Settings(
        _env_file=None, environment="test", persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis"),
        local_auth_token=SecretStr(TEST_AUTH_TOKEN), owner_id=TEST_OWNER_ID, **overrides,
    )


def persistent_client(app, **options):
    return TestClient(app, headers=AUTH_HEADERS, **options)


def client(provider=None, **overrides):
    settings_values = {}
    headers = {}
    if provider is not None and getattr(provider, "is_demo", False) is not True:
        from pydantic import SecretStr
        settings_values.update(
            local_auth_token=SecretStr(TEST_AUTH_TOKEN),
            owner_id=TEST_OWNER_ID,
        )
        headers = AUTH_HEADERS
    settings = Settings(environment="test", **settings_values, **overrides)
    return TestClient(create_app(settings=settings, provider=provider), headers=headers)


def test_health_and_readiness_include_version():
    with client() as test_client:
        assert test_client.get("/health").json() == {"status": "ok", "version": "0.1.0"}
        assert test_client.get("/ready").json() == {
            "status": "ready",
            "version": "0.1.0",
            "components": {
                "application": "ready",
                "provider": "ready",
                "orchestration": "ready",
                "authorization": "ready",
                "persistence": "disabled",
                "repository_intelligence": "ready",
            },
        }


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
        "conversation_id": None,
        "memory_context": None,
    }
    assert len(response.headers["x-jarvis-execution-id"]) == 32


def test_optional_persistence_saves_exchange_with_additive_chat_metadata():
    class Repository:
        def __init__(self):
            self.saved = None

        def save_exchange(self, user_content, assistant_content, conversation_id=None):
            self.saved = (user_content, assistant_content)
            return conversation_id or UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    repository = Repository()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    with persistent_client(app) as test_client:
        response = test_client.post("/chat", json={"message": "persist this request"})
    assert response.status_code == 200
    assert response.json()["conversation_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert repository.saved == (
        "persist this request",
        "This is a JARVIS demo response, not an AI-generated answer. I received: persist this request",
    )


def test_chat_continues_supplied_conversation_id_and_returns_same_id():
    conversation_id = uuid4()

    class Repository:
        def __init__(self):
            self.calls = []

        def save_exchange(self, user_content, assistant_content, supplied_id=None):
            self.calls.append((user_content, assistant_content, supplied_id))
            return supplied_id

    repository = Repository()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    with persistent_client(app) as test_client:
        response = test_client.post(
            "/chat", json={"message": "continue", "conversation_id": str(conversation_id)}
        )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == str(conversation_id)
    assert repository.calls[0][2] == conversation_id


def test_chat_unknown_conversation_is_not_replaced():
    from jarvis.persistence.base import ConversationNotFound

    conversation_id = uuid4()

    class Repository:
        def save_exchange(self, user_content, assistant_content, supplied_id=None):
            assert supplied_id == conversation_id
            raise ConversationNotFound()

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = Repository()
    with persistent_client(app) as test_client:
        response = test_client.post(
            "/chat", json={"message": "continue", "conversation_id": str(conversation_id)}
        )
    assert response.status_code == 404
    assert response.json() == {
        "error": "conversation_not_found",
        "detail": "The requested conversation was not found.",
    }


def test_history_returns_bounded_chronological_isolated_messages():
    conversation_id, other_id = uuid4(), uuid4()

    class Repository:
        def get_messages(self, requested_id, *, before_sequence=None, limit=51):
            assert requested_id == conversation_id
            assert before_sequence is None
            assert limit == 3
            return tuple(
                ConversationMessage(
                    id=uuid4(), conversation_id=conversation_id, sequence_number=i,
                    role="user" if i % 2 == 0 else "assistant", content=f"m{i}",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
                for i in range(3)
            )

    from jarvis.persistence.base import ConversationMessage

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = Repository()
    with persistent_client(app) as test_client:
        response = test_client.get(f"/conversations/{conversation_id}/messages?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == str(conversation_id)
    assert [m["sequence_number"] for m in data["messages"]] == [1, 2]
    assert data["has_more"] is True
    assert data["next_before_sequence"] == 1
    assert str(other_id) not in response.text


def test_history_is_unavailable_when_persistence_is_disabled():
    with client() as test_client:
        response = test_client.get(f"/conversations/{uuid4()}/messages")
    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "detail": "Conversation persistence is not enabled.",
    }


def test_conversation_list_returns_authenticated_bounded_page_without_logging_previews(caplog):
    from jarvis.models.schemas import ConversationListResponse, ConversationSummary
    from jarvis.security.policy import Capability, CapabilityPolicy

    created_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    activity_at = datetime(2026, 2, 4, 5, 6, 7, tzinfo=timezone.utc)
    conversation_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    private_preview = "do-not-log-conversation-preview"

    class Repository:
        def __init__(self):
            self.calls = []

        def list_conversations(self, *, limit=20, offset=0):
            self.calls.append((limit, offset))
            return ConversationListResponse(
                items=[ConversationSummary(
                    conversation_id=conversation_id,
                    created_at=created_at,
                    last_activity_at=activity_at,
                    preview=private_preview,
                )],
                limit=limit,
                offset=offset,
                has_more=True,
            )

    class RecordingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(TEST_OWNER_ID)
            self.calls = []

        def allows(self, owner_id, capability, **kwargs):
            self.calls.append((owner_id, capability))
            return super().allows(owner_id, capability, **kwargs)

    repository = Repository()
    policy = RecordingPolicy()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    app.state.authorization_policy = policy
    with caplog.at_level("INFO", logger="jarvis"):
        with persistent_client(app) as test_client:
            response = test_client.get("/conversations")

    assert response.status_code == 200
    assert response.json() == {
        "items": [{
            "conversation_id": str(conversation_id),
            "created_at": "2026-02-03T04:05:06Z",
            "last_activity_at": "2026-02-04T05:06:07Z",
            "preview": private_preview,
        }],
        "limit": 20,
        "offset": 0,
        "has_more": True,
    }
    assert repository.calls == [(20, 0)]
    assert policy.calls == [(TEST_OWNER_ID, Capability.CONVERSATION_READ)]
    assert private_preview not in caplog.text


def test_conversation_list_uses_custom_pagination_and_empty_result():
    from jarvis.models.schemas import ConversationListResponse

    class Repository:
        def __init__(self):
            self.calls = []

        def list_conversations(self, *, limit=20, offset=0):
            self.calls.append((limit, offset))
            return ConversationListResponse(
                items=[], limit=limit, offset=offset, has_more=False
            )

    repository = Repository()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    with persistent_client(app) as test_client:
        response = test_client.get("/conversations?limit=7&offset=13")

    assert response.status_code == 200
    assert response.json() == {
        "items": [], "limit": 7, "offset": 13, "has_more": False,
    }
    assert repository.calls == [(7, 13)]


@pytest.mark.parametrize(
    ("query", "expected_detail"),
    [
        ("limit=0", "The request body is invalid."),
        ("limit=101", "The request body is invalid."),
        ("offset=-1", "The request body is invalid."),
        ("offset=1000001", "The request body is invalid."),
    ],
)
def test_conversation_list_rejects_out_of_range_pagination(query, expected_detail):
    class Repository:
        def list_conversations(self, **_kwargs):
            raise AssertionError("Invalid pagination must not reach persistence")

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = Repository()
    with persistent_client(app) as test_client:
        response = test_client.get(f"/conversations?{query}")

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "detail": expected_detail}


def test_conversation_list_is_unavailable_when_persistence_is_disabled():
    with client() as test_client:
        response = test_client.get("/conversations")
    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "detail": "Conversation persistence is not enabled.",
    }


def test_conversation_list_sanitizes_persistence_failures(caplog):
    from jarvis.persistence.base import PersistenceError

    class Repository:
        def list_conversations(self, **_kwargs):
            raise PersistenceError() from RuntimeError("private SQL and credential details")

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = Repository()
    with caplog.at_level("ERROR", logger="jarvis"):
        with persistent_client(app) as test_client:
            response = test_client.get("/conversations")

    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "detail": "Conversation list is unavailable.",
    }
    assert "private SQL" not in response.text
    assert "credential details" not in caplog.text


@pytest.mark.parametrize(
    ("failure", "status", "error_code"),
    [
        ("missing", 404, "conversation_not_found"),
        ("database", 503, "persistence_unavailable"),
    ],
)
def test_history_failures_are_sanitized(failure, status, error_code):
    from jarvis.persistence.base import ConversationNotFound, PersistenceError

    class Repository:
        def get_messages(self, conversation_id, *, before_sequence=None, limit=51):
            if failure == "missing":
                raise ConversationNotFound()
            raise PersistenceError() from RuntimeError("private database details")

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = Repository()
    with persistent_client(app) as test_client:
        response = test_client.get(f"/conversations/{uuid4()}/messages")
    assert response.status_code == status
    assert response.json()["error"] == error_code
    assert "private database details" not in response.text


def test_chat_cannot_continue_conversation_when_persistence_is_disabled():
    conversation_id = uuid4()
    with client() as test_client:
        response = test_client.post(
            "/chat", json={"message": "continue", "conversation_id": str(conversation_id)}
        )
    assert response.status_code == 503
    assert response.json()["error"] == "persistence_unavailable"
    assert response.headers["x-jarvis-execution-id"]


def test_document_ingestion_creates_one_owner_scoped_memory_with_sanitized_basename():
    from jarvis.memory.models import MemoryRecord

    class MemoryRepository:
        def __init__(self):
            self.calls = []

        def create_document(self, owner_id, content, filename):
            self.calls.append((owner_id, content, filename))
            return MemoryRecord(
                id=uuid4(), content=content, source_reference=filename, source_kind="document",
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            ), True

    repository = MemoryRepository()
    app = create_app(settings=persistent_settings())
    app.state.memory_repository = repository
    with persistent_client(app) as test_client:
        response = test_client.post(
            "/memories/ingest",
            content=b"Project notes",
            headers={"Content-Type": "application/octet-stream", "X-Document-Filename": r"C:\\Users\\private\\notes.MD"},
        )

    assert response.status_code == 201
    assert response.json()["content"] == "Project notes"
    assert response.json()["source_reference"] == "notes.MD"
    assert response.json()["source_kind"] == "document"
    assert repository.calls == [(TEST_OWNER_ID, "Project notes", "notes.MD")]
    assert "C:\\Users" not in response.text


@pytest.mark.parametrize(
    ("filename", "body", "status_code", "declared_length"),
    [
        ("notes.pdf", b"not imported", 422, None),
        ("notes.txt", b"\xff", 422, None),
        ("notes.txt", b"\x00text", 422, None),
        ("notes.txt", b"   \n", 422, None),
        ("bad%C2%85name.md", b"content", 422, None),
        ("C:notes.md", b"content", 422, None),
        ("notes.txt", b"x" * 4001, 422, None),
        # A deliberately understated Content-Length cannot bypass stream counting.
        ("notes.txt", b"x" * (16 * 1024 + 1), 413, "1"),
    ],
)
def test_document_ingestion_rejects_invalid_input_before_persistence(filename, body, status_code, declared_length):
    class MemoryRepository:
        def __init__(self):
            self.calls = []

        def create(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("Invalid documents must not be persisted")

    repository = MemoryRepository()
    app = create_app(settings=persistent_settings())
    app.state.memory_repository = repository
    with persistent_client(app) as test_client:
        headers = {"Content-Type": "application/octet-stream", "X-Document-Filename": filename}
        if declared_length is not None:
            headers["Content-Length"] = declared_length
        response = test_client.post(
            "/memories/ingest",
            content=body,
            headers=headers,
        )
    assert response.status_code == status_code
    assert response.json()["error"] in {"http_error", "request_too_large"}
    assert repository.calls == []
    decoded_sample = body[:32].decode("utf-8", errors="ignore")
    if decoded_sample:
        assert decoded_sample not in response.text


def test_document_ingestion_rejects_oversized_stream_without_content_length():
    class MemoryRepository:
        def __init__(self):
            self.calls = []

        def create(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("Oversized documents must not be persisted")

    repository = MemoryRepository()
    app = create_app(settings=persistent_settings())
    app.state.memory_repository = repository

    def body_chunks():
        yield b"x" * (16 * 1024 + 1)

    with persistent_client(app) as test_client:
        response = test_client.post(
            "/memories/ingest",
            content=body_chunks(),
            headers={"Content-Type": "application/octet-stream", "X-Document-Filename": "notes.txt"},
        )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"] in {"http_error", "request_too_large"}
    assert repository.calls == []


def test_document_ingestion_requires_authentication_and_octet_stream():
    app = create_app(settings=persistent_settings())
    app.state.memory_repository = object()
    with TestClient(app) as test_client:
        unauthorized = test_client.post(
            "/memories/ingest", content=b"text", headers={"X-Document-Filename": "notes.txt"}
        )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"

    app = create_app(settings=persistent_settings())
    app.state.memory_repository = object()
    with persistent_client(app) as test_client:
        wrong_type = test_client.post(
            "/memories/ingest", content=b"text", headers={"X-Document-Filename": "notes.txt"}
        )
    assert wrong_type.status_code == 422


def test_document_ingestion_sanitizes_persistence_failures():
    from jarvis.memory.base import MemoryPersistenceError

    class MemoryRepository:
        def create_document(self, *args, **kwargs):
            raise MemoryPersistenceError() from RuntimeError("private path and database details")

    app = create_app(settings=persistent_settings())
    app.state.memory_repository = MemoryRepository()
    with persistent_client(app) as test_client:
        response = test_client.post(
            "/memories/ingest", content=b"plain text",
            headers={"Content-Type": "application/octet-stream", "X-Document-Filename": "notes.txt"},
        )
    assert response.status_code == 503
    assert response.json() == {
        "error": "persistence_unavailable",
        "detail": "Memory storage is unavailable.",
    }
    assert "private path" not in response.text


def test_invalid_conversation_id_is_rejected():
    with client() as test_client:
        response = test_client.get("/conversations/not-a-uuid/messages")
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_unmatched_route_keeps_generic_not_found_error():
    with client() as test_client:
        response = test_client.get("/not-a-route")
    assert response.status_code == 404
    assert response.json()["error"] == "http_error"


def test_persistence_failure_does_not_report_success_or_leak_database_details(caplog):
    class UnavailableRepository:
        def save_exchange(self, user_content, assistant_content, conversation_id=None):
            from jarvis.persistence.base import PersistenceError
            raise PersistenceError() from RuntimeError("private database credential")

    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = UnavailableRepository()
    with persistent_client(app, raise_server_exceptions=False) as test_client:
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

    def adapter(database_url, *, connect_timeout_seconds, operation_timeout_seconds):
        constructed.update(
            url=database_url,
            timeout=connect_timeout_seconds,
            operation_timeout=operation_timeout_seconds,
        )
        return sentinel

    monkeypatch.setattr("jarvis.main.PostgresConversationRepository", adapter)
    app = create_app(settings=Settings(
        environment="test",
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis"),
        local_auth_token=SecretStr(TEST_AUTH_TOKEN),
        owner_id=TEST_OWNER_ID,
    ))
    assert app.state.conversation_repository is sentinel
    assert constructed == {
        "url": "postgresql://local/jarvis", "timeout": 3, "operation_timeout": 5
    }


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
        "conversation_id": None,
        "memory_context": None,
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


def test_overall_deadline_expiring_during_memory_retrieval_returns_request_timeout(monkeypatch):
    import time

    from jarvis.api.routes import chat as chat_route
    from jarvis.memory.models import MemoryRetrievalResult

    class RecordingProvider:
        name = "recording"
        is_demo = True

        def __init__(self):
            self.calls = 0

        async def generate_reply(self, message, *, system_instruction=None):
            self.calls += 1
            return "should not execute"

    provider = RecordingProvider()
    settings = persistent_settings(
        memory_retrieval_enabled=True,
        request_timeout_seconds=0.02,
    )
    app = create_app(settings=settings, provider=provider)
    app.state.memory_repository = object()

    def slow_retrieval(*args, **kwargs):
        time.sleep(0.05)
        return MemoryRetrievalResult(records=(), outcome="empty")

    monkeypatch.setattr(chat_route, "retrieve_context", slow_retrieval)
    with persistent_client(app) as test_client:
        response = test_client.post("/chat", json={"message": "request"})

    assert response.status_code == 504
    assert response.json() == {
        "error": "request_timeout",
        "detail": "The overall chat request timed out.",
    }
    execution_id = response.headers["x-jarvis-execution-id"]
    assert len(execution_id) == 32
    assert provider.calls == 0


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
