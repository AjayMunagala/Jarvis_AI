"""Persistence layer for self-improvement proposals and snapshots (v3.9)."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import threading
from typing import Any
from uuid import UUID

from jarvis.improvement.models import PromptMutationProposal


class ImprovementStore(ABC):
    @abstractmethod
    def save_proposal(self, proposal: PromptMutationProposal) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_proposal(self, owner_id: UUID, proposal_id: UUID) -> PromptMutationProposal | None:
        raise NotImplementedError

    @abstractmethod
    def list_proposals(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[PromptMutationProposal], int]:
        raise NotImplementedError

    @abstractmethod
    def save_snapshot(self, snapshot_id: UUID, target_name: str, content: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_snapshot(self, snapshot_id: UUID) -> tuple[str, str] | None:
        raise NotImplementedError


class MemoryImprovementStore(ImprovementStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[UUID, PromptMutationProposal] = {}
        # snapshot_id -> (target_name, content)
        self._snapshots: dict[UUID, tuple[str, str]] = {}

    def save_proposal(self, proposal: PromptMutationProposal) -> None:
        with self._lock:
            self._proposals[proposal.id] = proposal

    def get_proposal(self, owner_id: UUID, proposal_id: UUID) -> PromptMutationProposal | None:
        with self._lock:
            p = self._proposals.get(proposal_id)
            if p and p.owner_id == owner_id:
                return p
            return None

    def list_proposals(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[PromptMutationProposal], int]:
        with self._lock:
            matching = [p for p in self._proposals.values() if p.owner_id == owner_id]
            matching.sort(key=lambda p: p.created_at, reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def save_snapshot(self, snapshot_id: UUID, target_name: str, content: str) -> None:
        with self._lock:
            self._snapshots[snapshot_id] = (target_name, content)

    def get_snapshot(self, snapshot_id: UUID) -> tuple[str, str] | None:
        with self._lock:
            return self._snapshots.get(snapshot_id)
