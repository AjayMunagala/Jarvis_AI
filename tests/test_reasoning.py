"""v5.5 plan-quality evaluation, constraints, uncertainty, verification, refinement."""

import asyncio

import pytest
from pydantic import ValidationError

from jarvis.orchestration.models import ExecutionPlan, PlanStep
from jarvis.reasoning.evaluator import (
    compare_to_baseline, evaluate_plan, from_execution_plan, load_plan_dataset, structural_baseline,
)
from jarvis.reasoning.models import EvidenceItem, PlanCandidate, VerifierVerdict
from jarvis.reasoning.refinement import MAX_ATTEMPTS, refine
from jarvis.reasoning.strategies import STRATEGIES, check_answer, strategy_for
from jarvis.reasoning.verification import assess_claims, consolidate

DATASET = load_plan_dataset()
CASES = {case.case_id: case for case in DATASET.cases}


def plan(case_id):
    return CASES[case_id].plan


@pytest.mark.parametrize("case", DATASET.cases, ids=lambda case: case.case_id)
def test_each_labelled_defect_is_detected(case):
    evaluation = evaluate_plan(case.plan)
    failed = {name for name, ok in evaluation.checks.items() if not ok}
    assert set(case.expected_failed_checks) <= failed
    if not case.expected_failed_checks:
        assert evaluation.verdict == case.expected_verdict


def test_baseline_comparison_is_measured_and_reported_honestly():
    report = compare_to_baseline(DATASET)
    assert report["cases"] == 13
    assert report["evaluator_accuracy"] > report["baseline_accuracy"]
    assert report["evaluator_accuracy"] == round(12 / 13, 4) and report["baseline_accuracy"] == round(5 / 13, 4)
    # Known limitation, kept visible rather than tuned away: a duplicated step is only a warning.
    miss = next(row for row in report["rows"] if row["case_id"] == "duplicate-step")
    assert miss["evaluator"] == "pass" and miss["expected"] == "needs_revision" and miss["expected_checks_found"]
    assert structural_baseline(plan("missing-information")) == "pass"


def test_constraint_and_safety_validation():
    unsafe = evaluate_plan(plan("unapproved-consequential"))
    assert unsafe.verdict == "reject" and unsafe.checks["safety"] is False
    approved = evaluate_plan(plan("approved-consequential"))
    assert approved.verdict == "pass" and any("does not execute" in item.message for item in approved.findings)
    assert "Nothing has been executed".lower() in approved.note.lower()
    assert evaluate_plan(plan("too-many-steps")).checks["constraints"] is False
    forbidden = evaluate_plan(plan("forbidden-tool"))
    assert any("shell.exec" in item.message for item in forbidden.findings)
    assert any("backup" in item.message for item in evaluate_plan(plan("required-step-missing")).findings)


def test_dependency_and_information_findings_name_the_step():
    cycle = evaluate_plan(plan("dependency-cycle"))
    assert any("Dependency cycle" in item.message for item in cycle.findings)
    missing = evaluate_plan(plan("missing-information"))
    item = next(entry for entry in missing.findings if entry.check == "information")
    assert item.step_id == "reproduce" and "test_name" in item.message
    assert missing.uncertainty.level == "high" and missing.uncertainty.missing


def test_plan_contract_limits():
    with pytest.raises(ValidationError):
        PlanCandidate(goal="x", steps=())
    with pytest.raises(ValidationError):
        PlanCandidate(goal="x", steps=({"step_id": "Bad Id", "description": "d"},))
    with pytest.raises(ValidationError):
        PlanCandidate(goal="x", steps=({"step_id": "a", "description": "d", "action_kind": "shell"},))


def test_adapter_from_existing_execution_plan():
    existing = ExecutionPlan(execution_id="e1", goal="Assess the deployment risk", intent="risk_assessment",
                             steps=(PlanStep(step_id="expert_1_risk", expert_id="risk", round_number=1,
                                             expected_evidence="Risk assessment of the deployment"),
                                    PlanStep(step_id="expert_2_validation", expert_id="validation", round_number=2,
                                             depends_on=("expert_1_risk",), expected_evidence="Validation of risks")),
                             max_rounds=2, stop_condition="all experts answered")
    candidate = from_execution_plan(existing)
    assert [item.depends_on for item in candidate.steps] == [(), ("expert_1_risk",)]
    assert evaluate_plan(candidate).checks["dependencies"] is True


# ---------------------------------------------------------- strategies

