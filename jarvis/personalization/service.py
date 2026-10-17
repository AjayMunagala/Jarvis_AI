"""Personalization service: explicit preferences, controlled inference, safe application.

- Explicit preferences are validated against ``CATALOG``; nothing else can be stored.
- Inference only counts fixed, non-sensitive observation kinds (for example
  "asked for a shorter answer"). It creates a *proposal* after
  ``MIN_EVIDENCE`` consistent observations and never overrides an explicit
  preference; a proposal changes behavior only after the owner accepts it.
- ``apply_to_routing`` can tighten privacy and reorder eligible models; it can
  never loosen a request's privacy, re-enable remote or demo models a policy
  forbids, or add models. ``apply_to_presentation`` affects formatting only.
- Disabling stops all effects immediately; reset deletes everything.
"""

import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from jarvis.personalization.models import (
    SCOPES, EffectivePreferences, Observation, Preference, PreferenceProposal, PresentationDirectives, validate_value,
)
from jarvis.personalization.stores import StorageError
from jarvis.routing.models import RoutingPolicy, TaskRequirements

MIN_EVIDENCE = 3
_OBSERVATION_RULES = {
    "requested_shorter": ("response_detail", "brief"), "requested_longer": ("response_detail", "detailed"),
    "requested_bullets": ("output_format", "bullets"), "requested_table": ("output_format", "table"),
    "requested_prose": ("output_format", "prose"), "requested_examples": ("coding_explanation", "with_examples"),
    "requested_steps": ("coding_explanation", "step_by_step"), "requested_concise_code": ("coding_explanation", "concise"),
}


