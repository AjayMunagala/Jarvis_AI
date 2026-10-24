"""Model routing: hard eligibility rules, explicit scoring, bounded fallback.

Order of operations:

1. Eligibility (hard rules, every exclusion is explained): adapter available,
   not disabled; demo models only if the policy allows them; remote models only
   if both the policy and the privacy requirement allow them; every requested
   modality supported; known context capacity at least the requirement
   (unknown capacity never satisfies a requirement); structured output and tool
   calling only when the descriptor states support.
2. Scoring of eligible models with the policy's explicit weights. Quality and
   latency come only from evaluation evidence an operator accepted; missing
   evidence scores neutral (0.5) and is noted. Demo models rank last.
3. Execution tries the selected model, then at most ``max_fallbacks`` further
   models **from the same eligible list**, so a fallback can never relax
   privacy, modality or permission constraints.

The router selects among models; it never grants permissions. Observability
records contain hashes and lengths, never prompt or reply text.
"""

import asyncio
import hashlib
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Mapping

from pydantic import BaseModel

from jarvis.intelligence.base import ProviderError, TransientProviderError
from jarvis.routing.models import (
    Attempt, Candidate, EvaluationReport, Exclusion, ModelDescriptor, ModelEvaluationSummary, PolicyConfig,
    RoutedReply, RoutingDecision, RoutingPolicy, RoutingRecord, TaskRequirements,
)
from jarvis.routing.registry import ModelRegistry, policy_by_name

