"""Integration adapters and secret references (v4.8).

Only one adapter exists, ``mock.lamp``: a simulated lamp whose state lives in
memory. It exercises the whole framework (enrollment, approval, secret
reference, read-only and state-changing actions, revocation) without
controlling any real device. A real adapter must implement the same
``AdapterSpec`` interface and needs its own review before it is registered.
"""

import os
import threading
from typing import Any, Callable

from pydantic import SecretStr

from jarvis.integrations.models import AdapterAction, AdapterDescriptor


class AdapterError(ValueError):
    """Invalid action or arguments (HTTP 422); the message is safe to show."""


def resolve_secret(secret_ref: str | None) -> SecretStr | None:
    """Resolve an ``env:`` secret reference at call time. The value is never logged or returned to clients."""
    if secret_ref is None:
        return None
    _, _, name = secret_ref.partition(":")
    value = os.environ.get(name)
    return SecretStr(value) if value else None


class AdapterSpec:
    descriptor: AdapterDescriptor

    def action(self, name: str) -> AdapterAction | None:
        return next((item for item in self.descriptor.actions if item.name == name), None)

    def validate(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self, enrollment_id: str, action: str, arguments: dict[str, Any],
                secret: SecretStr | None) -> dict[str, Any]:
        raise NotImplementedError

    def forget(self, enrollment_id: str) -> None:
        """Drop any state kept for a revoked enrollment."""


class MockLampAdapter(AdapterSpec):
    descriptor = AdapterDescriptor(
        adapter_id="mock.lamp", name="Simulated lamp", mode="mock", requires_secret=False,
        actions=(
            AdapterAction(name="read_state", read_only=True, description="Read the simulated power and brightness."),
            AdapterAction(name="set_power", read_only=False, description="Turn the simulated lamp on or off."),
            AdapterAction(name="set_brightness", read_only=False, description="Set the simulated brightness (0-100)."),
        ),
        limitations=("Simulation only: no real device is contacted; state is kept in memory and lost on restart.",),
    )

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def validate(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action == "read_state":
            if arguments:
                raise AdapterError("read_state takes no arguments.")
            return {}
        if action == "set_power":
            if set(arguments) != {"on"} or not isinstance(arguments["on"], bool):
                raise AdapterError("set_power needs exactly one boolean argument: on.")
            return {"on": arguments["on"]}
        if action == "set_brightness":
            level = arguments.get("level")
            if set(arguments) != {"level"} or isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
                raise AdapterError("set_brightness needs exactly one integer argument: level, from 0 to 100.")
            return {"level": level}
        raise AdapterError(f"The adapter has no action '{action}'.")

    def execute(self, enrollment_id, action, arguments, secret):
        with self._lock:
            state = self._state.setdefault(enrollment_id, {"power": "off", "brightness": 0})
            if action == "set_power":
                state["power"] = "on" if arguments["on"] else "off"
            elif action == "set_brightness":
                state["brightness"] = arguments["level"]
            return {"power": state["power"], "brightness": state["brightness"], "simulated": True}

    def forget(self, enrollment_id):
        with self._lock:
            self._state.pop(enrollment_id, None)


def default_adapters() -> dict[str, AdapterSpec]:
    adapter = MockLampAdapter()
    return {adapter.descriptor.adapter_id: adapter}
