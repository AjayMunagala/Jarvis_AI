"""Truthful per-subsystem status (v4.2).

Each subsystem gets one state:

| State | Meaning |
|---|---|
| ``operational`` | It works as designed, and the checks listed were actually run |
| ``limited`` | It works, with documented limitations that are listed |
| ``mock`` | It produces demonstration output only (for example the mock provider) |
| ``disabled`` | It is switched off by configuration, or no adapter is configured |
| ``unavailable`` | It is configured but a real check failed. Only this state degrades ``overall`` |

``checks`` lists what this request actually verified. ``limitations`` lists
known gaps in the code. Limitations are facts about the current
implementation, and a later version removes its entry when it fixes the gap.
HTTP 200 from this endpoint does not mean every capability works. It reports
which ones do.
"""

from datetime import datetime, timezone
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from jarvis.core.correlation import get_correlation_id
from jarvis.health.probes import measure_memory_percent

State = Literal["operational", "limited", "mock", "disabled", "unavailable"]
Storage = Literal["postgresql", "memory", "none"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SubsystemReport(_Frozen):
    key: str
    name: str
    state: State
    storage: Storage = "none"
    checks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class StatusReport(_Frozen):
    generated_at: str
    correlation_id: str | None
    overall: Literal["operational", "degraded"]
    counts: dict[str, int]
    subsystems: tuple[SubsystemReport, ...]
    note: str = ("States come from checks run for this request and from documented limitations. "
                 "'limited', 'mock' and 'disabled' are honest descriptions, not failures; "
                 "only 'unavailable' degrades the overall status.")


def _storage(service: Any) -> Storage:
    store = getattr(service, "store", None)
    name = type(store).__name__ if store is not None else ""
    if name.startswith("Postgres"):
        return "postgresql"
    if name.startswith("Memory"):
        return "memory"
    return "none"


class StatusService:
    def __init__(self, app_state: Any, *, database_timeout_seconds: int = 2):
        self.state = app_state
        self.database_timeout_seconds = database_timeout_seconds

    # ------------------------------------------------------------ checks

    def _database(self) -> SubsystemReport:
        settings = self.state.settings
        if not settings.persistence_enabled:
            return SubsystemReport(key="database", name="PostgreSQL", state="disabled",
                                   limitations=("Persistence is disabled; data is kept in memory and lost on restart.",))
        started = monotonic()
        try:
            import psycopg

            url = settings.persistence_database_url.get_secret_value()
            with psycopg.connect(url, connect_timeout=self.database_timeout_seconds) as connection:
                connection.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (f"{self.database_timeout_seconds * 1000}ms",))
                connection.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001 - sanitized: no driver message or URL is reported
            return SubsystemReport(key="database", name="PostgreSQL", state="unavailable", storage="postgresql",
                                   checks=("SELECT 1 failed or timed out",))
        elapsed = round((monotonic() - started) * 1000)
        return SubsystemReport(key="database", name="PostgreSQL", state="operational", storage="postgresql",
                               checks=(f"SELECT 1 succeeded in {elapsed} ms",))

    def _telemetry(self) -> SubsystemReport:
        import sys

        memory = measure_memory_percent()
        cpu_supported = sys.platform == "win32" or sys.platform.startswith("linux")
        checks = (f"memory reading {'succeeded' if memory is not None else 'unavailable'}",
                  f"CPU sampler {'available' if cpu_supported else 'not available'} on this platform")
        complete = memory is not None and cpu_supported
        return SubsystemReport(
            key="health", name="System health (v3.6)", state="operational" if complete else "limited",
            storage=_storage(getattr(self.state, "health_service", None)), checks=checks,
            limitations=(() if complete else ("Unmeasurable metrics are reported as null.",))
            + ("Quarantine is a move with a SHA-256 check, not encryption.",
               "Quarantine and restore wait for an approved request (v4.3)."))

    def _scheduler(self) -> SubsystemReport:
        runner = getattr(self.state, "scheduler_runner", None)
        running = bool(getattr(runner, "running", False))
        checks = (f"background runner {'running' if running else 'off'}",)
        if running:
            checks += (f"{runner.ticks} tick(s), {runner.failed_ticks} failed",)
        limitations = ("Notifications appear in JARVIS only; nothing is sent to other devices.",)
        if not running:
            limitations = ("The background runner is off (JARVIS_SCHEDULER_RUNNER_ENABLED=false): reminders and jobs "
                           "run only when triggered manually.",) + limitations
        return SubsystemReport(key="scheduler", name="Scheduler and notifications (v3.5/v4.5)",
                               state="operational" if running and not getattr(runner, "failed_ticks", 0) else "limited",
                               storage=_storage(getattr(self.state, "scheduler_service", None)), checks=checks,
                               limitations=limitations)

    # ------------------------------------------------------------ report

    def report(self) -> StatusReport:
        state, settings = self.state, self.state.settings
        provider = getattr(state, "provider", None)
        demo = getattr(provider, "is_demo", False) is True
        audit = getattr(state, "audit_service", None)
        audit_failures = getattr(audit, "failures", 0) or 0
        database = self._database()
        reports = [
            SubsystemReport(key="provider", name="Model provider", state="mock" if demo else "operational",
                            checks=(f"configured provider: {getattr(provider, 'name', 'none')}",),
                            limitations=("Demo replies only; no language model is used.",) if demo else
                            ("The provider is not contacted by this check.",)),
            database,
            SubsystemReport(key="chat", name="Chat and orchestration", state="operational",
                            limitations=("Chat does not yet use v3.x services or the v5 intelligence layer (v4.7).",)),
            SubsystemReport(key="memory", name="Conversations and memory",
                            state="operational" if settings.persistence_enabled else "disabled",
                            storage="postgresql" if settings.persistence_enabled else "none"),
            SubsystemReport(key="audit", name="Audit trail", state="limited" if audit_failures else "operational",
                            storage=_storage(audit), checks=(f"{audit_failures} durable write failure(s) since start",),
                            limitations=(("Some records could only be kept in memory.",) if audit_failures else ())),
            SubsystemReport(key="approvals", name="Approval gateway", state="operational",
                            storage=_storage(getattr(state, "approval_service", None)),
                            limitations=("Tool and browser approvals record the decision only; nothing is executed.",
                                         "Autonomous writes, workflow runs with writes, improvement apply/restore and "
                                         "quarantine/restore run once after approval, when resubmitted with the approval "
                                         "ID (v4.3/v4.4).")),
            SubsystemReport(key="tools", name="Read-only tool runtime",
                            state="operational" if settings.tools_enabled else "disabled"),
            SubsystemReport(key="chat_tools", name="Chat tool use",
                            state="operational" if settings.tools_enabled and settings.chat_tools_enabled else "disabled"),
            SubsystemReport(key="browser", name="Browser intelligence",
                            state="operational" if settings.browser_enabled else "disabled"),
            SubsystemReport(key="verification_execution", name="Verification execution", state="disabled",
                            limitations=("Command execution is disabled by design.",)),
            SubsystemReport(key="voice", name="Voice (browser-native)",
                            state="operational" if settings.voice_enabled else "disabled",
                            limitations=("Runs in the browser; the server never receives audio.",)),
            SubsystemReport(key="assistant", name="Personal assistant (v3.1)", state="limited",
                            storage=_storage(getattr(state, "assistant_service", None)),
                            limitations=("Reminders are stored but never delivered (no scheduler runner until v4.5).",
                                         "The calendar is local only.")),
            SubsystemReport(key="workflow", name="Workflows (v3.2/v3.8)", state="limited",
                            storage=_storage(getattr(state, "workflow_service", None)),
                            limitations=("A timed-out write step cannot be stopped; its outcome is reported as unknown.",
                                         "Tool steps run only read-only, low-risk tools (the tool runtime's policy).",
                                         "Runs with write steps wait for an approved request (v4.4).")),
            SubsystemReport(key="knowledge", name="Knowledge store (v3.3)", state="limited",
                            storage=_storage(getattr(state, "knowledge_service", None)),
                            limitations=("Semantic scoring uses process-specific hashing; only lexical search is "
                                         "reliable across restarts (v4.6).",)),
            SubsystemReport(key="research", name="Research and documents (v3.4)", state="limited",
                            storage=_storage(getattr(state, "research_service", None)),
                            limitations=("Parses text, JSON and CSV only (no PDF/DOCX).", "Web URLs are not fetched.")),
            self._scheduler(),
            self._telemetry(),
            SubsystemReport(key="finance", name="Finance (v3.7)", state="limited",
                            storage=_storage(getattr(state, "finance_service", None)),
                            limitations=("Analytical only; manually entered data; no trading.",
                                         "Balance updates are atomic with their transaction (v4.4).")),
            SubsystemReport(key="improvement", name="Controlled improvement (v3.9)", state="limited",
                            storage=_storage(getattr(state, "improvement_service", None)),
                            limitations=("Proposals and snapshots are kept in memory and lost on restart (v4.9).",
                                         "Apply and restore wait for an approved request (v4.3).")),
            SubsystemReport(key="autonomous", name="Autonomous coordinator (v4.0)", state="limited",
                            limitations=("Keyword intent routing.",
                                         "Commands with write steps wait for an approved request (v4.3).")),
        ]
        intelligence = getattr(state, "intelligence", None)
        if intelligence is not None:
            adapters = set(getattr(intelligence.router, "adapters", {}))
            reports += [
                SubsystemReport(key="model_routing", name="Model routing (v5.1)",
                                state="mock" if adapters <= {"mock"} else "operational",
                                checks=(f"adapters: {', '.join(sorted(adapters)) or 'none'}",)),
                SubsystemReport(key="knowledge_graph", name="Knowledge graph (v5.2)", state="operational",
                                storage=_storage(intelligence.graph)),
                SubsystemReport(key="personalization", name="Personalization (v5.6)", state="operational",
                                storage=_storage(intelligence.personalization)),
                SubsystemReport(key="multimodal", name="Multimodal (v5.4)", state="disabled",
                                limitations=("No multimodal model adapter is configured.",)),
            ]
        if database.state == "unavailable":
            # A subsystem that stores its data in PostgreSQL cannot be operational while the database is unreachable.
            reports = [item.model_copy(update={
                "state": "unavailable",
                "limitations": ("PostgreSQL is unreachable, so this subsystem cannot read or write its data.",)
                + item.limitations})
                if item.key != "database" and item.storage == "postgresql" and item.state != "disabled" else item
                for item in reports]
        counts: dict[str, int] = {}
        for item in reports:
            counts[item.state] = counts.get(item.state, 0) + 1
        return StatusReport(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), correlation_id=get_correlation_id(),
            overall="degraded" if counts.get("unavailable") else "operational", counts=counts,
            subsystems=tuple(reports))

    def summary_map(self) -> dict[str, str]:
        """Compact {key: state} map used by health snapshots and scheduled health checks."""
        return {item.key: item.state for item in self.report().subsystems}
