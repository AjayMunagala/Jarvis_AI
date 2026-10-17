"""Deterministic plan-quality evaluation and the structural baseline.

``evaluate_plan`` checks seven properties:

- goal_coverage: significant goal terms are addressed by the steps;
- constraints: step limit, forbidden action kinds/tools, required steps;
- dependencies: known, not self-referential, acyclic, unique step ids;
- information: every step input is available up front or produced by a step
  it (transitively) depends on;
- redundancy: duplicate steps, and steps whose outputs nobody uses while
  other steps exist after them;
- completion: each step has an observable completion criterion;
- safety: consequential steps must be marked ``requires_approval`` and are
  never treated as executable by this module.

``structural_baseline`` reproduces what the existing ``ExecutionPlan``
contract validates (unique ids, known non-self dependencies, acyclicity) so
improvements can be measured against it on the same dataset.
"""

import json
from pathlib import Path

from jarvis.memory.query import extract_query_terms, tokenize
from jarvis.reasoning.models import (
    CHECKS, Finding, PlanCandidate, PlanConstraints, PlanDataset, PlanEvaluation, PlanStepSpec, UncertaintyAssessment,
)

DATASET_DIR = Path(__file__).with_name("datasets")
_VAGUE = {"done", "complete", "completed", "finished", "ok", "okay", "as needed", "when ready", "tbd", "n/a", "success",
          "it works", "looks good"}
ERROR_WEIGHT, WARNING_WEIGHT = 0.25, 0.08


def _cycle(steps: dict[str, PlanStepSpec]) -> list[str]:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        path.append(node)
        for dep in steps[node].depends_on:
            if dep not in steps or dep == node:
                continue
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return []

    for node in steps:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return []


def _ancestors(steps: dict[str, PlanStepSpec], step_id: str) -> set[str]:
    seen, stack = set(), list(steps[step_id].depends_on)
    while stack:
        node = stack.pop()
        if node in seen or node not in steps:
            continue
        seen.add(node)
        stack.extend(steps[node].depends_on)
    return seen


def evaluate_plan(plan: PlanCandidate) -> PlanEvaluation:
    findings: list[Finding] = []
    ids = [step.step_id for step in plan.steps]
    steps = {step.step_id: step for step in plan.steps}
    constraints: PlanConstraints = plan.constraints

    # dependencies
    if len(ids) != len(steps):
        findings.append(Finding(check="dependencies", severity="error", message="Step identifiers must be unique."))
    for step in plan.steps:
        for dep in step.depends_on:
            if dep == step.step_id:
                findings.append(Finding(check="dependencies", severity="error", step_id=step.step_id,
                                        message="A step cannot depend on itself."))
            elif dep not in steps:
                findings.append(Finding(check="dependencies", severity="error", step_id=step.step_id,
                                        message=f"Unknown dependency {dep!r}."))
    cycle = _cycle(steps)
    if cycle:
        findings.append(Finding(check="dependencies", severity="error",
                                message="Dependency cycle: " + " -> ".join(cycle)))

    # goal coverage
    goal_terms = extract_query_terms(plan.goal)
    step_text = " ".join(f"{s.description} {' '.join(s.outputs)}" for s in plan.steps)
    tokens = set(tokenize(step_text.replace("_", " ")))
    covered = [term for term in goal_terms if any(token.startswith(term) for token in tokens)]
    coverage = len(covered) / len(goal_terms) if goal_terms else 1.0
    if coverage < 0.25:
        findings.append(Finding(check="goal_coverage", severity="error",
                                message=f"The steps address {len(covered)} of {len(goal_terms)} goal terms."))
    elif coverage < 0.5:
        findings.append(Finding(check="goal_coverage", severity="warning",
                                message=f"The steps address only {len(covered)} of {len(goal_terms)} goal terms."))

    # constraints
    if constraints.max_steps is not None and len(plan.steps) > constraints.max_steps:
        findings.append(Finding(check="constraints", severity="error",
                                message=f"{len(plan.steps)} steps exceed the limit of {constraints.max_steps}."))
    for step in plan.steps:
        if step.action_kind in constraints.forbidden_action_kinds:
            findings.append(Finding(check="constraints", severity="error", step_id=step.step_id,
                                    message=f"Action kind {step.action_kind!r} is not allowed."))
        if step.tool_id and step.tool_id in constraints.forbidden_tools:
            findings.append(Finding(check="constraints", severity="error", step_id=step.step_id,
                                    message=f"Tool {step.tool_id!r} is not allowed."))
    for term in constraints.required_step_terms:
        if not any(term.lower() in step.description.lower() for step in plan.steps):
            findings.append(Finding(check="constraints", severity="error",
                                    message=f"A required step about {term!r} is missing."))

    # information sufficiency
    available = set(constraints.available_inputs)
    for step in plan.steps:
        produced = {name for anc in _ancestors(steps, step.step_id) for name in steps[anc].outputs}
        missing = [name for name in step.inputs if name not in available and name not in produced]
        if missing:
            findings.append(Finding(check="information", severity="error", step_id=step.step_id,
                                    message="Missing inputs (not provided and not produced by a prerequisite step): "
                                            + ", ".join(missing)))

    # redundancy
    seen_descriptions: dict[str, str] = {}
    consumed = {name for step in plan.steps for name in step.inputs}
    dependents = {dep for step in plan.steps for dep in step.depends_on}
    for index, step in enumerate(plan.steps):
        normalized = " ".join(tokenize(step.description))
        if normalized in seen_descriptions:
            findings.append(Finding(check="redundancy", severity="warning", step_id=step.step_id,
                                    message=f"Duplicates step {seen_descriptions[normalized]!r}."))
        seen_descriptions.setdefault(normalized, step.step_id)
        is_last = index == len(plan.steps) - 1
        if (not is_last and step.step_id not in dependents and step.outputs
                and not any(name in consumed for name in step.outputs) and step.action_kind != "communication"):
            findings.append(Finding(check="redundancy", severity="warning", step_id=step.step_id,
                                    message="Its outputs are never used and no later step depends on it."))

    # completion criteria
    for step in plan.steps:
        criteria = " ".join(step.completion_criteria.lower().split()).strip(" .!")
        if not criteria:
            findings.append(Finding(check="completion", severity="warning", step_id=step.step_id,
                                    message="No completion criterion."))
        elif criteria in _VAGUE or len(criteria) < 12:
            findings.append(Finding(check="completion", severity="warning", step_id=step.step_id,
                                    message=f"Completion criterion {step.completion_criteria!r} is not observable."))

    # safety
    for step in plan.steps:
        if step.action_kind == "consequential" and not step.requires_approval:
            findings.append(Finding(check="safety", severity="error", step_id=step.step_id,
                                    message="Consequential steps must require owner approval through the approval gateway."))
        elif step.action_kind == "consequential":
            findings.append(Finding(check="safety", severity="info", step_id=step.step_id,
                                    message="Consequential step: it can only be proposed for approval; JARVIS does not execute it."))

    failed = {item.check for item in findings if item.severity in {"error", "warning"}}
    errors = sum(item.severity == "error" for item in findings)
    warnings = sum(item.severity == "warning" for item in findings)
    score = max(0.0, round(1 - errors * ERROR_WEIGHT - warnings * WARNING_WEIGHT, 4))
    if any(item.check == "safety" and item.severity == "error" for item in findings):
        verdict = "reject"
    elif errors or warnings >= 3:
        verdict = "needs_revision"
    else:
        verdict = "pass"
    missing = tuple(item.message for item in findings if item.check == "information")
    level = "high" if missing or coverage < 0.5 else ("medium" if plan.assumptions or warnings else "low")
    notes = tuple(f"Assumption: {item}" for item in plan.assumptions)
    return PlanEvaluation(verdict=verdict, score=score, checks={name: name not in failed for name in CHECKS},
                          findings=tuple(findings), evaluator="plan-quality-v1",
                          uncertainty=UncertaintyAssessment(level=level, missing=missing, notes=notes))


