"""v3.0 unified interface configuration tests (public, non-sensitive feature flags)."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from jarvis.api.routes.interface import InterfaceConfig, VoiceConfig
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "interface-test-token-with-at-least-32-characters"


def client(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    app = create_app(settings=Settings(**values))
    return app, TestClient(app)


def test_defaults_offer_opt_in_browser_native_voice_and_report_disabled_features():
    app, test_client = client()
    with test_client:
        response = test_client.get("/interface/config")
    assert response.status_code == 200
    body = response.json()
    assert body["voice"] == {"enabled": True, "mode": "browser_native", "server_receives_audio": False,
                             "auto_send": False, "requires_opt_in": True}
    assert body["features"] == {"chat": True, "memory": False, "coding": True, "browser": False, "tools": False,
                                "chat_tools": False, "activity": True, "approval_execution": False}
    assert app.state.settings.voice_enabled is True


def test_config_reflects_settings_and_never_reports_chat_tools_without_tools():
    _, test_client = client(voice_enabled=False, tools_enabled=True, chat_tools_enabled=True,
                            browser_enabled=True, browser_provider="mock")
    with test_client:
        body = test_client.get("/interface/config").json()
    assert body["voice"]["enabled"] is False
    assert body["features"]["tools"] is True and body["features"]["chat_tools"] is True
    assert body["features"]["browser"] is True


def test_config_is_public_but_contains_no_sensitive_values():
    app, test_client = client(persistence_enabled=False)
    with test_client:
        text = test_client.get("/interface/config").text
    for secret in (TOKEN, str(OWNER), "http://", "https://", "postgres", "mock"):
        assert secret not in text


def test_contract_cannot_claim_server_audio_auto_send_or_execution():
    with pytest.raises(ValidationError):
        VoiceConfig(enabled=True, server_receives_audio=True)
    with pytest.raises(ValidationError):
        VoiceConfig(enabled=True, auto_send=True)
    with pytest.raises(ValidationError):
        InterfaceConfig.model_validate({"features": {"chat": True, "memory": True, "coding": True, "browser": True,
                                                     "tools": True, "chat_tools": True, "activity": True,
                                                     "approval_execution": True}, "voice": {"enabled": True}})


def test_execution_controls_are_unchanged_in_v30():
    app, _ = client()
    assert app.state.settings.verification_execution_enabled is False
    assert app.state.settings.tools_enabled is False and app.state.settings.chat_tools_enabled is False
    assert app.state.orchestration_service.tool_registry.execution_enabled is False
    assert app.state.authorization_policy.allows(OWNER, Capability.EXTERNAL_ACTION_EXECUTE) is False
