"""Model registry: validated metadata plus runtime availability.

Descriptors are data (``config/models.json``) validated by ``RegistryConfig``.
Availability is decided at runtime from the adapters JARVIS actually built, so
a model is never reported available because a file says so.
"""

import json
from pathlib import Path
from typing import Mapping

from jarvis.routing.models import ModelDescriptor, ModelStatus, PolicyConfig, RegistryConfig, RoutingPolicy

CONFIG_DIR = Path(__file__).with_name("config")
_PLACEHOLDER = "${JARVIS_OPENAI_MODEL}"


class ModelRegistry:
    def __init__(self, config: RegistryConfig, *, openai_model: str | None = None):
        models = []
        for item in config.models:
            if item.model_id == _PLACEHOLDER:
                # The OpenAI-compatible adapter is bound to one configured model.
                item = item.model_copy(update={"model_id": openai_model or "unconfigured"})
            models.append(item)
        self._models = {item.model_key: item for item in models}

    @classmethod
    def load(cls, path: Path | None = None, *, openai_model: str | None = None) -> "ModelRegistry":
        raw = json.loads((path or CONFIG_DIR / "models.json").read_text(encoding="utf-8"))
        return cls(RegistryConfig.model_validate(raw), openai_model=openai_model)

    def models(self) -> tuple[ModelDescriptor, ...]:
        return tuple(self._models.values())

    def get(self, model_key: str) -> ModelDescriptor | None:
        return self._models.get(model_key)

    def status(self, adapters: Mapping[str, object], disabled: frozenset[str] = frozenset()) -> tuple[ModelStatus, ...]:
        statuses = []
        for item in self._models.values():
            if item.model_key in disabled:
                statuses.append(ModelStatus(model=item, available=False, reason="Disabled by configuration."))
            elif item.provider not in adapters:
                requirement = ", ".join(item.requires) or "a configured adapter"
                statuses.append(ModelStatus(model=item, available=False,
                                            reason=f"No {item.provider!r} adapter is configured (requires {requirement})."))
            else:
                statuses.append(ModelStatus(model=item, available=True, reason="Adapter configured."))
        return tuple(statuses)


def load_policies(path: Path | None = None) -> PolicyConfig:
    raw = json.loads((path or CONFIG_DIR / "policies.json").read_text(encoding="utf-8"))
    return PolicyConfig.model_validate(raw)


def policy_by_name(config: PolicyConfig, name: str | None) -> RoutingPolicy:
    wanted = name or config.default_policy
    for item in config.policies:
        if item.name == wanted:
            return item
    raise KeyError(wanted)
