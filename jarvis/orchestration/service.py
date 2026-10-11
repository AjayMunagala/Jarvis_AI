"""Mother Orchestrator service boundary used by HTTP routes."""

import logging

from jarvis.core.config import Settings
from jarvis.intelligence.base import IntelligenceProvider
from jarvis.orchestration.classification import IntentClassifier, RuleBasedIntentClassifier, classify_safely
from jarvis.orchestration.experts import ExpertExecutor
from jarvis.orchestration.models import (
    MAX_SYNTHESIZED_REPLY_LENGTH,
    AuditEvent,
    OrchestrationResponse,
    RequestContext,
)
from jarvis.orchestration.planner import Planner
from jarvis.orchestration.registry import ExpertRegistry, ToolRegistry

logger = logging.getLogger(__name__)
_TRUNCATION_MARKER = "\n\n[Response truncated because combined expert output exceeded the 40,000-character limit.]"


class OrchestrationService:
    def __init__(
        self,
        provider: IntelligenceProvider,
        settings: Settings,
        *,
        classifier: IntentClassifier | None = None,
        planner: Planner | None = None,
        expert_registry: ExpertRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self._provider = provider
        self._settings = settings
        self._classifier = classifier or RuleBasedIntentClassifier()
        self._planner = planner or Planner()
        self.expert_registry = expert_registry or ExpertRegistry.load()
        self.tool_registry = tool_registry or ToolRegistry.load()
        registered_tools = {tool.id for tool in self.tool_registry.config.tools}
        for definition in self.expert_registry.config.experts:
            if set(definition.tool_ids) - registered_tools:
                raise ValueError("Expert configuration references an unavailable tool.")
        self._executor = ExpertExecutor(
            provider,
            self.expert_registry,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_expert_retries,
            max_concurrency=settings.max_parallel_experts,
        )

    async def run(self, context: RequestContext) -> OrchestrationResponse:
        intent = classify_safely(self._classifier, context)
        plan = self._planner.create_plan(
            context,
            intent,
            self.expert_registry,
            max_rounds=self._settings.max_tool_rounds,
            max_concurrency=self._settings.max_parallel_experts,
        )
        results, expert_events = await self._executor.execute_plan(plan, context)
        successful = [result.output.content for result in results if result.status == "succeeded" and result.output]
        reply = "\n\n".join(successful)
        if len(reply) > MAX_SYNTHESIZED_REPLY_LENGTH:
            reply = reply[: MAX_SYNTHESIZED_REPLY_LENGTH - len(_TRUNCATION_MARKER)].rstrip() + _TRUNCATION_MARKER
        partial = any(result.status == "failed" for result in results)
        completion = AuditEvent(
            execution_id=context.execution_id,
            event_type="execution_completed",
            outcome="partial" if partial else "succeeded",
        )
        logger.info(
            "Orchestration completed",
            extra={
                "execution_id": context.execution_id,
                "intent": intent.intent,
                "confidence": intent.confidence,
                "planned_experts": len(plan.steps),
                "successful_experts": len(successful),
                "failed_experts": len(results) - len(successful),
                "partial": partial,
                "rounds": plan.max_rounds,
                "expert_outcomes": [
                    {"expert_id": result.expert_id, "status": result.status, "attempts": result.attempts, "error_code": result.error_code}
                    for result in results
                ],
                "tool_execution_enabled": self.tool_registry.execution_enabled,
            },
        )
        return OrchestrationResponse(
            execution_id=context.execution_id,
            intent=intent,
            plan=plan,
            expert_results=results,
            reply=reply,
            partial=partial,
            audit_events=(*expert_events, completion),
        )
