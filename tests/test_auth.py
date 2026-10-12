import logging
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.memory.models import MemoryPage


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
