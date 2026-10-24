"""Offline evaluation and regression detection for prompt changes (v3.9, v4.9).

These are deterministic, offline static checks of a prompt's text. They do
not run a language model, so they cannot show that a prompt makes answers
better; they can show that a change does not break obvious requirements.
v4.9 adds a comparison: every proposal is evaluated against the content it
would replace, and a change that lowers any score (a *regression*), fails a
check that used to pass, or drops a rule line of the current prompt (lines
with "must", "never", "always", "do not", "only", "secure", "privacy") is
refused before it can be approved.
"""

from datetime import datetime, timezone
import re
import time
from typing import Any
from uuid import uuid4

from jarvis.improvement.models import BenchmarkMetric, BenchmarkSuiteResult

_RULE_LINE = re.compile(r"\b(must|never|always|do not|don't|only|secure|security|privacy)\b", re.IGNORECASE)


def _metric(name: str, check, details: str) -> BenchmarkMetric:
    started = time.perf_counter()
    score, passed = check()
    return BenchmarkMetric(name=name, score=round(score, 4), passed=passed,
                           duration_ms=round((time.perf_counter() - started) * 1000, 3), details=details)


def _rule_lines(text: str) -> list[str]:
    return [" ".join(line.lower().split()) for line in text.splitlines() if _RULE_LINE.search(line)]


def run_offline_benchmarks(prompt_text: str | None = None, *, baseline_text: str | None = None) -> BenchmarkSuiteResult:
    """Run the static checks on ``prompt_text``; with ``baseline_text`` also check that its rules are kept."""
    text = prompt_text or "Standard assistant system prompt"
    lower = text.lower()
    metrics = [
        _metric("instruction_adherence", lambda: ((1.0, True) if len(text.strip()) > 10 else (0.5, False)),
                "The prompt states non-trivial instructions (more than 10 characters)."),
        _metric("security_boundary_preservation",
                lambda: ((0.0, False) if any(sig in lower for sig in
                                             ("ignore previous instructions", "override policy", "bypass safety",
                                              "ignore all previous", "disregard the system"))
                         else (1.0, True)),
                "No adversarial override phrases."),
        _metric("token_budget_compliance", lambda: ((1.0, True) if len(text) <= 10000 else (0.6, False)),
                f"Prompt length {len(text)} characters (limit 10000)."),
    ]
    if baseline_text is not None:
        rules = _rule_lines(baseline_text)
        kept_text = " ".join(lower.split())

        def kept():
            if not rules:
                return 1.0, True
            share = sum(1 for rule in rules if rule in kept_text) / len(rules)
            return share, share == 1.0

        metrics.append(_metric("baseline_rules_preserved", kept,
                               f"{len(rules)} rule line(s) of the current prompt must still be present."))
    passed = sum(1 for metric in metrics if metric.passed)
    return BenchmarkSuiteResult(suite_id=uuid4(), total_benchmarks=len(metrics), passed_benchmarks=passed,
                                all_passed=passed == len(metrics), metrics=metrics,
                                evaluated_at=datetime.now(timezone.utc))


def compare_with_baseline(baseline_text: str, candidate_text: str) -> dict[str, Any]:
    """Evaluate a candidate against the content it would replace; ``acceptable`` is False on any regression."""
    baseline = run_offline_benchmarks(baseline_text)
    candidate = run_offline_benchmarks(candidate_text, baseline_text=baseline_text)
    before = {metric.name: metric for metric in baseline.metrics}
    regressions = []
    for metric in candidate.metrics:
        old = before.get(metric.name)
        if old is not None and (metric.score < old.score or (old.passed and not metric.passed)):
            regressions.append(f"{metric.name}: {old.score} -> {metric.score}")
        elif old is None and not metric.passed:
            regressions.append(f"{metric.name}: {metric.score} ({metric.details})")
    return {
        "method": "offline static checks (no model is run)",
        "baseline": {metric.name: metric.score for metric in baseline.metrics},
        "candidate": {metric.name: metric.score for metric in candidate.metrics},
        "candidate_all_passed": candidate.all_passed,
        "regressions": regressions,
        "acceptable": candidate.all_passed and not regressions,
        "evaluated_at": candidate.evaluated_at.isoformat(timespec="seconds"),
    }
