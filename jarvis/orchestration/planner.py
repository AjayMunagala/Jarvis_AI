"""Deterministic planner that emits only registered expert steps."""

from jarvis.orchestration.models import ExecutionPlan, IntentClassification, PlanStep, RequestContext
from jarvis.orchestration.registry import ExpertRegistry


class Planner:
    def create_plan(
        self,
        context: RequestContext,
        intent: IntentClassification,
        registry: ExpertRegistry,
        *,
        max_rounds: int = 5,
        max_concurrency: int = 2,
    ) -> ExecutionPlan:
        experts = registry.select(intent.intent)
        parallel = intent.intent in registry.config.parallel_intents
        if parallel:
            selected = experts
            steps = tuple(
                PlanStep(
                    step_id=f"expert_{index}_{expert.id}",
                    expert_id=expert.id,
                    round_number=1,
                    expected_evidence=f"Advisory response from {expert.persona}.",
                )
                for index, expert in enumerate(selected, start=1)
            )
            concurrency = min(max_concurrency, len(steps))
            rounds = 1
        else:
            selected = experts[:max_rounds]
            steps = tuple(
                PlanStep(
                    step_id=f"expert_{index}_{expert.id}",
                    expert_id=expert.id,
                    round_number=index,
                    depends_on=(f"expert_{index - 1}_{selected[index - 2].id}",) if index > 1 else (),
                    expected_evidence=f"Advisory response from {expert.persona}.",
                )
                for index, expert in enumerate(selected, start=1)
            )
            concurrency = 1
            rounds = len(steps)
        if not steps:
            raise ValueError("The planner could not select a configured expert.")
        return ExecutionPlan(
            execution_id=context.execution_id,
            goal=context.message,
            intent=intent.intent,
            steps=steps,
            max_rounds=rounds,
            allow_parallel=parallel,
            max_concurrency=concurrency,
            stop_condition="Stop after the configured expert steps complete or the request deadline expires.",
        )
