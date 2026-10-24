"""Persistence layer for self-improvement proposals and snapshots (v3.9).

v4.9: proposals, restore snapshots and the content of virtual prompt targets
are durable in PostgreSQL (migration 0066) when persistence is enabled.
Snapshots are owner-scoped: an owner can only read or restore their own.
The content of virtual prompt targets is deliberately global, like the
file-backed prompts: a deployment has one configured owner.

Applying and restoring change two things, the target content and the
proposal status. Both use conditional writes (compare-and-set on the content,
and on the proposal's current status), so two workers can never both apply
competing changes to the same content.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import threading
from uuid import UUID

from jarvis.improvement.models import PromptMutationProposal


class ImprovementStorageError(RuntimeError):
    """Sanitized storage failure."""


class ImprovementStore(ABC):
    durable = False

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
    def save_snapshot(self, owner_id: UUID, snapshot_id: UUID, target_name: str, content: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_snapshot(self, owner_id: UUID, snapshot_id: UUID) -> tuple[str, str] | None:
        raise NotImplementedError

    @abstractmethod
    def get_target_content(self, target_name: str) -> str | None:
        """Stored content of a virtual prompt target, or None when it still has its built-in default."""
        raise NotImplementedError

    @abstractmethod
    def set_target_content(self, target_name: str, content: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def replace_target_content(self, target_name: str, expected: str, content: str, *, default: str) -> bool:
        """Set the content only if it currently equals ``expected`` (``default`` when no row is stored)."""
        raise NotImplementedError

    @abstractmethod
    def update_proposal_status(self, proposal: PromptMutationProposal, *, from_status: str) -> bool:
        """Store ``proposal``'s status, snapshot, decision time and evaluation only if it is still ``from_status``."""
        raise NotImplementedError