def structural_baseline(plan: PlanCandidate) -> str:
    """What the existing ExecutionPlan contract checks: structure only."""
    steps = {step.step_id: step for step in plan.steps}
    if len(steps) != len(plan.steps):
        return "needs_revision"
    if any(dep == step.step_id or dep not in steps for step in plan.steps for dep in step.depends_on):
        return "needs_revision"
    return "needs_revision" if _cycle(steps) else "pass"


def load_plan_dataset(name: str = "plan_quality_v1") -> PlanDataset:
    return PlanDataset.model_validate(json.loads((DATASET_DIR / f"{name}.json").read_text(encoding="utf-8")))


def compare_to_baseline(dataset: PlanDataset) -> dict:
    """Verdict accuracy of the evaluator vs. the structural baseline on a dataset."""
    rows = []
    for case in dataset.cases:
        evaluation = evaluate_plan(case.plan)
        failed = {name for name, ok in evaluation.checks.items() if not ok}
        rows.append({"case_id": case.case_id, "expected": case.expected_verdict, "evaluator": evaluation.verdict,
                     "baseline": structural_baseline(case.plan),
                     "expected_checks_found": set(case.expected_failed_checks) <= failed})
    total = len(rows)
    return {
        "dataset_id": dataset.dataset_id, "version": dataset.version, "cases": total,
        "evaluator_accuracy": round(sum(row["evaluator"] == row["expected"] for row in rows) / total, 4),
        "baseline_accuracy": round(sum(row["baseline"] == row["expected"] for row in rows) / total, 4),
        "evaluator_defect_recall": round(sum(row["expected_checks_found"] for row in rows) / total, 4),
        "rows": rows,
    }


def from_execution_plan(plan) -> PlanCandidate:
    """Adapter from the existing orchestration ``ExecutionPlan`` (read-only)."""
    return PlanCandidate(goal=plan.goal[:2000], steps=tuple(
        PlanStepSpec(step_id=step.step_id[:40], description=f"{step.expert_id}: {step.expected_evidence}"[:500],
                     depends_on=tuple(step.depends_on), outputs=(f"{step.step_id}.result",),
                     completion_criteria=f"{step.expert_id} returns advisory evidence"[:300])
        for step in plan.steps))
