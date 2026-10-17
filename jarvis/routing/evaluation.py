"""Repeatable model evaluation: correctness, structure, latency, failures, cost.

A run is reproducible from (dataset id, version, sha256, model keys, repeats).
Correctness is checked by deterministic expectations, never by another model.
Summaries from demo/mock adapters are marked ``simulated`` and must not be
presented as real model quality. Cost is reported only when the descriptor has
operator-supplied pricing, and then only as an estimate from a conservative
token count.
"""

import asyncio
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Callable, Mapping

from pydantic import create_model

from jarvis.intelligence.base import ProviderError, TransientProviderError
from jarvis.routing.models import (
    CaseResult, EvaluationCase, EvaluationDataset, EvaluationReport, Expectation, ModelEvaluationSummary,
)
from jarvis.routing.registry import ModelRegistry
from jarvis.routing.requirements import estimate_tokens

DATASET_DIR = Path(__file__).with_name("datasets")
MAX_REPEATS = 5
_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def load_dataset(name_or_path: str | Path) -> tuple[EvaluationDataset, str]:
    path = Path(name_or_path)
    if not path.suffix:
        path = DATASET_DIR / f"{name_or_path}.json"
    raw = path.read_bytes()
    return EvaluationDataset.model_validate(json.loads(raw)), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _response_model(expected: Expectation):
    fields = {name: (_TYPES[kind], ...) for name, kind in expected.fields.items()}
    return create_model("EvaluationOutput", **fields) if fields else None


def check(expected: Expectation, content: str) -> tuple[bool, bool | None]:
    """Return (correct, structured_valid); structured_valid is None for text cases."""
    text = content.strip()
    if expected.kind == "exact":
        return text == expected.value, None
    if expected.kind == "contains_all":
        lowered = text.lower()
        return all(item.lower() in lowered for item in expected.values), None
    if expected.kind == "regex":
        return re.search(expected.value, text) is not None, None
    if expected.kind == "number":
        found = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        return any(abs(float(item) - expected.number) <= expected.tolerance for item in found), None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False, False
    if not isinstance(data, dict):
        return False, False
    valid = all(name in data and _type_ok(data[name], kind) for name, kind in expected.fields.items())
    correct = valid and all(data.get(name) == value for name, value in expected.field_values.items())
    return correct, valid


def _type_ok(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 3)


class EvaluationHarness:
    def __init__(self, registry: ModelRegistry, adapters: Mapping[str, object], *,
                 timeout_seconds: float = 60.0, clock: Callable[[], float] = perf_counter):
        self.registry = registry
        self.adapters = dict(adapters)
        self.timeout_seconds = timeout_seconds
        self._clock = clock

    async def _run_case(self, case: EvaluationCase, model_key: str, repeat: int) -> tuple[CaseResult, int]:
        model = self.registry.get(model_key)
        adapter = self.adapters.get(model.provider)
        structured = case.expected.kind == "json_fields"
        started = self._clock()
        if adapter is None:
            return CaseResult(case_id=case.case_id, model_key=model_key, repeat=repeat, completed=False, correct=False,
                              structured_valid=False if structured else None, latency_ms=0,
                              error_code="unavailable"), 0
        try:
            content = await asyncio.wait_for(adapter.generate_reply(
                case.prompt, system_instruction=case.system_instruction,
                response_model=_response_model(case.expected) if structured and model.structured_output else None,
            ), timeout=self.timeout_seconds)
            error = None
        except asyncio.TimeoutError:
            content, error = None, "timeout"
        except TransientProviderError:
            content, error = None, "transient_provider_error"
        except ProviderError:
            content, error = None, "provider_error"
        except Exception:  # noqa: BLE001 - sanitized
            content, error = None, "adapter_error"
        latency = round(max(0.0, (self._clock() - started) * 1000), 3)
        if content is None:
            return CaseResult(case_id=case.case_id, model_key=model_key, repeat=repeat, completed=False, correct=False,
                              structured_valid=False if structured else None, latency_ms=latency,
                              error_code=error), 0
        correct, valid = check(case.expected, content)
        return CaseResult(case_id=case.case_id, model_key=model_key, repeat=repeat, completed=True, correct=correct,
                          structured_valid=valid, latency_ms=latency), estimate_tokens(case.prompt) + estimate_tokens(content)

    async def run(self, dataset: EvaluationDataset, dataset_sha256: str, model_keys: tuple[str, ...], *,
                  repeats: int = 1) -> EvaluationReport:
        if not 1 <= repeats <= MAX_REPEATS:
            raise ValueError(f"repeats must be between 1 and {MAX_REPEATS}")
        unknown = [key for key in model_keys if self.registry.get(key) is None]
        if unknown or not model_keys:
            raise ValueError("unknown or missing model keys")
        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        results: list[CaseResult] = []
        summaries = []
        for model_key in model_keys:
            model = self.registry.get(model_key)
            own: list[CaseResult] = []
            tokens = 0
            for repeat in range(1, repeats + 1):
                for case in dataset.cases:  # sequential: bounded and deterministic
                    result, used = await self._run_case(case, model_key, repeat)
                    own.append(result)
                    tokens += used
            results.extend(own)
            runs = len(own)
            structured = [item.structured_valid for item in own if item.structured_valid is not None]
            latencies = [item.latency_ms for item in own if item.completed]
            if model.pricing is not None:
                rate = (model.pricing.input_per_million_tokens + model.pricing.output_per_million_tokens) / 2
                cost, cost_note = round(tokens * rate / 1_000_000, 6), (
                    f"Estimated from ~{tokens} tokens and operator pricing ({model.pricing.source}, {model.pricing.as_of}).")
            else:
                cost, cost_note = None, "Not measurable: no operator-supplied pricing for this model."
            summaries.append(ModelEvaluationSummary(
                model_key=model_key, runs=runs,
                completion_rate=round(sum(item.completed for item in own) / runs, 6),
                correctness_rate=round(sum(item.correct for item in own) / runs, 6),
                structured_validity_rate=round(sum(structured) / len(structured), 6) if structured else None,
                failure_rate=round(sum(not item.completed for item in own) / runs, 6),
                p50_latency_ms=round(median(latencies), 3) if latencies else None,
                p95_latency_ms=_percentile(latencies, 0.95), estimated_cost_usd=cost, cost_note=cost_note,
                simulated=model.is_demo or getattr(self.adapters.get(model.provider), "simulated", False),
            ))
        notes = ("Correctness uses deterministic expectations; no model grades another model.",)
        if any(item.simulated for item in summaries):
            notes += ("Simulated summaries come from demo or scripted adapters and do not measure real model quality.",)
        return EvaluationReport(report_id="ER-" + secrets.token_hex(8), dataset_id=dataset.dataset_id,
                                dataset_version=dataset.version, dataset_sha256=dataset_sha256, started_at=started_at,
                                repeats=repeats, summaries=tuple(summaries), results=tuple(results), notes=notes)
