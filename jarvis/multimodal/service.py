"""Multimodal capability checks and analysis through the v5.1 router.

The router decides which models are eligible for the request's modalities.
Media or transcripts force ``local_only`` privacy unless the owner set
``allow_external_disclosure`` for that request, so a remote provider is never
even considered without authorization. Analysis runs only on models that have
a registered multimodal adapter; text-only adapters never receive media.
"""

import asyncio
from typing import Mapping, Protocol

from jarvis.multimodal.models import (
    InspectionResult, Interpretation, MultimodalRequest, MultimodalResult, NormalizedPart,
)
from jarvis.multimodal.normalize import normalize
from jarvis.routing.models import TaskRequirements
from jarvis.routing.router import ModelRouter

_ROUTER_MODALITY = {"text": "text", "image": "image", "audio_transcript": "audio_transcript",
                    "visual_observation": "text"}
_SENSITIVE = {"image", "audio_transcript"}


class MultimodalAdapter(Protocol):
    name: str
    simulated: bool

    async def analyze(self, prompt: str, parts: tuple[NormalizedPart, ...]) -> str: ...


class MockMultimodalAdapter:
    """Deterministic stand-in: it reports what it received, it does not see images."""

    simulated = True

    def __init__(self, name: str = "mock-vision"):
        self.name = name
        self.calls: list[tuple[str, int]] = []

    async def analyze(self, prompt, parts):
        self.calls.append((prompt, len(parts)))
        summary = ", ".join(
            f"{item.descriptor.modality}"
            + (f" {item.descriptor.media_type} {item.descriptor.width}x{item.descriptor.height}"
               if item.descriptor.modality == "image" else "")
            for item in parts) or "text only"
        return f"Simulated interpretation (no real image understanding): received {summary}."


class MultimodalService:
    def __init__(self, router: ModelRouter, adapters: Mapping[str, MultimodalAdapter], *, timeout_seconds: float = 60.0):
        self.router = router
        self.adapters = dict(adapters)
        self.timeout_seconds = timeout_seconds

    def _requirements(self, request: MultimodalRequest, parts: tuple[NormalizedPart, ...]) -> TaskRequirements:
        modalities = {"text"} | {_ROUTER_MODALITY[item.descriptor.modality] for item in parts}
        sensitive = any(item.descriptor.modality in _SENSITIVE for item in parts)
        return TaskRequirements(
            task_type="vision" if "image" in modalities else "general",
            modalities=tuple(sorted(modalities, key=lambda value: value != "text")),
            privacy="local_only" if sensitive and not request.allow_external_disclosure else "any",
        )

    def inspect(self, request: MultimodalRequest) -> InspectionResult:
        parts = normalize(request)
        requirements = self._requirements(request, parts)
        decision = self.router.decide(requirements, policy=request.policy)
        eligible = tuple(item.model_key for item in decision.ranked
                         if self.router.registry.get(item.model_key).provider in self.adapters
                         or requirements.modalities == ("text",))
        excluded = {item.model_key: item.reasons for item in decision.excluded}
        for item in decision.ranked:
            if item.model_key not in eligible:
                excluded[item.model_key] = ("no multimodal adapter is configured for this provider",)
        note = ("Media stay local unless you allow external disclosure for this request."
                if requirements.privacy == "local_only" else "Inputs may be sent to an eligible remote provider.")
        if not eligible:
            note += " No configured model supports these inputs."
        return InspectionResult(
            descriptors=tuple(item.descriptor for item in parts), modalities=tuple(
                dict.fromkeys(item.descriptor.modality for item in parts)) or ("text",),
            eligible_models=eligible, excluded=excluded,
            external_disclosure="allowed_by_owner" if request.allow_external_disclosure else "not_allowed", note=note)

    async def analyze(self, request: MultimodalRequest) -> MultimodalResult:
        parts = normalize(request)
        observations = tuple(item.descriptor for item in parts)
        requirements = self._requirements(request, parts)
        decision = self.router.decide(requirements, policy=request.policy)
        for candidate in (decision.selected, *decision.fallbacks):
            if candidate is None:
                continue
            model = self.router.registry.get(candidate)
            adapter = self.adapters.get(model.provider)
            if adapter is None:
                continue
            try:
                text = await asyncio.wait_for(adapter.analyze(request.prompt, parts), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                return MultimodalResult(status="failed", observations=observations, decision_id=decision.decision_id,
                                        reasons=("The multimodal model timed out.",),
                                        note="Observations are measured by JARVIS; no interpretation is available.")
            except Exception:  # noqa: BLE001 - sanitized
                return MultimodalResult(status="failed", observations=observations, decision_id=decision.decision_id,
                                        reasons=("The multimodal model failed.",),
                                        note="Observations are measured by JARVIS; no interpretation is available.")
            simulated = bool(getattr(adapter, "simulated", False)) or model.is_demo
            return MultimodalResult(
                status="succeeded", observations=observations, model_key=candidate, decision_id=decision.decision_id,
                interpretation=Interpretation(model_key=candidate, text=text[:20_000], simulated=simulated),
                note=("Observations are measured by JARVIS from the inputs. The interpretation is model output, "
                      "untrusted and possibly wrong" + (", and simulated by a mock adapter." if simulated else ".")))
        reasons = tuple(f"{item.model_key}: {'; '.join(item.reasons)}" for item in decision.excluded)
        return MultimodalResult(status="unsupported", observations=observations, decision_id=decision.decision_id,
                                reasons=reasons or ("No model with a multimodal adapter is eligible.",),
                                note="No configured model supports these inputs under the current privacy settings.")
