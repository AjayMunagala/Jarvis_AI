"""Memory update proposals: propose -> validate -> owner decision -> apply.

Nothing is written to long-term memory automatically. A proposal records what
would change, why, and which existing records it conflicts with.

Durability and consistency:

- Proposals live in a ``ProposalStore``: ``MemoryProposalStore`` (non-durable,
  used when persistence is disabled) or ``PostgresProposalStore`` (table
  ``jarvis_app.memory_update_proposals``, migration 0057, used when
  persistence is enabled). Storage errors are reported (HTTP 503); there is no
  silent fallback to memory, so durable and in-memory state never mix.
- ``mark_outdated``: the accepted proposal row *is* the durable outdated
  state. Acceptance is one conditional update (``proposed -> accepted``), so
  a decision and its effect cannot diverge. The overlay is applied during
  context selection; the memory record itself is not modified, because the
  memory schema belongs to the memory/knowledge workstream.
- ``add``: the memory record is written by a caller-supplied applier (the
  existing ``MemoryRepository.create``). The proposal is first claimed
  (``proposed -> applying``), then the applier runs, then the proposal is
  finalized (``applying -> accepted`` with the new record reference). If the
  applier fails the claim is released (``applying -> proposed`` with
  ``last_error``). If the process stops between the write and finalization the
  proposal stays ``applying`` - visible, never reported as accepted, and never
  re-applied automatically - until the owner resolves it with
  ``resolve_interrupted``.
"""

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol
from uuid import UUID

from jarvis.audit.stores import StorageError, _PostgresBase
from jarvis.browser.redaction import contains_secret
from jarvis.contextual.models import Fact, MemoryCandidate, MemoryUpdateProposal
from jarvis.contextual.selection import RELIABILITY_SCORE
from jarvis.memory.query import normalize_text

MAX_PENDING_PER_OWNER = 200


class UpdateError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ProposalStore(Protocol):
    durable: bool

    def create(self, proposal: MemoryUpdateProposal, *, max_pending: int) -> bool: ...
    def get(self, owner_id: UUID, proposal_id: str) -> MemoryUpdateProposal | None: ...
    def list(self, owner_id: UUID, *, status: str | None) -> tuple[MemoryUpdateProposal, ...]: ...
    def transition(self, owner_id: UUID, proposal_id: str, *, expected: str, status: str, decided_at: str | None,
                   applied_ref: str | None = None, last_error: str | None = None) -> MemoryUpdateProposal | None: ...
    def outdated_refs(self, owner_id: UUID) -> frozenset[str]: ...


class MemoryProposalStore:
    durable = False

    def __init__(self):
        self._items: dict[str, MemoryUpdateProposal] = {}
        self._lock = threading.Lock()

    def create(self, proposal, *, max_pending):
        with self._lock:
            pending = sum(1 for item in self._items.values()
                          if item.owner_id == proposal.owner_id and item.status in {"proposed", "applying"})
            if pending >= max_pending:
                return False
            self._items[proposal.proposal_id] = proposal
            return True

    def get(self, owner_id, proposal_id):
        item = self._items.get(proposal_id)
        return item if item is not None and item.owner_id == owner_id else None

    def list(self, owner_id, *, status):
        with self._lock:
            items = [item for item in self._items.values() if item.owner_id == owner_id
                     and (status is None or item.status == status)]
        return tuple(sorted(items, key=lambda item: item.created_at, reverse=True))

    def transition(self, owner_id, proposal_id, *, expected, status, decided_at, applied_ref=None, last_error=None):
        with self._lock:
            item = self._items.get(proposal_id)
            if item is None or item.owner_id != owner_id or item.status != expected:
                return None
            updated = item.model_copy(update={"status": status, "decided_at": decided_at,
                                              "applied_ref": applied_ref or item.applied_ref, "last_error": last_error})
            self._items[proposal_id] = updated
            return updated

    def outdated_refs(self, owner_id):
        with self._lock:
            return frozenset(item.target_ref for item in self._items.values() if item.owner_id == owner_id
                             and item.kind == "mark_outdated" and item.status == "accepted")


_COLUMNS = ("id, owner_id, kind, category, content, target_ref, fact, source_ref, reliability, status, reason, "
            "conflicts_with, created_at, decided_at, applied_ref, last_error")


def _iso(value):
    return value.isoformat(timespec="milliseconds") if hasattr(value, "isoformat") else (None if value is None else str(value))


def _json(value):
    return value if isinstance(value, (list, dict)) or value is None else json.loads(value)


