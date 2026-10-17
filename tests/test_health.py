from types import SimpleNamespace

from fastapi.responses import JSONResponse

from jarvis.api.routes.health import ready


def _request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state, version="2.0.0"))


def _wired_state(*, persistence_enabled=False):
    return SimpleNamespace(
        settings=SimpleNamespace(persistence_enabled=persistence_enabled),
        provider=SimpleNamespace(generate_reply=lambda: None, name="private-provider-name"),
        orchestration_service=SimpleNamespace(run=lambda: None),
        authorization_policy=SimpleNamespace(allows=lambda: True),
        repo_service=SimpleNamespace(get_inventory=lambda: None),
        conversation_repository=object() if persistence_enabled else None,
        memory_repository=object() if persistence_enabled else None,
    )


def test_readiness_reports_wired_components_and_valid_disabled_persistence():
    response = ready(_request_for(_wired_state()))

    assert response.status == "ready"
    assert response.components == {
        "application": "ready",
        "provider": "ready",
        "orchestration": "ready",
        "authorization": "ready",
        "repository_intelligence": "ready",
        "persistence": "disabled",
    }


def test_readiness_reports_configured_persistence_without_probing_database():
    state = _wired_state(persistence_enabled=True)
    response = ready(_request_for(state))

    assert response.status == "ready"
    assert response.components["persistence"] == "configured"
    assert response.components["repository_intelligence"] == "ready"


def test_readiness_fails_closed_when_required_components_are_missing():
    state = _wired_state()
    state.provider = None
    state.authorization_policy = None

    response = ready(_request_for(state))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"provider":"unavailable"' in response.body
    assert b'"authorization":"unavailable"' in response.body


def test_readiness_fails_closed_when_repo_intelligence_is_missing():
    state = _wired_state()
    state.repo_service = None

    response = ready(_request_for(state))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"repository_intelligence":"unavailable"' in response.body


def test_readiness_fails_closed_when_persistence_is_enabled_without_repositories():
    state = _wired_state(persistence_enabled=True)
    state.memory_repository = None

    response = ready(_request_for(state))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"persistence":"unavailable"' in response.body


def test_readiness_output_contains_no_provider_or_configuration_details():
    state = _wired_state()
    state.settings.private_token = "sensitive-token"
    state.settings.database_url = "postgresql://private"

    response = ready(_request_for(state))
    serialized = response.model_dump_json()

    assert "private-provider-name" not in serialized
    assert "sensitive-token" not in serialized
    assert "postgresql://private" not in serialized
