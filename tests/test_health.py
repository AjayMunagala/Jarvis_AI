from types import SimpleNamespace

from fastapi.responses import JSONResponse

from jarvis.api.routes.health import ready


def _request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state, version="2.0.0"))


def _wired_state(*, persistence_enabled=False):
    return SimpleNamespace(
        settings=SimpleNamespace(persistence_enabled=persistence_enabled, provider="mock"),
        provider=SimpleNamespace(generate_reply=lambda: None, name="mock", is_demo=True),
        orchestration_service=SimpleNamespace(run=lambda: None),
        authorization_policy=SimpleNamespace(allows=lambda: True),
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
        "persistence": "not_required",
    }


def test_readiness_reports_configured_persistence_as_not_checked_without_probing_database():
    state = _wired_state(persistence_enabled=True)
    response = ready(_request_for(state))

    assert response.status == "degraded"
    assert response.components["persistence"] == "not_checked"


def test_readiness_reports_remote_provider_as_not_checked_without_network_call():
    state = _wired_state()
    state.settings.provider = "openai"
    state.provider = SimpleNamespace(generate_reply=lambda: None, name="openai", is_demo=False)

    response = ready(_request_for(state))

    assert response.status == "degraded"
    assert response.components["provider"] == "not_checked"


def test_readiness_fails_closed_when_provider_does_not_match_validated_configuration():
    state = _wired_state()
    state.provider = SimpleNamespace(generate_reply=lambda: None, name="unexpected", is_demo=True)

    response = ready(_request_for(state))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"provider":"unavailable"' in response.body


def test_readiness_fails_closed_when_required_components_are_missing():
    state = _wired_state()
    state.provider = None
    state.authorization_policy = None

    response = ready(_request_for(state))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert response.body == (
        b'{"status":"not_ready","version":"2.0.0","components":'
        b'{"application":"ready","provider":"unavailable",'
        b'"orchestration":"ready","authorization":"unavailable",'
        b'"persistence":"not_required"}}'
    )


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
