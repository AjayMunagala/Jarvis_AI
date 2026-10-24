"""Regression: a developer's .env or JARVIS_* variables never change unit-test behaviour."""

import os

from jarvis.core.config import Settings


def test_local_env_file_and_variables_are_ignored(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "JARVIS_PROVIDER=openai\nJARVIS_OPENAI_API_KEY=placeholder-not-a-key\nJARVIS_TOOLS_ENABLED=true\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert not any(name.startswith("JARVIS_") for name in os.environ)
    settings = Settings()
    assert settings.provider == "mock" and settings.openai_api_key is None and settings.tools_enabled is False
