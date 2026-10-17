"""Small server-side capability policy for the single configured owner."""

from enum import StrEnum
from uuid import UUID


class Capability(StrEnum):
    """Server-defined operations; values are never accepted as client grants."""

    CHAT_ADVISORY = "chat.advisory"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    CONVERSATION_READ = "conversation.read"
    CONVERSATION_WRITE = "conversation.write"
    REPO_READ = "repo.read"
    VERIFY_READ = "verify.read"
    VERIFY_EXECUTE = "verify.execute"
    # Browser reading (observe, navigate, research) and non-consequential interaction.
    BROWSER_READ = "browser.read"
    BROWSER_INTERACT = "browser.interact"
    # Tool runtime (v2.7): list capabilities/audit, and invoke registered read-only tools.
    TOOLS_READ = "tools.read"
    TOOLS_INVOKE = "tools.invoke"
    # Audit trail and approval gateway (v2.9). Deciding never executes anything.
    AUDIT_READ = "audit.read"
    APPROVALS_READ = "approvals.read"
    APPROVALS_DECIDE = "approvals.decide"
    # Personal assistant foundation (v3.1): tasks, notes, reminders, local calendar.
    ASSISTANT_READ = "assistant.read"
    ASSISTANT_WRITE = "assistant.write"
    # Workflow orchestration (v3.2): DAG workflows and execution runs.
    WORKFLOW_READ = "workflow.read"
    WORKFLOW_WRITE = "workflow.write"
    # Knowledge intelligence & context continuity (v3.3): hybrid retrieval and context budgeting.
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_WRITE = "knowledge.write"
    # Research & document intelligence (v3.4): document parsing and synthesis.
    RESEARCH_READ = "research.read"
    RESEARCH_WRITE = "research.write"
    # Proactive assistance & scheduling (v3.5): background jobs and notifications.
    SCHEDULER_READ = "scheduler.read"
    SCHEDULER_WRITE = "scheduler.write"
    # System health & reliability (v3.6): telemetry diagnostics and security quarantine.
    HEALTH_READ = "health.read"
    HEALTH_DIAGNOSTIC_RUN = "health.diagnostic.run"
    SECURITY_QUARANTINE_FILE = "security.quarantine.file"
    # Finance & external data intelligence (v3.7): analytical personal finance intelligence.
    FINANCE_READ = "finance.read"
    FINANCE_WRITE = "finance.write"
    # Controlled self-improvement & hardening (v3.9): offline benchmarks, diffs, approval gates.
    IMPROVEMENT_READ = "improvement.read"
    IMPROVEMENT_PROPOSE = "improvement.propose"
    IMPROVEMENT_APPROVE = "improvement.approve"
    # Autonomous AI Operating System Capstone (v4.0): unified multi-subsystem autonomous coordination.
    AUTONOMOUS_READ = "autonomous.read"
    AUTONOMOUS_EXECUTE = "autonomous.execute"
    EXTERNAL_ACTION_EXECUTE = "external_action.execute"


_OWNER_CAPABILITIES = frozenset({
    Capability.CHAT_ADVISORY,
    Capability.MEMORY_READ,
    Capability.MEMORY_WRITE,
    Capability.CONVERSATION_READ,
    Capability.CONVERSATION_WRITE,
    Capability.REPO_READ,
    Capability.VERIFY_READ,
    Capability.BROWSER_READ,
    Capability.BROWSER_INTERACT,
    Capability.TOOLS_READ,
    Capability.TOOLS_INVOKE,
    Capability.AUDIT_READ,
    Capability.APPROVALS_READ,
    Capability.APPROVALS_DECIDE,
    Capability.ASSISTANT_READ,
    Capability.ASSISTANT_WRITE,
    Capability.WORKFLOW_READ,
    Capability.WORKFLOW_WRITE,
    Capability.KNOWLEDGE_READ,
    Capability.KNOWLEDGE_WRITE,
    Capability.RESEARCH_READ,
    Capability.RESEARCH_WRITE,
    Capability.SCHEDULER_READ,
    Capability.SCHEDULER_WRITE,
    Capability.HEALTH_READ,
    Capability.HEALTH_DIAGNOSTIC_RUN,
    Capability.SECURITY_QUARANTINE_FILE,
    Capability.FINANCE_READ,
    Capability.FINANCE_WRITE,
    Capability.IMPROVEMENT_READ,
    Capability.IMPROVEMENT_PROPOSE,
    Capability.IMPROVEMENT_APPROVE,
    Capability.AUTONOMOUS_READ,
    Capability.AUTONOMOUS_EXECUTE,
})


class CapabilityPolicy:
    """Allow current operations to an authenticated owner; deny unknown work."""

    def __init__(self, configured_owner_id: UUID | None) -> None:
        self._configured_owner_id = (
            configured_owner_id
            if isinstance(configured_owner_id, UUID) and configured_owner_id.int != 0
            else None
        )

    def allows(
        self,
        owner_id: UUID | None,
        capability: object,
        *,
        allow_stateless_chat: bool = False,
    ) -> bool:
        # Stateless mock chat is the sole intentional anonymous capability.
        if allow_stateless_chat:
            return owner_id is None and type(capability) is Capability and capability is Capability.CHAT_ADVISORY
        if (
            not isinstance(owner_id, UUID)
            or owner_id.int == 0
            or owner_id != self._configured_owner_id
        ):
            return False
        # Exact enum type prevents arbitrary strings, model output, plan steps,
        # or tool metadata from being interpreted as a permission grant.
        return type(capability) is Capability and capability in _OWNER_CAPABILITIES
