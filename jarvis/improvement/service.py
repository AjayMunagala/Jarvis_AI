"""Controlled self-improvement service with offline benchmarks, diffs, and approval gates (v3.9)."""

from datetime import datetime, timezone
import difflib
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jarvis.improvement.immutable import resolve_improvement_target
from jarvis.improvement.models import (
    BenchmarkSuiteResult,
    PromptMutationProposal,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalPage,
)
from jarvis.improvement.sandbox import run_offline_benchmarks
from jarvis.improvement.stores import ImprovementStore

logger = logging.getLogger(__name__)


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
        # In-memory registry for mock/virtual target contents (e.g. prompt templates)
        self._target_storage: dict[str, str] = {
            "prompts/assistant_persona.md": "You are JARVIS, a helpful, secure AI personal assistant.",
            "prompts/summarizer.md": "Summarize the given text concisely and accurately.",
        }

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="approval",
                    event_type=event_type,
                    decision="allow",
                    status="success",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record improvement audit event: %s", exc)

    def get_target_content(self, target_name: str) -> str:
        """Retrieves target content from virtual storage or local filesystem."""
        file_path = resolve_improvement_target(self.repo_root, target_name, self._target_storage)
        if file_path is None:
            return self._target_storage[target_name]
        if file_path.is_file():
            return file_path.read_text(encoding="utf-8")
        return ""

    def set_target_content(self, target_name: str, content: str) -> None:
        """Saves target content to virtual storage or local filesystem."""
        file_path = resolve_improvement_target(self.repo_root, target_name, self._target_storage)
        if file_path is None:
            self._target_storage[target_name] = content
            return
        if file_path.is_file():  # only existing allowlisted prompt files are rewritten; nothing is created
            file_path.write_text(content, encoding="utf-8")

    def propose_mutation(
        self, owner_id: UUID, req: ProposalCreateRequest
    ) -> PromptMutationProposal:
        resolve_improvement_target(self.repo_root, req.target_name, self._target_storage)
        original = self.get_target_content(req.target_name)
        diff = generate_unified_diff(original, req.proposed_content, req.target_name)

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
        )
        self.store.save_proposal(proposal)
        self._audit(
            owner_id,
            "improvement.mutation.propose",
            f"improvement:proposal:{proposal.id}",
            {"target": proposal.target_name, "rationale": proposal.rationale},
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
        self, owner_id: UUID, proposal_id: UUID, req: ProposalDecisionRequest
    ) -> PromptMutationProposal:
        proposal = self.store.get_proposal(owner_id, proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")
        if proposal.status != "pending":
            raise ValueError(f"Proposal {proposal_id} is already {proposal.status}.")

        now = datetime.now(timezone.utc)
        snapshot_id = None

        if req.decision == "approved":
            # Save 1-click restore snapshot before mutating target
            snapshot_id = uuid4()
            self.store.save_snapshot(snapshot_id, proposal.target_name, proposal.original_content)
            # Apply mutation
            self.set_target_content(proposal.target_name, proposal.proposed_content)

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
        )
        self.store.save_proposal(updated)
        self._audit(
            owner_id,
            f"improvement.mutation.{req.decision}",
            f"improvement:proposal:{proposal.id}",
            {"decision": req.decision, "comment": req.comment},
        )
        return updated

    def restore_snapshot(
        self, owner_id: UUID, proposal_id: UUID
    ) -> PromptMutationProposal:
        proposal = self.store.get_proposal(owner_id, proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found.")
        if proposal.status != "approved" or not proposal.snapshot_id:
            raise ValueError(f"Proposal {proposal_id} does not have an active approved snapshot to restore.")

        snapshot = self.store.get_snapshot(proposal.snapshot_id)
        if not snapshot:
            raise ValueError(f"Snapshot data for {proposal.snapshot_id} not found.")

        target_name, original_content = snapshot
        # 1-click rollback: restore original content
        self.set_target_content(target_name, original_content)

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
        )
        self.store.save_proposal(restored)
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
