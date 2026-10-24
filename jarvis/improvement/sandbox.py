"""Offline evaluation and regression detection for prompt changes (v3.9, v4.9).

These are deterministic, offline static checks of a prompt's text. They do
not run a language model, so they cannot show that a prompt makes answers
better, and they cannot detect negation or contradiction in meaning. They
detect regressions relative to the content a proposal would replace:

- ``security_boundary_preservation``: occurrences of a fixed list of override
  phrases (for example "ignore previous instructions"), matched after Unicode
  normalisation (NFKC, case folding, invisible characters removed, whitespace
  collapsed). It is a phrase list, not general injection detection. A
  defensive prompt that already quotes such a phrase is compared by count,
  so it can still be edited.
- ``baseline_rules_preserved``: every rule line of the current content (a
  line with "must", "never", "always", "do not", "only", "secure",
  "security", "privacy") must still exist as a whole line of the new content.
  Rewording a rule line is a rule change: it is allowed only when the owner
  acknowledges it, and the approval request then lists the changed lines.
- ``instruction_adherence`` and ``token_budget_compliance``: trivial size
  checks.

A proposal is *acceptable* when no score is lower than the current content's.
A check that already failed for the current content is reported as a warning,
so it never freezes a target.
"""

from datetime import datetime, timezone
import re
import time
import unicodedata
from typing import Any
from uuid import uuid4

from jarvis.improvement.models import BenchmarkMetric, BenchmarkSuiteResult

_RULE_LINE = re.compile(r"\b(must|never|always|do not|don't|only|secure|security|privacy)\b", re.IGNORECASE)
_OVERRIDE_PHRASES = tuple(re.compile(pattern) for pattern in (
    r"\bignore (?:all |any )?(?:of )?(?:the |your )?(?:previous|prior|earlier|above|preceding) (?:instructions|rules|prompts?)\b",
    r"\bdisregard (?:all |any )?(?:of )?(?:the |your )?(?:previous|prior|earlier|above|system|preceding)\b",
    r"\bforget (?:all |any )?(?:of )?(?:the |your )?(?:previous|prior|earlier|above) (?:instructions|rules)\b",
    r"\boverride (?:the |your |any )?(?:system |safety )?polic",
    r"\bbypass (?:the |your |any )?(?:safety|security|guardrails?)\b",
    r"\byou have no (?:restrictions|rules|limits)\b",
))
RULE_CHECK = "baseline_rules_preserved"


def normalize(text: str, *, invisible: str = "") -> str:
    """NFKC, case folded, invisible format characters replaced by ``invisible``, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    visible = "".join(invisible if unicodedata.category(char) == "Cf" else char for char in folded)
    return " ".join(visible.split())


def override_phrase_count(text: str) -> int:
    # An invisible character may split a word ("ig​nore") or stand in for a space between words, so both
    # readings are checked and the higher count is used.
    return max(sum(len(pattern.findall(normalize(text, invisible=replacement))) for pattern in _OVERRIDE_PHRASES)
               for replacement in ("", " "))


def rule_lines(text: str) -> list[str]:
    return [normalize(line) for line in text.splitlines() if _RULE_LINE.search(line) and normalize(line)]


def _metric(name: str, check, details: str) -> BenchmarkMetric:
    started = time.perf_counter()
    score, passed = check()
    return BenchmarkMetric(name=name, score=round(score, 4), passed=passed,
                           duration_ms=round((time.perf_counter() - started) * 1000, 3), details=details)


def run_offline_benchmarks(prompt_text: str | None = None, *, baseline_text: str | None = None) -> BenchmarkSuiteResult:
    """Run the static checks on ``prompt_text``; with ``baseline_text`` also check that its rule lines are kept."""
    text = prompt_text or "Standard assistant system prompt"
    overrides = override_phrase_count(text)
    metrics = [
        _metric("instruction_adherence", lambda: ((1.0, True) if len(text.strip()) > 10 else (0.5, False)),
                "The prompt states non-trivial instructions (more than 10 characters)."),
        _metric("security_boundary_preservation", lambda: (1.0 / (1 + overrides), overrides == 0),
                f"{overrides} occurrence(s) of the fixed override-phrase list."),
        _metric("token_budget_compliance",
                lambda: ((1.0, True) if len(text) <= 10000 else (round(10000 / len(text), 4), False)),
                f"Prompt length {len(text)} characters (limit 10000)."),
    ]
    if baseline_text is not None:
        rules = rule_lines(baseline_text)
        candidate_lines = {normalize(line) for line in text.splitlines()}

        def kept():
            if not rules:
                return 1.0, True
            share = sum(1 for rule in rules if rule in candidate_lines) / len(rules)
            return share, share == 1.0

        metrics.append(_metric(RULE_CHECK, kept,
                               f"{len(rules)} rule line(s) of the current prompt must remain as whole lines."))
    passed = sum(1 for metric in metrics if metric.passed)
    return BenchmarkSuiteResult(suite_id=uuid4(), total_benchmarks=len(metrics), passed_benchmarks=passed,
                                all_passed=passed == len(metrics), metrics=metrics,
                                evaluated_at=datetime.now(timezone.utc))


def compare_with_baseline(baseline_text: str, candidate_text: str) -> dict[str, Any]:
    """Evaluate a candidate against the content it would replace.

    ``acceptable`` is True when nothing regressed. ``rule_changes_only`` is True when the only regression is a
    changed or removed rule line, which the owner may acknowledge explicitly.
    """
    baseline = run_offline_benchmarks(baseline_text)
    candidate = run_offline_benchmarks(candidate_text, baseline_text=baseline_text)
    before = {metric.name: metric for metric in baseline.metrics}
    regressions, warnings = [], []
    for metric in candidate.metrics:
        old = before.get(metric.name)
        if old is not None:
            if metric.score < old.score or (old.passed and not metric.passed):
                regressions.append(f"{metric.name}: {old.score} -> {metric.score}")
            elif not metric.passed:
                warnings.append(f"{metric.name}: still failing ({metric.details})")
        elif not metric.passed:
            regressions.append(f"{metric.name}: {metric.score} ({metric.details})")
    candidate_lines = {normalize(line) for line in candidate_text.splitlines()}
    changed_rules = [rule for rule in rule_lines(baseline_text) if rule not in candidate_lines]
    return {
        "method": "offline static checks (no model is run; negation and meaning are not checked)",
        "baseline": {metric.name: metric.score for metric in baseline.metrics},
        "candidate": {metric.name: metric.score for metric in candidate.metrics},
        "regressions": regressions,
        "warnings": warnings,
        "changed_rules": changed_rules[:20],
        "rule_changes_only": bool(regressions) and all(item.startswith(RULE_CHECK) for item in regressions),
        "acceptable": not regressions,
        "evaluated_at": candidate.evaluated_at.isoformat(timespec="seconds"),
    }