class MemoryImprovementStore(ImprovementStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[UUID, PromptMutationProposal] = {}
        # snapshot_id -> (owner_id, target_name, content)
        self._snapshots: dict[UUID, tuple[UUID, str, str]] = {}
        self._targets: dict[str, str] = {}

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

    def save_snapshot(self, owner_id: UUID, snapshot_id: UUID, target_name: str, content: str) -> None:
        with self._lock:
            self._snapshots[snapshot_id] = (owner_id, target_name, content)

    def get_snapshot(self, owner_id: UUID, snapshot_id: UUID) -> tuple[str, str] | None:
        with self._lock:
            found = self._snapshots.get(snapshot_id)
        if found is None or found[0] != owner_id:
            return None
        return found[1], found[2]

    def get_target_content(self, target_name: str) -> str | None:
        with self._lock:
            return self._targets.get(target_name)

    def set_target_content(self, target_name: str, content: str) -> None:
        with self._lock:
            self._targets[target_name] = content

    def replace_target_content(self, target_name: str, expected: str, content: str, *, default: str) -> bool:
        with self._lock:
            if self._targets.get(target_name, default) != expected:
                return False
            self._targets[target_name] = content
            return True

    def update_proposal_status(self, proposal: PromptMutationProposal, *, from_status: str) -> bool:
        with self._lock:
            current = self._proposals.get(proposal.id)
            if current is None or current.owner_id != proposal.owner_id or current.status != from_status:
                return False
            self._proposals[proposal.id] = proposal
            return True


_PROPOSAL_COLUMNS = ("id, owner_id, target_name, original_content, proposed_content, diff, rationale, status, "
                     "snapshot_id, evaluation, created_at, decided_at")


class PostgresImprovementStore(ImprovementStore):
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5):
        self.database_url = database_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds

    def _run(self, work):
        import psycopg

        try:
            with psycopg.connect(self.database_url, connect_timeout=int(self.connect_timeout_seconds),
                                 options=f"-c statement_timeout={int(self.operation_timeout_seconds * 1000)}") as conn:
                return work(conn)
        except Exception as exc:
            raise ImprovementStorageError("Improvement storage is unavailable.") from exc

    @staticmethod
    def _proposal(row) -> PromptMutationProposal:
        evaluation = row[9] if isinstance(row[9], (dict, type(None))) else json.loads(row[9])
        return PromptMutationProposal(id=row[0], owner_id=row[1], target_name=row[2], original_content=row[3],
                                      proposed_content=row[4], diff=row[5], rationale=row[6], status=row[7],
                                      snapshot_id=row[8], evaluation=evaluation, created_at=row[10],
                                      decided_at=row[11])

    def save_proposal(self, proposal: PromptMutationProposal) -> None:
        self._run(lambda conn: conn.execute(
            f"""INSERT INTO jarvis_app.improvement_proposals ({_PROPOSAL_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, snapshot_id = EXCLUDED.snapshot_id,
                    evaluation = EXCLUDED.evaluation, decided_at = EXCLUDED.decided_at
                WHERE jarvis_app.improvement_proposals.owner_id = EXCLUDED.owner_id""",
            (proposal.id, proposal.owner_id, proposal.target_name, proposal.original_content,
             proposal.proposed_content, proposal.diff, proposal.rationale, proposal.status, proposal.snapshot_id,
             json.dumps(proposal.evaluation) if proposal.evaluation is not None else None, proposal.created_at,
             proposal.decided_at)))

    def get_proposal(self, owner_id: UUID, proposal_id: UUID) -> PromptMutationProposal | None:
        row = self._run(lambda conn: conn.execute(
            f"SELECT {_PROPOSAL_COLUMNS} FROM jarvis_app.improvement_proposals WHERE id = %s AND owner_id = %s",
            (proposal_id, owner_id)).fetchone())
        return self._proposal(row) if row else None

    def list_proposals(self, owner_id: UUID, limit: int = 50, offset: int = 0):
        def work(conn):
            total = conn.execute("SELECT count(*) FROM jarvis_app.improvement_proposals WHERE owner_id = %s",
                                 (owner_id,)).fetchone()[0]
            rows = conn.execute(
                f"""SELECT {_PROPOSAL_COLUMNS} FROM jarvis_app.improvement_proposals WHERE owner_id = %s
                    ORDER BY created_at DESC, id LIMIT %s OFFSET %s""", (owner_id, limit, offset)).fetchall()
            return [self._proposal(row) for row in rows], total
        return self._run(work)

    def save_snapshot(self, owner_id: UUID, snapshot_id: UUID, target_name: str, content: str) -> None:
        self._run(lambda conn: conn.execute(
            """INSERT INTO jarvis_app.improvement_snapshots (id, owner_id, target_name, content, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (snapshot_id, owner_id, target_name, content, datetime.now(timezone.utc))))

    def get_snapshot(self, owner_id: UUID, snapshot_id: UUID) -> tuple[str, str] | None:
        row = self._run(lambda conn: conn.execute(
            "SELECT target_name, content FROM jarvis_app.improvement_snapshots WHERE id = %s AND owner_id = %s",
            (snapshot_id, owner_id)).fetchone())
        return (row[0], row[1]) if row else None

    def get_target_content(self, target_name: str) -> str | None:
        row = self._run(lambda conn: conn.execute(
            "SELECT content FROM jarvis_app.improvement_target_contents WHERE target_name = %s",
            (target_name,)).fetchone())
        return row[0] if row else None

    def replace_target_content(self, target_name: str, expected: str, content: str, *, default: str) -> bool:
        now = datetime.now(timezone.utc)
        if expected == default:  # no stored row also means "default"
            row = self._run(lambda conn: conn.execute(
                """INSERT INTO jarvis_app.improvement_target_contents (target_name, content, updated_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (target_name) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at
                   WHERE jarvis_app.improvement_target_contents.content = %s
                   RETURNING target_name""", (target_name, content, now, expected)).fetchone())
        else:
            row = self._run(lambda conn: conn.execute(
                """UPDATE jarvis_app.improvement_target_contents SET content = %s, updated_at = %s
                   WHERE target_name = %s AND content = %s RETURNING target_name""",
                (content, now, target_name, expected)).fetchone())
        return row is not None

    def update_proposal_status(self, proposal: PromptMutationProposal, *, from_status: str) -> bool:
        row = self._run(lambda conn: conn.execute(
            """UPDATE jarvis_app.improvement_proposals
               SET status = %s, snapshot_id = %s, decided_at = %s, evaluation = %s
               WHERE id = %s AND owner_id = %s AND status = %s RETURNING id""",
            (proposal.status, proposal.snapshot_id, proposal.decided_at,
             json.dumps(proposal.evaluation) if proposal.evaluation is not None else None,
             proposal.id, proposal.owner_id, from_status)).fetchone())
        return row is not None

    def set_target_content(self, target_name: str, content: str) -> None:
        self._run(lambda conn: conn.execute(
            """INSERT INTO jarvis_app.improvement_target_contents (target_name, content, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (target_name) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at""",
            (target_name, content, datetime.now(timezone.utc))))
