"""Memory update proposals: propose -> validate -> owner decision -> apply.

Nothing is written to long-term memory automatically. A proposal records what
would change, why, and which existing records it conflicts with. Accepting an
``add`` proposal calls an ``applier`` supplied by the caller (for example the
existing ``MemoryRepository.create``), so this module never owns memory
storage. ``mark_outdated`` decisions are kept as an in-memory overlay applied
during selection; making them durable requires a reviewed change to the memory
schema, which belongs to the knowledge/memory workstream.
"""

import secrets
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable
from uuid import UUID

from jarvis.contextual.models import Fact, MemoryCandidate, MemoryUpdateProposal
from jarvis.contextual.selection import RELIABILITY_SCORE
from jarvis.memory.query import normalize_text
from jarvis.browser.redaction import contains_secret

MAX_PENDING_PER_OWNER = 200


class UpdateError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MemoryUpdateService:
    durable = False

    def __init__(self):
        self._proposals: dict[str, MemoryUpdateProposal] = {}
        self._outdated: dict[UUID, set[str]] = {}
        self._lock = threading.Lock()

    def outdated_refs(self, owner_id: UUID) -> frozenset[str]:
        with self._lock:
            return frozenset(self._outdated.get(owner_id, set()))

    def _store(self, proposal: MemoryUpdateProposal) -> MemoryUpdateProposal:
        with self._lock:
            pending = sum(1 for item in self._proposals.values()
                          if item.owner_id == proposal.owner_id and item.status == "proposed")
            if pending >= MAX_PENDING_PER_OWNER:
                raise UpdateError(409, "Too many pending memory proposals; decide some first.")
            self._proposals[proposal.proposal_id] = proposal
        return proposal

    def propose_add(self, owner_id: UUID, *, category: str, content: str, source_ref: str, reliability: str,
                    existing: Iterable[MemoryCandidate] = (), fact: Fact | None = None,
                    reason: str = "New information was observed.") -> MemoryUpdateProposal:
        if contains_secret(content):
            raise UpdateError(422, "The content appears to contain a credential and was not proposed.")
        existing = [item for item in existing if item.owner_id == owner_id]
        fingerprint = " ".join(normalize_text(content).split())
        if any(" ".join(normalize_text(item.content).split()) == fingerprint for item in existing):
            raise UpdateError(409, "An identical memory already exists.")
        conflicts = tuple(item.candidate_id for item in existing if fact is not None and item.fact is not None
                          and item.fact.key == fact.key and item.fact.normalized_value != fact.normalized_value)
        note = reason
        if conflicts:
            stronger = [item for item in existing if item.candidate_id in conflicts
                        and RELIABILITY_SCORE[item.reliability] > RELIABILITY_SCORE[reliability]]
            note += (" It contradicts a more reliable existing record, which will be kept unless you decide otherwise."
                     if stronger else " It contradicts existing records; accepting it does not delete them.")
        return self._store(MemoryUpdateProposal(
            proposal_id="MP-" + secrets.token_hex(8), owner_id=owner_id, kind="add", category=category,
            content=content, fact=fact, source_ref=source_ref, reliability=reliability, reason=note[:500],
            conflicts_with=conflicts[:10], created_at=_now()))

    def propose_outdated(self, owner_id: UUID, *, target_ref: str, source_ref: str, reason: str,
                         category: str = "long_term") -> MemoryUpdateProposal:
        return self._store(MemoryUpdateProposal(
            proposal_id="MP-" + secrets.token_hex(8), owner_id=owner_id, kind="mark_outdated", category=category,
            target_ref=target_ref, source_ref=source_ref, reliability="user_stated", reason=reason[:500],
            created_at=_now()))

    def list(self, owner_id: UUID, *, status: str | None = None) -> tuple[MemoryUpdateProposal, ...]:
        with self._lock:
            items = [item for item in self._proposals.values() if item.owner_id == owner_id
                     and (status is None or item.status == status)]
        return tuple(sorted(items, key=lambda item: item.created_at, reverse=True))

    def decide(self, owner_id: UUID, proposal_id: str, *, accept: bool,
               applier: Callable[[MemoryUpdateProposal], object] | None = None) -> MemoryUpdateProposal:
        with self._lock:
            item = self._proposals.get(proposal_id)
            if item is None or item.owner_id != owner_id:
                raise UpdateError(404, "The proposal was not found.")
            if item.status != "proposed":
                raise UpdateError(409, f"The proposal is already {item.status}.")
        if accept and item.kind == "add":
            if applier is None:
                raise UpdateError(503, "No memory store is configured to apply this proposal.")
            applier(item)  # raises on failure; the proposal then stays pending
        with self._lock:
            if accept and item.kind == "mark_outdated":
                self._outdated.setdefault(owner_id, set()).add(item.target_ref)
            decided = item.model_copy(update={"status": "accepted" if accept else "rejected", "decided_at": _now()})
            self._proposals[proposal_id] = decided
            return decided