def test_strategies_cover_required_task_classes_and_check_structure():
    assert set(STRATEGIES) == {"coding_analysis", "technical_comparison", "research_synthesis",
                               "constraint_satisfaction", "decision_analysis", "risk_analysis"}
    good = ("Options: Postgres versus SQLite. Criteria: concurrency, operations. Trade-off: SQLite has lower cost "
            "but weaker concurrency. I recommend Postgres for multi-user use.")
    assert check_answer("technical_comparison", good).outcome == "agree"
    weak = check_answer("technical_comparison", "Postgres is good.")
    assert weak.outcome == "disagree" and "missing section: tradeoffs" in weak.reasons
    with pytest.raises(KeyError):
        strategy_for("astrology")


# ------------------------------------------------- uncertainty and verify

def test_uncertainty_reports_missing_and_conflicting_evidence():
    evidence = [
        EvidenceItem(claim_id="port", source_ref="doc:readme", stance="supports", reliability="document"),
        EvidenceItem(claim_id="port", source_ref="config:prod", stance="contradicts", reliability="verified"),
        EvidenceItem(claim_id="db", source_ref="memory:1", stance="supports", reliability="verified"),
        EvidenceItem(claim_id="cache", source_ref="model:guess", stance="supports", reliability="inferred"),
    ]
    claims, uncertainty = assess_claims(["port", "db", "cache", "queue"], evidence)
    status = {item.claim_id: item.status for item in claims}
    assert status == {"port": "contested", "db": "supported", "cache": "supported", "queue": "unsupported"}
    assert uncertainty.level == "high" and uncertainty.missing == ("queue",) and uncertainty.conflicting == ("port",)
    _, only_inferred = assess_claims(["cache"], evidence)
    assert only_inferred.level == "medium" and "inferred" in only_inferred.notes[0]


def test_a_second_model_is_never_treated_as_proof():
    model_yes = VerifierVerdict(verifier_id="model-b", kind="model", outcome="agree")
    model_no = VerifierVerdict(verifier_id="model-c", kind="model", outcome="disagree")
    rule_yes = VerifierVerdict(verifier_id="structure", kind="deterministic", outcome="agree")
    rule_no = VerifierVerdict(verifier_id="tests", kind="deterministic", outcome="disagree")
    assert consolidate([model_yes]).status == "plausible_unverified"
    assert consolidate([rule_yes]).status == "verified"
    assert consolidate([rule_yes, model_yes]).status == "verified"
    assert consolidate([rule_no, model_yes]).status == "failed"
    assert consolidate([model_yes, model_no]).status == "contested"
    assert consolidate([rule_yes, model_no]).status == "contested"
    assert consolidate([]).status == "plausible_unverified"


# ------------------------------------------------------------ refinement

def test_bounded_refinement_reaches_pass_and_never_executes():
    calls = []

    async def fix_inputs(current, evaluation):
        calls.append(evaluation.verdict)
        return current.model_copy(update={"constraints": current.constraints.model_copy(
            update={"available_inputs": ("test_name",)})})

    result = asyncio.run(refine(plan("missing-information"), fix_inputs))
    assert result.stopped_reason == "passed" and result.final_evaluation.verdict == "pass"
    assert result.executed is False and [item.attempt for item in result.attempts] == [0, 1] and calls == ["needs_revision"]


def test_refinement_stops_on_no_progress_errors_limits_and_safety():
    async def same(current, evaluation):
        return current

    async def broken(current, evaluation):
        raise RuntimeError("refiner crashed")

    async def slow(current, evaluation):
        await asyncio.sleep(1)
        return current

    calls = []

    async def counting(current, evaluation):
        calls.append(1)
        steps = [*current.model_dump()["steps"], {"step_id": f"extra{len(calls)}", "description": "Run the failing login test again",
                                                   "completion_criteria": "test output captured in the log"}]
        return PlanCandidate.model_validate({**current.model_dump(), "steps": steps})

    assert asyncio.run(refine(plan("missing-information"), same)).stopped_reason == "no_progress"
    assert asyncio.run(refine(plan("missing-information"), broken)).stopped_reason == "refiner_error"
    assert asyncio.run(refine(plan("missing-information"), slow, deadline_seconds=0.05)).stopped_reason == "timeout"
    assert asyncio.run(refine(plan("unapproved-consequential"), same)).stopped_reason == "rejected"
    capped = asyncio.run(refine(plan("missing-information"), counting, max_attempts=99))
    assert len(calls) <= MAX_ATTEMPTS and capped.stopped_reason in {"no_progress", "max_attempts"}
    assert asyncio.run(refine(plan("good-fix"), broken)).stopped_reason == "passed"

    async def invalid(current, evaluation):
        return {"goal": "x", "steps": [{"step_id": "Bad Id", "description": "d"}]}

    assert asyncio.run(refine(plan("missing-information"), invalid)).stopped_reason == "refiner_error"
