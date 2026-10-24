"""Controlled self-improvement service with offline benchmarks, diffs, and approval gates (v3.9)."""

from datetime import datetime, timezone
import difflib
import hashlib
import logging
from pathlib import Path
import threading
from typing import Any
from uuid import UUID, uuid4

from jarvis.audit.models import ApprovalProposal
from jarvis.audit.service import run_approved
from jarvis.improvement.immutable import resolve_improvement_target
from jarvis.improvement.models import (
    BenchmarkSuiteResult,
    PromptMutationProposal,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalPage,
)
from jarvis.improvement.sandbox import compare_with_baseline, run_offline_benchmarks
from jarvis.improvement.stores import ImprovementStore

logger = logging.getLogger(__name__)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_unified_diff(original: str, proposed: str, filename: str) -> str:
    """Computes standard unified diff between original and proposed content."""
    orig_lines = original.splitlines(keepends=True)
    prop_lines = proposed.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        prop_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(diff)


class ImprovementService:
    def __init__(
        self,
        store: ImprovementStore,
        repo_root: Path | None = None,
        audit_service: Any = None,
    ) -> None:
        self.store = store
        self.repo_root = repo_root or Path.cwd()
        self.audit_service = audit_service
        self.approval_service: Any = None  # v4.3: apply and restore run only under an approved request
        self._apply_lock = threading.Lock()  # one apply/restore at a time, re-checked after the approval is used
        # Built-in defaults of the virtual targets (prompt templates without a file). v4.9: changes to them are
        # kept in the store, so they survive restarts when persistence is enabled.
        self._target_storage: dict[str, str] = {
            "prompts/assistant_persona.md": "You are JARVIS, a helpful, secure AI personal assistant.",
            "prompts/summarizer.md": "Summarize the given text concisely and accurately.",
        }

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="improvement",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record improvement audit event: %s", exc)

    def get_target_content(self, target_name: str) -> str:
        """Retrieves target content from virtual storage or local filesystem."""
        file_path = resolve_improvement_target(self.repo_root, target_name, self._target_storage)
        if file_path is None:
            stored = self.store.get_target_content(target_name)
            return stored if stored is not None else self._target_storage[target_name]
        if file_path.is_file():
            return file_path.read_text(encoding="utf-8")
        return ""

    def _replace_target(self, target_name: str, expected: str, content: str) -> bool:
        """Compare-and-set the target content (v4.9). Virtual targets are atomic in the store; file targets are
        compared and written under the process lock (one process writes prompt files)."""
        file_path = resolve_improvement_target(self.repo_root, target_name, self._target_storage)
        if file_path is None:
            return self.store.replace_target_content(target_name, expected, content,
                                                     default=self._target_storage[target_name])
        if self.get_target_content(target_name) != expected:
            return False
        self.set_target_content(target_name, content)
        return True

    def set_target_content(self, target_name: str, content: str) -> None:
        """Saves target content to virtual storage or local filesystem."""
        file_path = resolve_improvement_target(self.repo_root, target_name, self._target_storage)
        if file_path is None:
            self.store.set_target_content(target_name, content)
            return
        if file_path.is_file():  # only existing allowlisted prompt files are rewritten; nothing is created
            file_path.write_text(content, encoding="utf-8")

    def propose_mutation(
        self, owner_id: UUID, req: ProposalCreateRequest
    ) -> PromptMutationProposal:
        resolve_improvement_target(self.repo_root, req.target_name, self._target_storage)
        original = self.get_target_content(req.target_name)
        diff = generate_unified_diff(original, req.proposed_content, req.target_name)
        # v4.9: every proposal is evaluated against the content it would replace; approval needs a clean result.
        evaluation = compare_with_baseline(original, req.proposed_content)

        now = datetime.now(timezone.utc)
        proposal = PromptMutationProposal(
            id=uuid4(),
            owner_id=owner_id,
            target_name=req.target_name,
            original_content=original,
            proposed_content=req.proposed_content,
            diff=diff,
            rationale=req.rationale,
            status="pending",
            snapshot_id=None,
            created_at=now,
            decided_at=None,
            evaluation=evaluation,
        )
        self.store.save_proposal(proposal)
        self._audit(
            owner_id,
            "improvement.mutation.propose",
            f"improvement:proposal:{proposal.id}",
            {"target": proposal.target_name, "acceptable": evaluation["acceptable"],
             "regressions": len(evaluation["regressions"]), "changed_rules": len(evaluation["changed_rules"])},
        )
        return proposal

    def get_proposal(
        self, owner_id: UUID, proposal_id: UUID
    ) -> PromptMutationProposal | None:
        return self.store.get_proposal(owner_id, proposal_id)

    def list_proposals(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> ProposalPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_proposals(owner_id, limit=limit, offset=offset)
        return ProposalPage(items=items, total=total)

    def decide_proposal(
        self, owner_id: UUID, proposal_id: UUID, req: ProposalDecisionRequest, approval_id: str | None = None
    ) -> PromptMutationProposal:
        proposal = self.store.get_proposal(owner_id, proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")
        if proposal.status != "pending":
            raise ValueError(f"Proposal {proposal_id} is already {proposal.status}.")
        if req.decision != "approved":
            return self._decide(owner_id, proposal, req)
        # v4.3: applying writes the target, so it needs an approved request bound to this exact change.
        if self.get_target_content(proposal.target_name) != proposal.original_content:
            raise ValueError("The target has changed since this proposal was made; nothing was applied. "
                             "Create a new proposal.")
        # v4.9: re-evaluated at decision time. Regressions block approval; a rule-line change alone can be
        # acknowledged explicitly by the owner, and the approval request then names the changed rule lines.
        evaluation = compare_with_baseline(proposal.original_content, proposal.proposed_content)
        acknowledged = bool(evaluation["rule_changes_only"] and req.acknowledge_rule_changes)
        if not evaluation["acceptable"] and not acknowledged:
            problems = "; ".join(evaluation["regressions"])
            hint = (" It only changes rule lines; to apply it anyway, decide again with acknowledge_rule_changes "
                    "set to true." if evaluation["rule_changes_only"] else "")
            raise ValueError(f"The proposal fails its evaluation ({problems}); nothing was applied.{hint}")
        reason = proposal.rationale
        if acknowledged:
            reason = "RULE CHANGES ACKNOWLEDGED: " + " | ".join(evaluation["changed_rules"]) + ". " + reason
        approval = ApprovalProposal(
            category="improvement", action="improvement.apply", target=proposal.target_name,
            reason=reason[:500], risk="high",
            expected_side_effects=f"Replaces the content of {proposal.target_name}; a restore snapshot is saved first."[:300],
            scope_material=f"{proposal.id}|{proposal.target_name}|{_sha(proposal.original_content)}|"
                           f"{_sha(proposal.proposed_content)}|ack={acknowledged}",
        )
        return run_approved(self.approval_service, owner_id, approval, approval_id,
                            lambda: self._apply_if_still_pending(owner_id, proposal, req))

    def _apply_if_still_pending(
        self, owner_id: UUID, proposal: PromptMutationProposal, req: ProposalDecisionRequest
    ) -> PromptMutationProposal:
        with self._apply_lock:
            current = self.store.get_proposal(owner_id, proposal.id)
            if current is None or current.status != "pending":
                raise ValueError(f"Proposal {proposal.id} is no longer pending; nothing was applied.")
            if self.get_target_content(proposal.target_name) != proposal.original_content:
                raise ValueError("The target has changed since this proposal was made; nothing was applied. "
                                 "Create a new proposal.")
            return self._decide(owner_id, proposal, req)

    def _decide(
        self, owner_id: UUID, proposal: PromptMutationProposal, req: ProposalDecisionRequest
    ) -> PromptMutationProposal:
        now = datetime.now(timezone.utc)
        snapshot_id = None

        if req.decision == "approved":
            # Save 1-click restore snapshot before mutating target
            snapshot_id = uuid4()
            self.store.save_snapshot(owner_id, snapshot_id, proposal.target_name, proposal.original_content)

        updated = PromptMutationProposal(
            id=proposal.id,
            owner_id=proposal.owner_id,
            target_name=proposal.target_name,
            original_content=proposal.original_content,
            proposed_content=proposal.proposed_content,
            diff=proposal.diff,
            rationale=proposal.rationale,
            status=req.decision,
            snapshot_id=snapshot_id,
            created_at=proposal.created_at,
            decided_at=now,
            evaluation=proposal.evaluation,  # v4.9: the evaluation stays with the proposal
        )
        # v4.9: the status changes only from "pending" (another worker cannot decide it too) ...
        if not self.store.update_proposal_status(updated, from_status="pending"):
            raise ValueError(f"Proposal {proposal.id} is no longer pending; nothing was applied.")
        # ... and the content only from the content the proposal was made against.
        if req.decision == "approved" and not self._replace_target(proposal.target_name, proposal.original_content,
                                                                   proposal.proposed_content):
            self.store.update_proposal_status(proposal, from_status="approved")  # back to pending
            raise ValueError("The target changed while the proposal was being applied; nothing was applied.")
        self._audit(
            owner_id,
            f"improvement.mutation.{req.decision}",
            f"improvement:proposal:{proposal.id}",
            {"decision": req.decision, "rule_changes_acknowledged": req.acknowledge_rule_changes},
        )
        return updated

    def restore_snapshot(
        self, owner_id: UUID, proposal_id: UUID, approval_id: str | None = None
    ) -> PromptMutationProposal:
        proposal = self.store.get_proposal(owner_id, proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")
        if proposal.status != "approved" or not proposal.snapshot_id:
            raise ValueError(f"Proposal {proposal_id} does not have an active approved snapshot to restore.")

        snapshot = self.store.get_snapshot(owner_id, proposal.snapshot_id)  # v4.9: owner-scoped
        if not snapshot:
            raise ValueError(f"Snapshot data for {proposal.snapshot_id} not found.")

        target_name, original_content = snapshot
        current = self.get_target_content(target_name)
        if current != proposal.proposed_content:
            # v4.9: restoring across later changes would silently undo them without any evaluation.
            raise ValueError("The target has changed since this proposal was applied (a later change exists); "
                             "nothing was restored. Create a new proposal instead.")
        approval = ApprovalProposal(
            category="improvement", action="improvement.restore", target=target_name,
            reason=f"Restore {target_name} to its content before proposal {proposal.id}"[:500], risk="high",
            expected_side_effects=f"Replaces the current content of {target_name} with the saved snapshot."[:300],
            scope_material=f"{proposal.id}|{proposal.snapshot_id}|{_sha(current)}|{_sha(original_content)}",
        )
        return run_approved(self.approval_service, owner_id, approval, approval_id,
                            lambda: self._restore_if_unchanged(owner_id, proposal, target_name, original_content,
                                                               current))

    def _restore_if_unchanged(self, owner_id: UUID, proposal: PromptMutationProposal, target_name: str,
                              original_content: str, approved_current: str) -> PromptMutationProposal:
        with self._apply_lock:
            latest = self.store.get_proposal(owner_id, proposal.id)
            if latest is None or latest.status != "approved" or self.get_target_content(target_name) != approved_current:
                raise ValueError(f"Proposal {proposal.id} changed after the restore was approved; nothing was restored.")
            return self._restore(owner_id, proposal, target_name, original_content)

    def _restore(self, owner_id: UUID, proposal: PromptMutationProposal, target_name: str,
                 original_content: str) -> PromptMutationProposal:
        # 1-click rollback: restore original content, only from the content this proposal applied
        if not self._replace_target(target_name, proposal.proposed_content, original_content):
            raise ValueError("The target changed while it was being restored; nothing was restored.")

        now = datetime.now(timezone.utc)
        restored = PromptMutationProposal(
            id=proposal.id,
            owner_id=proposal.owner_id,
            target_name=proposal.target_name,
            original_content=proposal.original_content,
            proposed_content=proposal.proposed_content,
            diff=proposal.diff,
            rationale=proposal.rationale,
            status="restored",
            snapshot_id=proposal.snapshot_id,
            created_at=proposal.created_at,
            decided_at=now,
            evaluation=proposal.evaluation,  # v4.9: the evaluation stays with the proposal
        )
        if not self.store.update_proposal_status(restored, from_status="approved"):
            raise ValueError(f"Proposal {proposal.id} was restored concurrently.")
        self._audit(
            owner_id,
            "improvement.mutation.restore",
            f"improvement:proposal:{proposal.id}",
            {"restored_target": target_name},
        )
        return restored

    def evaluate_benchmarks(
        self, target_name: str | None = None
    ) -> BenchmarkSuiteResult:
        content = self.get_target_content(target_name) if target_name else None
        return run_offline_benchmarks(prompt_text=content)
