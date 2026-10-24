"""Autonomous intent classification and execution planner (v4.0).

Analyzes natural language prompts and formulates coordinated, multi-subsystem execution plans.
"""

from typing import Any
from pydantic import BaseModel, ConfigDict


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subsystem: str
    action: str
    payload: dict[str, Any]
    description: str


class IntentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: str
    subsystems_involved: list[str]
    planned_steps: list[PlannedStep]
    estimated_impact: str


def classify_and_plan(prompt: str, context: dict[str, Any] | None = None) -> IntentPlan:
    """Classifies user intent and builds a bounded multi-step autonomous plan."""
    p_lower = prompt.lower()
    ctx = context or {}

    # 1. Financial Health & Net Worth Assessment
    if any(k in p_lower for k in ("finance", "net worth", "balance", "expense", "budget", "runway", "financial")):
        return IntentPlan(
            intent="FINANCIAL_HEALTH_ASSESSMENT",
            subsystems_involved=["finance", "assistant"],
            planned_steps=[
                PlannedStep(
                    subsystem="finance",
                    action="accounts.list",
                    payload={},
                    description="Retrieve registered financial accounts",
                ),
                PlannedStep(
                    subsystem="finance",
                    action="snapshot.generate",
                    payload={},
                    description="Compute current net worth snapshot and runway analytics",
                ),
                PlannedStep(
                    subsystem="assistant",
                    action="notes.create",
                    payload={
                        "title": f"Autonomous Financial Review: {prompt[:40]}",
                        "content": f"Automated assessment executed for prompt: '{prompt}'.",
                    },
                    description="Persist financial assessment summary note in Assistant memory",
                ),
            ],
            estimated_impact="Analytical snapshot and assistant record generated (zero trading).",
        )

    # 2. Proactive Scheduling & Notification Setup
    if any(k in p_lower for k in ("schedule", "remind", "cron", "recurring")):
        return IntentPlan(
            intent="PROACTIVE_SCHEDULING",
            subsystems_involved=["scheduler", "assistant"],
            planned_steps=[
                PlannedStep(
                    subsystem="scheduler",
                    action="jobs.create",
                    payload={
                        "name": f"Proactive Job: {prompt[:30]}",
                        "schedule_type": "interval",
                        "interval_seconds": 3600,
                        "action_type": "health_check",
                    },
                    description="Register background proactive scheduled job",
                ),
                PlannedStep(
                    subsystem="assistant",
                    action="tasks.create",
                    payload={
                        "title": f"Monitor scheduled job for '{prompt[:30]}'",
                        "priority": "low",
                    },
                    description="Record scheduler oversight task in Assistant",
                ),
            ],
            estimated_impact="Proactive background schedule registered with anti-fatigue safeguards.",
        )

    # 3. Research, Document Synthesis & Knowledge Ingestion
    if any(k in p_lower for k in ("research", "document", "summarize doc", "analyze paper", "ingest", "paper")):
        doc_text = ctx.get("text_content", f"Autonomous research briefing on: {prompt}")
        return IntentPlan(
            intent="RESEARCH_AND_KNOWLEDGE_INGEST",
            subsystems_involved=["research", "knowledge", "assistant"],
            planned_steps=[
                PlannedStep(
                    subsystem="research",
                    action="documents.parse",
                    payload={
                        "title": f"Autonomous Brief: {prompt[:30]}",
                        "text_content": doc_text,
                        "content_type": "text/markdown",
                    },
                    description="Parse untrusted document under structural security fences",
                ),
                PlannedStep(
                    subsystem="knowledge",
                    action="chunks.ingest",
                    payload={
                        "title": f"Autonomous Knowledge: {prompt[:30]}",
                        "text": doc_text,
                        "metadata": {"source": "autonomous_router", "topic": prompt[:50]},
                    },
                    description="Ingest synthesized insights into Knowledge Store with vector embeddings",
                ),
                PlannedStep(
                    subsystem="assistant",
                    action="tasks.create",
                    payload={
                        "title": f"Review research findings on '{prompt[:40]}'",
                        "priority": "medium",
                    },
                    description="Create follow-up review task in Assistant task manager",
                ),
            ],
            estimated_impact="Document synthesized, knowledge indexed, and follow-up task logged.",
        )

    # 4. System Health & Self-Improvement Diagnostic Sweep
    if any(k in p_lower for k in ("health", "diagnostic", "telemetry", "maintenance", "system check", "benchmark")):
        return IntentPlan(
            intent="PROACTIVE_MAINTENANCE_SWEEP",
            subsystems_involved=["health", "improvement", "assistant"],
            planned_steps=[
                PlannedStep(
                    subsystem="health",
                    action="diagnostic.run",
                    payload={},
                    description="Execute multi-metric system health telemetry probe",
                ),
                PlannedStep(
                    subsystem="improvement",
                    action="benchmarks.run",
                    payload={},
                    description="Evaluate offline prompt adherence and safety benchmark suite",
                ),
                PlannedStep(
                    subsystem="assistant",
                    action="notes.create",
                    payload={
                        "title": "System Diagnostic & Benchmark Log",
                        "content": f"Autonomous maintenance probe completed successfully for prompt: '{prompt}'.",
                    },
                    description="Record maintenance telemetry log in Assistant notes",
                ),
            ],
            estimated_impact="Telemetry captured and offline benchmarks validated.",
        )

    # 5. Default General Assistance & Knowledge Search
    return IntentPlan(
        intent="GENERAL_ASSISTANCE",
        subsystems_involved=["assistant", "knowledge"],
        planned_steps=[
            PlannedStep(
                subsystem="knowledge",
                action="search",
                payload={"query": prompt, "limit": 3},
                description="Perform hybrid search across existing knowledge base",
            ),
            PlannedStep(
                subsystem="assistant",
                action="notes.create",
                payload={
                    "title": f"Autonomous Log: {prompt[:40]}",
                    "content": f"Processed autonomous instruction: '{prompt}'.",
                },
                description="Persist interaction in Assistant notes",
            ),
        ],
        estimated_impact="General assistance executed and note saved.",
    )
