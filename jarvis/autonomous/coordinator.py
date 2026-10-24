"""Autonomous AI Operating System Coordinator and Capstone Dispatcher (v4.0).

Binds Assistant, Workflows, Knowledge, Research, Scheduler, Health, Finance,
and Self-Improvement into a coherent AI operating system experience.
"""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.assistant.models import NoteCreateRequest, TaskCreateRequest
from jarvis.autonomous.models import (
    AutonomousCommandRequest,
    AutonomousCommandResult,
    AutonomousStepLog,
    SubsystemStatus,
    SystemOverview,
)
from jarvis.autonomous.router import classify_and_plan
from jarvis.knowledge.models import KnowledgeDocumentCreateRequest, KnowledgeSearchRequest
from jarvis.research.models import DocumentParseRequest
from jarvis.scheduler.models import JobCreateRequest

logger = logging.getLogger(__name__)


class AutonomousCoordinator:
    """Orchestrates cross-subsystem tasks and delivers a unified OS capability."""

    def __init__(
        self,
        assistant_service: Any = None,
        workflow_service: Any = None,
        knowledge_service: Any = None,
        research_service: Any = None,
        scheduler_service: Any = None,
        health_service: Any = None,
        finance_service: Any = None,
        improvement_service: Any = None,
        audit_service: Any = None,
    ) -> None:
        self.assistant_service = assistant_service
        self.workflow_service = workflow_service
        self.knowledge_service = knowledge_service
        self.research_service = research_service
        self.scheduler_service = scheduler_service
        self.health_service = health_service
        self.finance_service = finance_service
        self.improvement_service = improvement_service
        self.audit_service = audit_service
        self._history: list[AutonomousCommandResult] = []

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="orchestration",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record autonomous audit event: %s", exc)

    def execute_command(
        self, owner_id: UUID, req: AutonomousCommandRequest
    ) -> AutonomousCommandResult:
        """Executes a natural-language autonomous command across JARVIS subsystems."""
        plan = classify_and_plan(req.prompt, req.context)
        command_id = uuid4()
        now = datetime.now(timezone.utc)

        step_logs: list[AutonomousStepLog] = []

        if req.dry_run:
            for idx, pstep in enumerate(plan.planned_steps[: req.max_steps]):
                step_logs.append(
                    AutonomousStepLog(
                        step_index=idx,
                        subsystem=pstep.subsystem,
                        action=pstep.action,
                        status="skipped",
                        summary=f"[Dry Run] {pstep.description}",
                        data={"dry_run": True, "payload": pstep.payload},
                    )
                )
            result = AutonomousCommandResult(
                command_id=command_id,
                owner_id=owner_id,
                prompt=req.prompt,
                status="dry_run",
                intent=plan.intent,
                subsystems_involved=plan.subsystems_involved,
                steps=step_logs,
                summary=f"Dry-run simulation completed. {len(step_logs)} steps planned.",
                executed_at=now,
            )
            self._history.append(result)
            return result

        overall_status = "completed"
        for idx, pstep in enumerate(plan.planned_steps[: req.max_steps]):
            try:
                out_data, step_summary = self._dispatch_action(owner_id, pstep.subsystem, pstep.action, pstep.payload)
                step_logs.append(
                    AutonomousStepLog(
                        step_index=idx,
                        subsystem=pstep.subsystem,
                        action=pstep.action,
                        status="success",
                        summary=step_summary,
                        data=out_data,
                    )
                )
            except Exception as exc:
                logger.error("Autonomous step %d (%s.%s) failed: %s", idx, pstep.subsystem, pstep.action, exc)
                step_logs.append(
                    AutonomousStepLog(
                        step_index=idx,
                        subsystem=pstep.subsystem,
                        action=pstep.action,
                        status="failed",
                        summary=f"Step failed: {str(exc)}",
                        data={"error": str(exc)},
                    )
                )
                overall_status = "failed"
                break

        summary_text = (
            f"Autonomous command executed successfully across {len(plan.subsystems_involved)} subsystems."
            if overall_status == "completed"
            else f"Autonomous command partially executed; halted at step {len(step_logs)} due to error."
        )

        result = AutonomousCommandResult(
            command_id=command_id,
            owner_id=owner_id,
            prompt=req.prompt,
            status=overall_status,
            intent=plan.intent,
            subsystems_involved=plan.subsystems_involved,
            steps=step_logs,
            summary=summary_text,
            executed_at=now,
        )

        self._history.append(result)
        self._audit(
            owner_id,
            "autonomous.command.execute",
            f"autonomous:cmd:{command_id}",
            {
                "intent": plan.intent,
                "status": overall_status,
                "steps_completed": len(step_logs),
                "subsystems": plan.subsystems_involved,
            },
        )
        return result

    def _dispatch_action(
        self, owner_id: UUID, subsystem: str, action: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Dispatches an individual planned step to its respective subsystem service."""

        # 1. Finance Subsystem
        if subsystem == "finance":
            if self.finance_service is None:
                raise RuntimeError("Finance service is unavailable")
            if action == "accounts.list":
                page = self.finance_service.list_accounts(owner_id)
                return {"count": page.total}, f"Retrieved {page.total} financial accounts."
            if action == "snapshot.generate":
                snap = self.finance_service.generate_snapshot(owner_id)
                runway_desc = f"{snap.projected_runway_months:.1f} mos" if snap.projected_runway_months is not None else "N/A"
                return {
                    "snapshot_id": str(snap.id),
                    "net_worth": snap.net_worth,
                    "runway_months": snap.projected_runway_months,
                }, f"Generated snapshot: Net worth ${snap.net_worth:,.2f}, runway {runway_desc}."

        # 2. Assistant Subsystem
        elif subsystem == "assistant":
            if self.assistant_service is None:
                raise RuntimeError("Assistant service is unavailable")
            if action == "notes.create":
                req = NoteCreateRequest(
                    title=payload.get("title", "Autonomous Note"),
                    content=payload.get("content", ""),
                    tags=["autonomous", "os_capstone"],
                )
                note = self.assistant_service.create_note(owner_id, req)
                return {"note_id": str(note.id), "title": note.title}, f"Created Assistant note: '{note.title}'."
            if action == "tasks.create":
                req = TaskCreateRequest(
                    title=payload.get("title", "Autonomous Task"),
                    priority=payload.get("priority", "medium"),
                    tags=["autonomous"],
                )
                task = self.assistant_service.create_task(owner_id, req)
                return {"task_id": str(task.id), "title": task.title}, f"Created Assistant task: '{task.title}'."

        # 3. Research Subsystem
        elif subsystem == "research":
            if self.research_service is None:
                raise RuntimeError("Research service is unavailable")
            if action == "documents.parse":
                req = DocumentParseRequest(
                    filename=payload.get("filename", "autonomous_brief.md"),
                    content_type=payload.get("content_type", "text/markdown"),
                    text_content=payload.get("text_content", ""),
                )
                doc = self.research_service.parse_and_store_document(owner_id, req)
                return {"doc_id": str(doc.id), "pages": doc.page_count}, f"Parsed document '{doc.filename}' under structural injection fencing."

        # 4. Knowledge Subsystem
        elif subsystem == "knowledge":
            if self.knowledge_service is None:
                raise RuntimeError("Knowledge service is unavailable")
            if action == "chunks.ingest":
                req = KnowledgeDocumentCreateRequest(
                    title=payload.get("title", "Autonomous Document"),
                    content=payload.get("text", ""),
                    metadata=payload.get("metadata", {}),
                )
                doc = self.knowledge_service.ingest_document(owner_id, req)
                chunk_cnt = len(doc.chunks)
                return {"doc_id": str(doc.document.id), "chunks": chunk_cnt}, f"Ingested into knowledge store ({chunk_cnt} chunks)."
            if action == "search":
                req = KnowledgeSearchRequest(
                    query=payload.get("query", ""),
                    top_k=payload.get("limit", 3),
                )
                resp = self.knowledge_service.search(owner_id, req)
                return {"matches": len(resp.results)}, f"Retrieved {len(resp.results)} hybrid search matches."

        # 5. Health Subsystem
        elif subsystem == "health":
            if self.health_service is None:
                raise RuntimeError("Health service is unavailable")
            if action == "diagnostic.run":
                report = self.health_service.run_diagnostics(owner_id)
                return {
                    "disk_free_gb": report.telemetry.disk_free_gb,
                    "findings_count": len(report.secret_findings),
                }, f"Diagnostics completed: disk free {report.telemetry.disk_free_gb:.1f} GB, {len(report.secret_findings)} secret findings."

        # 6. Improvement Subsystem
        elif subsystem == "improvement":
            if self.improvement_service is None:
                raise RuntimeError("Improvement service is unavailable")
            if action == "benchmarks.run":
                bench = self.improvement_service.evaluate_benchmarks(target_name=payload.get("target_name"))
                return {
                    "passed": bench.passed_benchmarks,
                    "total": bench.total_benchmarks,
                    "all_passed": bench.all_passed,
                }, f"Benchmarks evaluated: {bench.passed_benchmarks}/{bench.total_benchmarks} passed."

        # 7. Scheduler Subsystem
        elif subsystem == "scheduler":
            if self.scheduler_service is None:
                raise RuntimeError("Scheduler service is unavailable")
            if action == "jobs.create":
                req = JobCreateRequest(
                    name=payload.get("name", "Autonomous Scheduled Job"),
                    schedule_type=payload.get("schedule_type", "interval"),
                    schedule_expr=str(payload.get("interval_seconds", 3600)),
                    action_type=payload.get("action_type", "health_check"),
                    payload=payload.get("payload", {}),
                    enabled=True,
                )
                job = self.scheduler_service.create_job(owner_id, req)
                return {"job_id": str(job.id), "name": job.name}, f"Created proactive scheduled job '{job.name}'."

        raise NotImplementedError(f"Action '{action}' on subsystem '{subsystem}' is not supported.")

    def get_system_overview(self, owner_id: UUID) -> SystemOverview:
        """Gathers runtime operational status and telemetry from all 8 subsystems."""
        subsystems: dict[str, SubsystemStatus] = {}

        # 1. Assistant
        if self.assistant_service is not None:
            tasks_page = self.assistant_service.list_tasks(owner_id, limit=1)
            notes_page = self.assistant_service.list_notes(owner_id, limit=1)
            subsystems["assistant"] = SubsystemStatus(
                name="Assistant Foundation (v3.1)",
                operational=True,
                metrics={"tasks_count": tasks_page.total, "notes_count": notes_page.total},
                details="Tasks, notes, reminders, and calendar operational.",
            )
        else:
            subsystems["assistant"] = SubsystemStatus(name="Assistant Foundation (v3.1)", operational=False)

        # 2. Workflow
        subsystems["workflow"] = SubsystemStatus(
            name="Workflow Orchestration (v3.2 & v3.8)",
            operational=self.workflow_service is not None,
            metrics={"engine": "DAG & 4-tier self-healing"},
            details="Workflow compilation, retries, and rollback operational.",
        )

        # 3. Knowledge
        subsystems["knowledge"] = SubsystemStatus(
            name="Knowledge Intelligence (v3.3)",
            operational=self.knowledge_service is not None,
            metrics={"retrieval": "Hybrid BM25 + Vector RRF"},
            details="Knowledge indexing and context budgeting operational.",
        )

        # 4. Research
        subsystems["research"] = SubsystemStatus(
            name="Research & Document Intelligence (v3.4)",
            operational=self.research_service is not None,
            metrics={"parser": "Structural injection fencing enabled"},
            details="Multi-format document ingestion and cited synthesis operational.",
        )

        # 5. Scheduler
        if self.scheduler_service is not None:
            jobs_page = self.scheduler_service.list_jobs(owner_id, limit=1)
            subsystems["scheduler"] = SubsystemStatus(
                name="Proactive Scheduling (v3.5)",
                operational=True,
                metrics={"jobs_count": jobs_page.total},
                details="In-process scheduling and anti-fatigue throttling operational.",
            )
        else:
            subsystems["scheduler"] = SubsystemStatus(name="Proactive Scheduling (v3.5)", operational=False)

        # 6. Health
        subsystems["health"] = SubsystemStatus(
            name="System Health & Reliability (v3.6)",
            operational=self.health_service is not None,
            metrics={"quarantine": "Tamper-detection quarantine manager active"},
            details="Telemetry probes and diagnostic sweeps operational.",
        )

        # 7. Finance
        if self.finance_service is not None:
            acc_page = self.finance_service.list_accounts(owner_id, limit=1)
            subsystems["finance"] = SubsystemStatus(
                name="Finance Intelligence (v3.7)",
                operational=True,
                metrics={"accounts_count": acc_page.total, "safety": "Zero-trading AST invariant"},
                details="Personal finance analytics and net worth forecasting operational.",
            )
        else:
            subsystems["finance"] = SubsystemStatus(name="Finance Intelligence (v3.7)", operational=False)

        # 8. Improvement
        subsystems["improvement"] = SubsystemStatus(
            name="Controlled Self-Improvement (v3.9)",
            operational=self.improvement_service is not None,
            metrics={"sandbox": "Offline benchmark suite and immutable file gating"},
            details="Prompt mutation proposals and 1-click restore operational.",
        )

        subsystems = self._truthful(subsystems)
        all_operational = all(s.operational for s in subsystems.values())

        return SystemOverview(
            system_status="operational" if all_operational else "degraded",
            subsystems=subsystems,
            active_features=[
                "personal_assistant",
                "workflow_orchestration",
                "knowledge_intelligence",
                "document_intelligence",
                "proactive_scheduling",
                "system_health_telemetry",
                "finance_intelligence",
                "controlled_self_improvement",
                "autonomous_os_capstone",
            ],
            version="4.0.0",
            timestamp=datetime.now(timezone.utc),
        )

    def _truthful(self, subsystems: dict[str, SubsystemStatus]) -> dict[str, SubsystemStatus]:
        """v4.2: keep measured counts; replace fixed claims with the shared status report.

        Text metrics such as "Hybrid BM25 + Vector RRF" and "... operational" details
        were fixed strings, not observations. Only numeric (counted) metrics are kept,
        and the state and limitations come from StatusService. Without a status
        service, nothing beyond "configured" is claimed.
        """
        reports = {}
        status_service = getattr(self, "status_service", None)
        if status_service is not None:
            reports = {item.key: item for item in status_service.report().subsystems}
        result = {}
        for key, item in subsystems.items():
            metrics = {name: value for name, value in item.metrics.items()
                       if isinstance(value, (int, float)) and not isinstance(value, bool)}
            report = reports.get(key)
            if report is None:
                result[key] = item.model_copy(update={"metrics": metrics, "details": (
                    "Configured; no status check is available." if item.operational else item.details)})
                continue
            metrics["state"] = report.state
            result[key] = item.model_copy(update={
                "operational": item.operational and report.state not in {"unavailable", "disabled"},
                "metrics": metrics,
                "details": "; ".join(report.limitations) or f"State: {report.state}.",
            })
        return result

    def get_history(self, owner_id: UUID, limit: int = 50) -> list[AutonomousCommandResult]:
        """Returns autonomous execution history for the given owner."""
        matched = [r for r in self._history if r.owner_id == owner_id]
        return matched[-limit:]
