import logging
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.memory.models import MemoryPage
from jarvis.security.policy import Capability, CapabilityPolicy


TEST_TOKEN = "auth-regression-token-0123456789-abcdefghijklmnopqrstuvwxyz"
OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def persistent_settings(**overrides):
    return Settings(
        _env_file=None,
        environment="test",
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis"),
        local_auth_token=SecretStr(TEST_TOKEN),
        owner_id=OWNER_ID,
        **overrides,
    )


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic abc", "Bearer", "Bearer ", "Bearer wrong-token-value-which-is-long-enough"],
)
def test_persistence_endpoints_reject_missing_or_malformed_credentials(authorization):
    app = create_app(settings=persistent_settings())
    headers = {} if authorization is None else {"Authorization": authorization}
    paths = [
        ("get", "/memories"),
        ("get", "/conversations"),
        ("post", "/memories/search"),
        ("get", f"/memories/{uuid4()}"),
        ("get", f"/conversations/{uuid4()}/messages"),
        ("post", "/chat"),
    ]
    with TestClient(app) as client:
        responses = [
            client.request(method, path, headers=headers, json={"message": "hello"} if method == "post" and path == "/chat" else None)
            for method, path in paths
        ]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.headers["www-authenticate"] == "Bearer" for response in responses)
    assert all(response.json() == {
        "error": "unauthorized", "detail": "A valid bearer credential is required."
    } for response in responses)


def test_valid_bearer_resolves_only_the_configured_owner(monkeypatch):
    comparisons = []
    original_compare_digest = __import__("secrets").compare_digest

    def recording_compare_digest(left, right):
        comparisons.append((left, right))
        return original_compare_digest(left, right)

    monkeypatch.setattr("jarvis.api.dependencies.secrets.compare_digest", recording_compare_digest)

    class Repository:
        def __init__(self):
            self.owner_ids = []

        def list(self, owner_id, *, limit=50, offset=0, category=None):
            self.owner_ids.append(owner_id)
            return MemoryPage(items=(), total=0, limit=limit, offset=offset, has_more=False)

    app = create_app(settings=persistent_settings())
    repository = Repository()
    app.state.memory_repository = repository
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        response = client.get("/memories")

    assert response.status_code == 200
    assert repository.owner_ids == [OWNER_ID]
    assert comparisons == [(TEST_TOKEN.encode("utf-8"), TEST_TOKEN.encode("utf-8"))]


def test_invalid_token_is_not_returned_or_logged(caplog):
    sentinel = "credential-secret-sentinel-0123456789"
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings=persistent_settings())) as client:
        response = client.get("/memories", headers={"Authorization": f"Bearer {sentinel}"})
    assert response.status_code == 401
    assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_create_app_revalidates_settings_that_bypassed_model_validation():
    unsafe = Settings.model_construct(
        environment="test",
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis"),
        local_auth_token=None,
        owner_id=None,
    )
    with pytest.raises(ValueError, match="JARVIS_LOCAL_AUTH_TOKEN"):
        create_app(settings=unsafe)


def test_memory_routes_enforce_server_defined_read_and_write_capabilities():
    from datetime import datetime, timezone

    from jarvis.memory.models import MemoryRecord

    class RecordingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(OWNER_ID)
            self.calls = []

        def allows(self, owner_id, capability, **kwargs):
            self.calls.append((owner_id, capability))
            return super().allows(owner_id, capability, **kwargs)

    class Repository:
        def list(self, owner_id, *, limit=50, offset=0, category=None):
            return MemoryPage(items=(), total=0, limit=limit, offset=offset, has_more=False)

        def create(self, owner_id, content, category=None, source_reference=None):
            now = datetime.now(timezone.utc)
            return MemoryRecord(
                id=uuid4(), content=content, category=category,
                source_reference=source_reference, created_at=now, updated_at=now,
            )

    app = create_app(settings=persistent_settings())
    policy = RecordingPolicy()
    app.state.authorization_policy = policy
    app.state.memory_repository = Repository()
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        read_response = client.get("/memories")
        write_response = client.post("/memories", json={"content": "explicit memory"})

    assert read_response.status_code == 200
    assert write_response.status_code == 201
    assert policy.calls == [
        (OWNER_ID, Capability.MEMORY_READ),
        (OWNER_ID, Capability.MEMORY_WRITE),
    ]


def test_missing_policy_denies_memory_operation_before_repository_access():
    class Repository:
        calls = 0

        def list(self, owner_id, *, limit=50, offset=0, category=None):
            self.calls += 1
            return MemoryPage(items=(), total=0, limit=limit, offset=offset, has_more=False)

    repository = Repository()
    app = create_app(settings=persistent_settings())
    app.state.authorization_policy = None
    app.state.memory_repository = repository
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        response = client.get("/memories")

    assert response.status_code == 403
    assert response.json() == {"error": "http_error", "detail": "The operation is not permitted."}
    assert repository.calls == 0


def test_conversation_list_requires_server_side_read_capability():
    class DenyingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(OWNER_ID)
            self.calls = []

        def allows(self, owner_id, capability, **kwargs):
            self.calls.append((owner_id, capability))
            return False

    class Repository:
        calls = 0

        def list_conversations(self, *, limit=20, offset=0):
            self.calls += 1
            raise AssertionError("Denied conversation listing reached persistence")

    repository = Repository()
    policy = DenyingPolicy()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    app.state.authorization_policy = policy
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        response = client.get("/conversations")

    assert response.status_code == 403
    assert response.json() == {
        "error": "http_error", "detail": "The operation is not permitted."
    }
    assert policy.calls == [(OWNER_ID, Capability.CONVERSATION_READ)]
    assert repository.calls == 0


