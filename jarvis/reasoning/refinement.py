"""Bounded plan refinement: evaluate, ask a refiner to revise, re-evaluate.

At most ``MAX_ATTEMPTS`` revisions and an overall deadline. Refinement stops
as soon as the plan passes, is rejected for safety, stops improving, or the
refiner fails. The result is always a proposal (``executed`` is literally
``False``); refiners cannot bypass the evaluator's safety check.
"""

import asyncio
from time import monotonic
from typing import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from jarvis.reasoning.evaluator import evaluate_plan
from jarvis.reasoning.models import PlanCandidate, PlanEvaluation, RefinementAttempt, RefinementResult

MAX_ATTEMPTS = 3
Refiner = Callable[[PlanCandidate, PlanEvaluation], Awaitable[PlanCandidate]]


def _attempt(index: int, evaluation: PlanEvaluation) -> RefinementAttempt:
    return RefinementAttempt(attempt=index, verdict=evaluation.verdict, score=evaluation.score,
                             error_count=sum(item.severity == "error" for item in evaluation.findings))


async def refine(plan: PlanCandidate, refiner: Refiner, *, max_attempts: int = 2,
                 deadline_seconds: float = 30.0) -> RefinementResult:
    max_attempts = max(0, min(max_attempts, MAX_ATTEMPTS))
    deadline = monotonic() + deadline_seconds
    current, evaluation = plan, evaluate_plan(plan)
    attempts = [_attempt(0, evaluation)]

    def result(reason: str) -> RefinementResult:
        return RefinementResult(final_plan=current, final_evaluation=evaluation, attempts=tuple(attempts),
                                stopped_reason=reason)

    for index in range(1, max_attempts + 1):
        if evaluation.verdict == "pass":
            return result("passed")
        if evaluation.verdict == "reject":
            return result("rejected")
        remaining = deadline - monotonic()
        if remaining <= 0:
            return result("timeout")
        try:
            revised = await asyncio.wait_for(refiner(current, evaluation), timeout=remaining)
            # Refiner output is untrusted: re-validate it fully before evaluating it.
            revised = PlanCandidate.model_validate(revised.model_dump() if isinstance(revised, BaseModel) else revised)
        except asyncio.TimeoutError:
            return result("timeout")
        except (ValidationError, Exception):  # noqa: BLE001 - keep the last evaluated plan
            return result("refiner_error")
        revised_evaluation = evaluate_plan(revised)
        attempts.append(_attempt(index, revised_evaluation))
        if revised_evaluation.score <= evaluation.score and revised_evaluation.verdict != "pass":
            return result("no_progress")
        current, evaluation = revised, revised_evaluation
    if evaluation.verdict == "pass":
        return result("passed")
    return result("rejected" if evaluation.verdict == "reject" else "max_attempts")