NEUTRAL = 0.5
DEMO_PENALTY = 0.1
PREFERENCE_BONUS = 0.25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ModelRouter:
    def __init__(self, registry: ModelRegistry, policies: PolicyConfig, adapters: Mapping[str, object], *,
                 disabled: frozenset[str] = frozenset(), attempt_timeout_seconds: float = 60.0,
                 clock: Callable[[], float] = perf_counter, log_capacity: int = 500):
        self.registry = registry
        self.policies = policies
        self.adapters = dict(adapters)
        self.disabled = disabled
        self.attempt_timeout_seconds = attempt_timeout_seconds
        self._clock = clock
        self._evidence: dict[str, ModelEvaluationSummary] = {}
        self._log: deque[RoutingRecord] = deque(maxlen=log_capacity)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ evidence

    def accept_evidence(self, report: EvaluationReport, *, allow_simulated: bool = False) -> tuple[str, ...]:
        """Use an evaluation report's summaries as quality/latency evidence.

        Policies never change; only the evidence used for scoring does, and only
        through this explicit call. Simulated (mock-provider) summaries are
        refused unless ``allow_simulated`` is set, so mock runs cannot steer
        real routing.
        """
        accepted = []
        with self._lock:
            for summary in report.summaries:
                if summary.simulated and not allow_simulated:
                    continue
                if self.registry.get(summary.model_key) is None or summary.runs == 0:
                    continue
                self._evidence[summary.model_key] = summary
                accepted.append(summary.model_key)
        return tuple(accepted)

    def evidence(self) -> dict[str, ModelEvaluationSummary]:
        with self._lock:
            return dict(self._evidence)

    # ------------------------------------------------------------ decision

    def policy(self, name: str | None = None) -> RoutingPolicy:
        return policy_by_name(self.policies, name)

    def _exclusions(self, model: ModelDescriptor, req: TaskRequirements, policy: RoutingPolicy) -> list[str]:
        reasons = []
        if model.model_key in self.disabled:
            reasons.append("disabled by configuration")
        if model.provider not in self.adapters:
            reasons.append(f"no {model.provider!r} adapter is configured")
        if model.is_demo and not policy.allow_demo:
            reasons.append("demo models are not allowed by this policy")
        if model.locality == "remote" and (req.privacy == "local_only" or not policy.allow_remote):
            reasons.append("remote execution is not permitted for this request")
        missing = [item for item in req.modalities if item not in model.input_modalities]
        if missing:
            reasons.append(f"unsupported input modality: {', '.join(missing)}")
        if req.task_type not in model.task_types and "general" not in model.task_types:
            reasons.append(f"not declared for task type {req.task_type!r}")
        if req.min_context_tokens is not None and (model.context_tokens is None
                                                   or model.context_tokens < req.min_context_tokens):
            reasons.append("context capacity is unknown" if model.context_tokens is None
                           else f"context capacity {model.context_tokens} < required {req.min_context_tokens}")
        if req.needs_structured_output and model.structured_output is not True:
            reasons.append("structured output support is not declared")
        if req.needs_tool_calling and model.tool_calling is not True:
            reasons.append("tool calling support is not declared")
        evidence = self._evidence.get(model.model_key)
        if (req.max_latency_ms is not None and evidence is not None and evidence.p50_latency_ms is not None
                and evidence.p50_latency_ms > req.max_latency_ms):
            reasons.append(f"measured p50 latency {evidence.p50_latency_ms:.0f} ms exceeds {req.max_latency_ms} ms")
        return reasons

    def _score(self, model: ModelDescriptor, req: TaskRequirements, policy: RoutingPolicy) -> Candidate:
        notes = []
        suitability = 1.0 if req.task_type in model.task_types else 0.5
        if suitability < 1:
            notes.append("generic model; no declared specialization for this task")
        evidence = self._evidence.get(model.model_key)
        if evidence is not None:
            quality = evidence.correctness_rate
        else:
            quality = NEUTRAL
            notes.append("quality unmeasured")
        if evidence is not None and evidence.p50_latency_ms is not None:
            latency = 1.0 / (1.0 + evidence.p50_latency_ms / 1000.0)
        else:
            latency = NEUTRAL
            notes.append("latency unmeasured")
        if model.pricing is not None:
            cost = 1.0 / (1.0 + (model.pricing.input_per_million_tokens + model.pricing.output_per_million_tokens) / 10.0)
        else:
            cost = NEUTRAL
            notes.append("cost unknown")
        locality = 1.0 if model.locality == "local" else 0.0
        weights = policy.weights.model_dump()
        if req.privacy == "prefer_local":
            weights["locality"] += 1.0
        factors = {"suitability": suitability, "quality": quality, "latency": latency, "cost": cost,
                   "locality": locality}
        total = sum(weights.values())
        score = sum(weights[key] * factors[key] for key in factors) / total
        if model.provider in policy.preferred_providers:
            score += PREFERENCE_BONUS
            notes.append("preferred provider")
        if model.is_demo:
            score *= DEMO_PENALTY
            notes.append("demo model: ranked last")
        return Candidate(model_key=model.model_key, score=round(score, 6),
                         factors={key: round(value, 6) for key, value in factors.items()}, notes=tuple(notes))

    def decide(self, requirements: TaskRequirements, *, policy: RoutingPolicy | str | None = None) -> RoutingDecision:
        chosen = policy if isinstance(policy, RoutingPolicy) else self.policy(policy)
        ranked, excluded = [], []
        for model in self.registry.models():
            reasons = self._exclusions(model, requirements, chosen)
            if reasons:
                excluded.append(Exclusion(model_key=model.model_key, reasons=tuple(reasons)))
            else:
                ranked.append(self._score(model, requirements, chosen))
        ranked.sort(key=lambda item: (-item.score, item.model_key))
        selected = ranked[0].model_key if ranked else None
        demo = bool(selected and self.registry.get(selected).is_demo)
        if selected is None:
            explanation = "No registered model satisfies the requirements and policy."
        else:
            explanation = (f"Selected {selected} under policy {chosen.name!r} from {len(ranked)} eligible model(s)"
                           + (" (demo provider: replies are not AI-generated)." if demo else "."))
        return RoutingDecision(
            decision_id="RD-" + secrets.token_hex(8), policy=chosen.name, requirements=requirements,
            selected=selected, fallbacks=tuple(item.model_key for item in ranked[1:1 + chosen.max_fallbacks]),
            ranked=tuple(ranked), excluded=tuple(excluded), demo=demo, explanation=explanation,
        )

    # ----------------------------------------------------------- execution

    async def execute(self, decision: RoutingDecision, message: str, *, system_instruction: str | None = None,
                      response_model: type[BaseModel] | None = None) -> RoutedReply:
        if decision.selected is None:
            self._record(decision, (), "no_eligible_model", message)
            return RoutedReply(decision_id=decision.decision_id, model_key=None, content=None, attempts=(),
                               status="no_eligible_model")
        attempts: list[Attempt] = []
        for model_key in (decision.selected, *decision.fallbacks):
            model = self.registry.get(model_key)
            adapter = self.adapters.get(model.provider) if model else None
            if adapter is None:
                attempts.append(Attempt(model_key=model_key, outcome="failed", duration_ms=0, error_code="unavailable"))
                continue
            started = self._clock()
            try:
                content = await asyncio.wait_for(
                    adapter.generate_reply(message, system_instruction=system_instruction,
                                           response_model=response_model if model.structured_output else None),
                    timeout=self.attempt_timeout_seconds)
            except asyncio.TimeoutError:
                attempts.append(Attempt(model_key=model_key, outcome="timeout", duration_ms=self._ms(started),
                                        error_code="timeout"))
                continue
            except TransientProviderError:
                attempts.append(Attempt(model_key=model_key, outcome="failed", duration_ms=self._ms(started),
                                        error_code="transient_provider_error"))
                continue
            except ProviderError:
                attempts.append(Attempt(model_key=model_key, outcome="failed", duration_ms=self._ms(started),
                                        error_code="provider_error"))
                continue
            except Exception:  # noqa: BLE001 - adapters are isolated; failures are sanitized
                attempts.append(Attempt(model_key=model_key, outcome="failed", duration_ms=self._ms(started),
                                        error_code="adapter_error"))
                continue
            attempts.append(Attempt(model_key=model_key, outcome="succeeded", duration_ms=self._ms(started)))
            self._record(decision, tuple(attempts), "succeeded", message)
            return RoutedReply(decision_id=decision.decision_id, model_key=model_key, content=content,
                               attempts=tuple(attempts), status="succeeded", demo=model.is_demo)
        self._record(decision, tuple(attempts), "failed", message)
        return RoutedReply(decision_id=decision.decision_id, model_key=None, content=None, attempts=tuple(attempts),
                           status="failed")

    def _ms(self, started: float) -> float:
        return round(max(0.0, (self._clock() - started) * 1000), 3)

    def _record(self, decision: RoutingDecision, attempts, status: str, message: str | None) -> None:
        record = RoutingRecord(
            decision_id=decision.decision_id, occurred_at=_now(), task_type=decision.requirements.task_type,
            policy=decision.policy, selected=decision.selected, attempts=attempts, status=status,
            prompt_sha256=hashlib.sha256(message.encode("utf-8", "replace")).hexdigest() if message else None,
            prompt_chars=len(message) if message else None,
        )
        with self._lock:
            self._log.append(record)

    def recent(self, limit: int = 50) -> tuple[RoutingRecord, ...]:
        with self._lock:
            return tuple(list(self._log)[-limit:][::-1])