def test_conversation_list_ignores_client_owner_and_capability_claims():
    from jarvis.models.schemas import ConversationListResponse

    class RecordingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(OWNER_ID)
            self.calls = []

        def allows(self, owner_id, capability, **kwargs):
            self.calls.append((owner_id, capability))
            return super().allows(owner_id, capability, **kwargs)

    class Repository:
        def __init__(self):
            self.calls = []

        def list_conversations(self, *, limit=20, offset=0):
            self.calls.append((limit, offset))
            return ConversationListResponse(
                items=[], limit=limit, offset=offset, has_more=False
            )

    repository = Repository()
    policy = RecordingPolicy()
    app = create_app(settings=persistent_settings())
    app.state.conversation_repository = repository
    app.state.authorization_policy = policy
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        response = client.get(
            "/conversations?owner_id=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            "&capability=conversation.write"
        )

    assert response.status_code == 200
    assert policy.calls == [(OWNER_ID, Capability.CONVERSATION_READ)]
    assert repository.calls == [(20, 0)]


def test_chat_and_conversation_routes_enforce_their_capabilities():
    class RecordingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(OWNER_ID)
            self.calls = []

        def allows(self, owner_id, capability, **kwargs):
            self.calls.append((owner_id, capability))
            return super().allows(owner_id, capability, **kwargs)

    class ConversationRepository:
        def save_exchange(self, user_content, assistant_content, conversation_id=None):
            return conversation_id or uuid4()

        def get_messages(self, conversation_id, *, before_sequence=None, limit=51):
            return ()

    class MemoryRepository:
        def search_with_deadline(self, owner_id, query, *, deadline, limit=10, offset=0):
            return MemoryPage(items=(), total=0, limit=limit, offset=offset, has_more=False)

    app = create_app(settings=persistent_settings(memory_retrieval_enabled=True))
    policy = RecordingPolicy()
    app.state.authorization_policy = policy
    app.state.conversation_repository = ConversationRepository()
    app.state.memory_repository = MemoryRepository()
    conversation_id = uuid4()
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as client:
        chat_response = client.post("/chat", json={"message": "ordinary analysis"})
        history_response = client.get(f"/conversations/{conversation_id}/messages")

    assert chat_response.status_code == 200
    assert history_response.status_code == 200
    assert policy.calls == [
        (OWNER_ID, Capability.CHAT_ADVISORY),
        (OWNER_ID, Capability.CONVERSATION_WRITE),
        (OWNER_ID, Capability.MEMORY_READ),
        (OWNER_ID, Capability.CONVERSATION_READ),
    ]


@pytest.mark.parametrize(
    ("method", "path", "capability", "headers", "body"),
    [
        ("post", "/memories/ingest", Capability.MEMORY_WRITE,
         {"X-Document-Filename": "notes.txt", "Content-Type": "application/octet-stream"}, b"valid document"),
        ("post", "/memories/search", Capability.MEMORY_READ, {}, {"query": "valid query"}),
        ("get", "/memories/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", Capability.MEMORY_READ, {}, None),
        ("post", "/memories", Capability.MEMORY_WRITE, {}, {"content": "valid memory"}),
        ("patch", "/memories/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", Capability.MEMORY_WRITE, {}, {"content": "updated memory"}),
        ("delete", "/memories/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", Capability.MEMORY_WRITE, {}, None),
    ],
)
def test_memory_operation_policy_denials_stop_before_repository_access(
    method, path, capability, headers, body
):
    from datetime import datetime, timezone

    from jarvis.memory.models import MemoryRecord

    memory_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    class DenyingPolicy(CapabilityPolicy):
        def __init__(self):
            super().__init__(OWNER_ID)
            self.decisions = []

        def allows(self, owner_id, requested_capability, **kwargs):
            self.decisions.append((owner_id, requested_capability))
            if requested_capability is capability:
                return False
            return super().allows(owner_id, requested_capability, **kwargs)

    class Repository:
        def __init__(self):
            self.calls = []
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.records = {
                memory_id: MemoryRecord(
                    id=memory_id, content="original memory", created_at=now, updated_at=now
                )
            }

        def create(self, owner_id, content, category=None, source_reference=None):
            self.calls.append("create")
            raise AssertionError("denied create reached repository")

        def search(self, owner_id, query, *, limit=10, offset=0):
            self.calls.append("search")
            raise AssertionError("denied search reached repository")

        def get(self, owner_id, requested_id):
            self.calls.append("get")
            raise AssertionError("denied get reached repository")

        def update(self, owner_id, requested_id, changes):
            self.calls.append("update")
            raise AssertionError("denied update reached repository")

        def delete(self, owner_id, requested_id):
            self.calls.append("delete")
            raise AssertionError("denied delete reached repository")

    repository = Repository()
    original_records = repository.records.copy()
    app = create_app(settings=persistent_settings())
    policy = DenyingPolicy()
    app.state.authorization_policy = policy
    app.state.memory_repository = repository

    request_headers = {"Authorization": f"Bearer {TEST_TOKEN}", **headers}
    request_kwargs = {"headers": request_headers}
    if method == "post" and path == "/memories/ingest":
        request_kwargs["content"] = body
    elif body is not None:
        request_kwargs["json"] = body
    with TestClient(app) as client:
        response = client.request(method, path, **request_kwargs)

    assert response.status_code == 403
    assert response.json() == {
        "error": "http_error", "detail": "The operation is not permitted."
    }
    assert policy.decisions == [(OWNER_ID, capability)]
    assert repository.calls == []
    assert repository.records == original_records
