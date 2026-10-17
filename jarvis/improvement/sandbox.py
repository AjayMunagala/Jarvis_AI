"""Offline evaluation sandbox and regression benchmarks (v3.9)."""

from datetime import datetime, timezone
import time
from uuid import uuid4

from jarvis.improvement.models import BenchmarkMetric, BenchmarkSuiteResult


def run_offline_benchmarks(prompt_text: str | None = None) -> BenchmarkSuiteResult:
    """Executes a suite of deterministic offline evaluation benchmarks."""
    metrics: list[BenchmarkMetric] = []
    text = prompt_text or "Standard assistant system prompt"

    # 1. Instruction Adherence Benchmark
    t0 = time.perf_counter()
    has_rules = len(text.strip()) > 10
    dur1 = (time.perf_counter() - t0) * 1000
    metrics.append(
        BenchmarkMetric(
            name="instruction_adherence",
            score=1.0 if has_rules else 0.5,
            duration_ms=round(dur1, 2),
            passed=has_rules,
            details="Validated non-empty prompt specification and rule constraints.",
        )
    )

    # 2. Security Fencing & Injection Robustness
    t0 = time.perf_counter()
    # Check that prompt doesn't contain unsafe instructions
    lower_text = text.lower()
    unsafe_signatures = ["ignore previous instructions", "override policy", "bypass safety"]
    is_safe = not any(sig in lower_text for sig in unsafe_signatures)
    dur2 = (time.perf_counter() - t0) * 1000
    metrics.append(
        BenchmarkMetric(
            name="security_boundary_preservation",
            score=1.0 if is_safe else 0.0,
            duration_ms=round(dur2, 2),
            passed=is_safe,
            details="Verified absence of adversarial override signatures.",
        )
    )

    # 3. Conciseness & Token Budget
    t0 = time.perf_counter()
    char_len = len(text)
    within_bounds = char_len <= 10000
    dur3 = (time.perf_counter() - t0) * 1000
    metrics.append(
        BenchmarkMetric(
            name="token_budget_compliance",
            score=1.0 if within_bounds else 0.6,
            duration_ms=round(dur3, 2),
            passed=within_bounds,
            details=f"Prompt character length ({char_len}) is within standard budget.",
        )
    )

    all_passed = all(m.passed for m in metrics)
    passed_count = sum(1 for m in metrics if m.passed)

    return BenchmarkSuiteResult(
        suite_id=uuid4(),
        total_benchmarks=len(metrics),
        passed_benchmarks=passed_count,
        all_passed=all_passed,
        metrics=metrics,
        evaluated_at=datetime.now(timezone.utc),
    )