class PostgresProposalStore(_PostgresBase):
    @staticmethod
    def _record(row) -> MemoryUpdateProposal:
        fact = _json(row[6])
        return MemoryUpdateProposal(
            proposal_id=row[0], owner_id=row[1], kind=row[2], category=row[3], content=row[4], target_ref=row[5],
            fact=Fact(**fact) if fact else None, source_ref=row[7], reliability=row[8], status=row[9], reason=row[10],
            conflicts_with=tuple(_json(row[11]) or ()), created_at=_iso(row[12]), decided_at=_iso(row[13]),
            applied_ref=row[14], last_error=row[15])

    def create(self, proposal, *, max_pending):
        item = proposal

        def work(execute):
            # The insert happens only while the owner's pending count is below the
            # limit, evaluated in the same statement.
            row = execute(
                f"""INSERT INTO jarvis_app.memory_update_proposals ({_COLUMNS})
                    SELECT %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
                    WHERE (SELECT count(*) FROM jarvis_app.memory_update_proposals
                           WHERE owner_id = %s AND status IN ('proposed', 'applying')) < %s
                    RETURNING id""",
                (item.proposal_id, item.owner_id, item.kind, item.category, item.content, item.target_ref,
                 json.dumps(item.fact.model_dump()) if item.fact else None, item.source_ref, item.reliability,
                 item.status, item.reason, json.dumps(list(item.conflicts_with)), item.created_at, item.decided_at,
                 item.applied_ref, item.last_error, item.owner_id, max_pending)).fetchone()
            return row is not None
        return self._run(work)

    def get(self, owner_id, proposal_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_COLUMNS} FROM jarvis_app.memory_update_proposals WHERE owner_id = %s AND id = %s",
            (owner_id, proposal_id)).fetchone())
        return self._record(row) if row else None

    def list(self, owner_id, *, status):
        def work(execute):
            if status is None:
                return execute(f"""SELECT {_COLUMNS} FROM jarvis_app.memory_update_proposals WHERE owner_id = %s
                                   ORDER BY created_at DESC, id DESC LIMIT 500""", (owner_id,)).fetchall()
            return execute(f"""SELECT {_COLUMNS} FROM jarvis_app.memory_update_proposals
                               WHERE owner_id = %s AND status = %s ORDER BY created_at DESC, id DESC LIMIT 500""",
                           (owner_id, status)).fetchall()
        return tuple(self._record(row) for row in self._run(work))

    def transition(self, owner_id, proposal_id, *, expected, status, decided_at, applied_ref=None, last_error=None):
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.memory_update_proposals
                SET status = %s, decided_at = %s, applied_ref = COALESCE(%s, applied_ref), last_error = %s
                WHERE owner_id = %s AND id = %s AND status = %s RETURNING {_COLUMNS}""",
            (status, decided_at, applied_ref, last_error, owner_id, proposal_id, expected)).fetchone())
        return self._record(row) if row else None

    def outdated_refs(self, owner_id):
        rows = self._run(lambda execute: execute(
            """SELECT DISTINCT target_ref FROM jarvis_app.memory_update_proposals
               WHERE owner_id = %s AND kind = 'mark_outdated' AND status = 'accepted' LIMIT 10000""",
            (owner_id,)).fetchall())
        return frozenset(row[0] for row in rows)


class MemoryUpdateService:
    def __init__(self, store: ProposalStore | None = None):
        self.store = store or MemoryProposalStore()

    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except StorageError as exc:
            raise UpdateError(503, "Memory proposal storage is unavailable; no decision was recorded.") from exc

    def outdated_refs(self, owner_id: UUID) -> frozenset[str]:
        return self._call(self.store.outdated_refs, owner_id)

    def _store(self, proposal: MemoryUpdateProposal) -> MemoryUpdateProposal:
        if not self._call(self.store.create, proposal, max_pending=MAX_PENDING_PER_OWNER):
            raise UpdateError(409, "Too many pending memory proposals; decide some first.")
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
        return self._call(self.store.list, owner_id, status=status)

    def _require(self, owner_id: UUID, proposal_id: str) -> MemoryUpdateProposal:
        item = self._call(self.store.get, owner_id, proposal_id)
        if item is None:
            raise UpdateError(404, "The proposal was not found.")
        return item

    def decide(self, owner_id: UUID, proposal_id: str, *, accept: bool,
               applier: Callable[[MemoryUpdateProposal], object] | None = None) -> MemoryUpdateProposal:
        item = self._require(owner_id, proposal_id)
        if item.status != "proposed":
            raise UpdateError(409, f"The proposal is already {item.status}.")
        if not accept or item.kind == "mark_outdated":
            decided = self._call(self.store.transition, owner_id, proposal_id, expected="proposed",
                                 status="accepted" if accept else "rejected", decided_at=_now())
            if decided is None:
                raise UpdateError(409, "The proposal was decided concurrently.")
            return decided
        if applier is None:
            raise UpdateError(503, "No memory store is configured to apply this proposal.")
        claimed = self._call(self.store.transition, owner_id, proposal_id, expected="proposed", status="applying",
                             decided_at=None)
        if claimed is None:
            raise UpdateError(409, "The proposal was decided concurrently.")
        try:
            created = applier(claimed)
        except Exception as exc:  # noqa: BLE001 - release the claim and report honestly
            self._call(self.store.transition, owner_id, proposal_id, expected="applying", status="proposed",
                       decided_at=None, last_error="apply_failed")
            raise UpdateError(503, "The memory could not be written; the proposal is still pending.") from exc
        ref = getattr(created, "id", None)
        finalized = self._call(self.store.transition, owner_id, proposal_id, expected="applying", status="accepted",
                               decided_at=_now(), applied_ref=f"memory:{ref}" if ref else None)
        if finalized is None:  # pragma: no cover - only reachable through concurrent manual resolution
            raise UpdateError(409, "The proposal changed while it was being applied.")
        return finalized

    def resolve_interrupted(self, owner_id: UUID, proposal_id: str, *, applied: bool,
                            applied_ref: str | None = None) -> MemoryUpdateProposal:
        """Owner resolution of an ``applying`` proposal whose outcome is unknown.

        ``applied=True`` records it as accepted (the owner confirmed the memory
        exists); ``applied=False`` returns it to ``proposed``.
        """
        item = self._require(owner_id, proposal_id)
        if item.status != "applying":
            raise UpdateError(409, "Only interrupted (applying) proposals can be resolved.")
        resolved = self._call(self.store.transition, owner_id, proposal_id, expected="applying",
                              status="accepted" if applied else "proposed",
                              decided_at=_now() if applied else None, applied_ref=applied_ref,
                              last_error=None if applied else "interrupted")
        if resolved is None:
            raise UpdateError(409, "The proposal changed concurrently.")
        return resolved
