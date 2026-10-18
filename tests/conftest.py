"""Unit-suite isolation from the developer's local configuration.

Tests that build default ``Settings()`` must not read a developer's ``.env``
(for example a real provider key) or inherited ``JARVIS_*`` environment
variables: that made results depend on the machine and could send requests to
a live provider. Explicit ``Settings(...)`` arguments in tests are unaffected.
The opt-in ``integration_tests`` suite is configured separately.
"""

import os

import pytest

from jarvis.core.config import Settings

Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _isolate_jarvis_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("JARVIS_"):
            monkeypatch.delenv(name, raising=False)