class PersonalizationError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PersonalizationService:
    def __init__(self, store):
        self.store = store

    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def _call(self, func, *args):
        try:
            return func(*args)
        except StorageError as exc:
            raise PersonalizationError(503, "Personalization storage is unavailable.") from exc

    @staticmethod
    def _scope(scope: str) -> str:
        if scope not in SCOPES:
            raise PersonalizationError(422, f"Unknown scope {scope!r}; use 'global' or a task type.")
        return scope

    # ------------------------------------------------------------ explicit

    def set_preference(self, owner_id: UUID, key: str, value, *, scope: str = "global") -> Preference:
        try:
            clean = validate_value(key, value)
        except ValueError as exc:
            raise PersonalizationError(422, str(exc)) from exc
        preference = Preference(owner_id=owner_id, key=key, scope=self._scope(scope), value=clean,
                                provenance="explicit", updated_at=_now())
        self._call(self.store.put_preference, preference)
        return preference

    def remove_preference(self, owner_id: UUID, key: str, *, scope: str = "global") -> None:
        if not self._call(self.store.delete_preference, owner_id, key, self._scope(scope)):
            raise PersonalizationError(404, "No such preference is set.")

    def preferences(self, owner_id: UUID) -> tuple[Preference, ...]:
        return self._call(self.store.preferences, owner_id)

    def enabled(self, owner_id: UUID) -> bool:
        return self._call(self.store.is_enabled, owner_id)

    def set_enabled(self, owner_id: UUID, enabled: bool) -> bool:
        self._call(self.store.set_enabled, owner_id, enabled, _now())
        return enabled

    def reset(self, owner_id: UUID) -> int:
        return self._call(self.store.reset, owner_id)

    def resolve(self, owner_id: UUID, task_type: str = "general") -> EffectivePreferences:
        if not self.enabled(owner_id):
            return EffectivePreferences(enabled=False, task_type=task_type, values={}, sources={})
        values, sources = {}, {}
        for item in sorted(self.preferences(owner_id), key=lambda pref: pref.scope != "global"):
            if item.scope in ("global", task_type):  # task-specific entries are applied last and win
                values[item.key] = item.value
                sources[item.key] = f"{item.provenance}:{item.scope}"
        return EffectivePreferences(enabled=True, task_type=task_type, values=values, sources=sources)

    # ----------------------------------------------------------- inference

    def propose_from_observations(self, owner_id: UUID, observations: Iterable[Observation]) -> tuple[PreferenceProposal, ...]:
        observations = list(observations)
        counts = Counter((item.kind, self._scope(item.scope)) for item in observations)
        explicit = {(item.key, item.scope) for item in self.preferences(owner_id) if item.provenance == "explicit"}
        pending = {(item.key, item.scope, item.value) for item in self._call(self.store.proposals, owner_id, "proposed")}
        created = []
        for (kind, scope), count in sorted(counts.items()):
            key, value = _OBSERVATION_RULES[kind]
            conflicting = sum(n for (other, other_scope), n in counts.items()
                              if other_scope == scope and _OBSERVATION_RULES[other][0] == key
                              and _OBSERVATION_RULES[other][1] != value)
            if count < MIN_EVIDENCE or conflicting >= count or (key, scope) in explicit or (key, scope, value) in pending:
                continue
            proposal = PreferenceProposal(
                proposal_id="PP-" + secrets.token_hex(8), owner_id=owner_id, key=key, scope=scope, value=value,
                confidence=round(count / (count + 2 + conflicting), 4), evidence_count=count,
                basis=f"{count} observation(s) of {kind.replace('_', ' ')} in {scope} tasks"
                      + (f" ({conflicting} contrary)" if conflicting else ""), created_at=_now())
            self._call(self.store.put_proposal, proposal)
            created.append(proposal)
        return tuple(created)

    def proposals(self, owner_id: UUID, status: str | None = None) -> tuple[PreferenceProposal, ...]:
        return self._call(self.store.proposals, owner_id, status)

    def decide(self, owner_id: UUID, proposal_id: str, *, accept: bool) -> PreferenceProposal:
        item = self._call(self.store.get_proposal, owner_id, proposal_id)
        if item is None:
            raise PersonalizationError(404, "The proposal was not found.")
        if item.status != "proposed":
            raise PersonalizationError(409, f"The proposal is already {item.status}.")
        decided = item.model_copy(update={"status": "accepted" if accept else "rejected", "decided_at": _now()})
        if accept:
            self._call(self.store.put_preference, Preference(
                owner_id=owner_id, key=item.key, scope=item.scope, value=item.value, provenance="inferred_confirmed",
                updated_at=_now()))
        self._call(self.store.put_proposal, decided)
        return decided

    # ---------------------------------------------------------- application

    @staticmethod
    def apply_to_routing(prefs: EffectivePreferences, policy: RoutingPolicy,
                         requirements: TaskRequirements) -> tuple[RoutingPolicy, TaskRequirements]:
        if not prefs.enabled:
            return policy, requirements
        privacy = prefs.values.get("privacy_routing")
        if privacy == "local_only":
            requirements = requirements.model_copy(update={"privacy": "local_only"})
        elif privacy == "prefer_local" and requirements.privacy == "any":
            requirements = requirements.model_copy(update={"privacy": "prefer_local"})
        update = {}
        providers = prefs.values.get("preferred_providers")
        if providers:
            update["preferred_providers"] = tuple(providers)
        tradeoff = prefs.values.get("latency_quality")
        if tradeoff in {"latency", "quality"}:
            weights = policy.weights.model_dump()
            weights[tradeoff] = min(10.0, weights[tradeoff] + 1.0)
            update["weights"] = policy.weights.model_copy(update=weights)
        # allow_remote, allow_demo and max_fallbacks are never touched.
        return (policy.model_copy(update=update) if update else policy), requirements

    @staticmethod
    def apply_to_presentation(prefs: EffectivePreferences) -> PresentationDirectives:
        if not prefs.enabled:
            return PresentationDirectives()
        return PresentationDirectives(**{key: prefs.values.get(key) for key in
                                         ("response_detail", "output_format", "language", "coding_explanation")})
