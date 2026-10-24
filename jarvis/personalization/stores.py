"""Personalization storage: in-memory and PostgreSQL (migration 0056)."""

import json
import threading
from uuid import UUID

from jarvis.audit.stores import StorageError, _PostgresBase
from jarvis.personalization.models import Preference, PreferenceProposal

__all__ = ["MemoryPreferenceStore", "PostgresPreferenceStore", "StorageError"]


class MemoryPreferenceStore:
    durable = False

    def __init__(self):
        self._enabled: dict[UUID, bool] = {}
        self._prefs: dict[tuple[UUID, str, str], Preference] = {}
        self._proposals: dict[str, PreferenceProposal] = {}
        self._lock = threading.Lock()

    def is_enabled(self, owner_id):
        return self._enabled.get(owner_id, True)

    def set_enabled(self, owner_id, enabled, now):
        with self._lock:
            self._enabled[owner_id] = enabled

    def preferences(self, owner_id):
        with self._lock:
            return tuple(sorted((item for (owner, _, _), item in self._prefs.items() if owner == owner_id),
                                key=lambda item: (item.key, item.scope)))

    def put_preference(self, preference):
        with self._lock:
            self._prefs[(preference.owner_id, preference.key, preference.scope)] = preference

    def delete_preference(self, owner_id, key, scope):
        with self._lock:
            return self._prefs.pop((owner_id, key, scope), None) is not None

    def proposals(self, owner_id, status=None):
        with self._lock:
            items = [item for item in self._proposals.values() if item.owner_id == owner_id
                     and (status is None or item.status == status)]
        return tuple(sorted(items, key=lambda item: item.created_at, reverse=True))

    def get_proposal(self, owner_id, proposal_id):
        item = self._proposals.get(proposal_id)
        return item if item is not None and item.owner_id == owner_id else None

    def put_proposal(self, proposal):
        with self._lock:
            self._proposals[proposal.proposal_id] = proposal

    def reset(self, owner_id):
        with self._lock:
            removed = [key for key in self._prefs if key[0] == owner_id]
            for key in removed:
                del self._prefs[key]
            proposals = [key for key, item in self._proposals.items() if item.owner_id == owner_id]
            for key in proposals:
                del self._proposals[key]
            self._enabled.pop(owner_id, None)
            return len(removed) + len(proposals)


def _value(raw):
    data = raw if isinstance(raw, (list, str)) else json.loads(raw)
    return tuple(data) if isinstance(data, list) else data


def _iso(value):
    return value.isoformat(timespec="milliseconds") if hasattr(value, "isoformat") else (None if value is None else str(value))


class PostgresPreferenceStore(_PostgresBase):
    def is_enabled(self, owner_id):
        row = self._run(lambda execute: execute(
            "SELECT enabled FROM jarvis_app.personalization_settings WHERE owner_id = %s", (owner_id,)).fetchone())
        return True if row is None else bool(row[0])

    def set_enabled(self, owner_id, enabled, now):
        self._run(lambda execute: execute(
            """INSERT INTO jarvis_app.personalization_settings (owner_id, enabled, updated_at) VALUES (%s, %s, %s)
               ON CONFLICT (owner_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = EXCLUDED.updated_at""",
            (owner_id, enabled, now)))

    def preferences(self, owner_id):
        rows = self._run(lambda execute: execute(
            """SELECT owner_id, key, scope, value, provenance, updated_at FROM jarvis_app.personalization_preferences
               WHERE owner_id = %s ORDER BY key, scope""", (owner_id,)).fetchall())
        return tuple(Preference(owner_id=row[0], key=row[1], scope=row[2], value=_value(row[3]), provenance=row[4],
                                updated_at=_iso(row[5])) for row in rows)

    def put_preference(self, preference):
        item = preference
        self._run(lambda execute: execute(
            """INSERT INTO jarvis_app.personalization_preferences (owner_id, key, scope, value, provenance, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (owner_id, key, scope) DO UPDATE SET value = EXCLUDED.value,
                   provenance = EXCLUDED.provenance, updated_at = EXCLUDED.updated_at""",
            (item.owner_id, item.key, item.scope, json.dumps(list(item.value) if isinstance(item.value, tuple) else item.value),
             item.provenance, item.updated_at)))

    def delete_preference(self, owner_id, key, scope):
        rows = self._run(lambda execute: execute(
            """DELETE FROM jarvis_app.personalization_preferences WHERE owner_id = %s AND key = %s AND scope = %s
               RETURNING key""", (owner_id, key, scope)).fetchall())
        return bool(rows)

    def proposals(self, owner_id, status=None):
        def work(execute):
            base = ("SELECT id, owner_id, key, scope, value, confidence, evidence_count, basis, status, created_at, "
                    "decided_at FROM jarvis_app.personalization_proposals WHERE owner_id = %s")
            if status is None:
                return execute(base + " ORDER BY created_at DESC", (owner_id,)).fetchall()
            return execute(base + " AND status = %s ORDER BY created_at DESC", (owner_id, status)).fetchall()
        return tuple(self._proposal(row) for row in self._run(work))

    @staticmethod
    def _proposal(row):
        return PreferenceProposal(proposal_id=row[0], owner_id=row[1], key=row[2], scope=row[3], value=_value(row[4]),
                                  confidence=float(row[5]), evidence_count=int(row[6]), basis=row[7], status=row[8],
                                  created_at=_iso(row[9]), decided_at=_iso(row[10]))

    def get_proposal(self, owner_id, proposal_id):
        row = self._run(lambda execute: execute(
            """SELECT id, owner_id, key, scope, value, confidence, evidence_count, basis, status, created_at, decided_at
               FROM jarvis_app.personalization_proposals WHERE owner_id = %s AND id = %s""",
            (owner_id, proposal_id)).fetchone())
        return self._proposal(row) if row else None

    def put_proposal(self, proposal):
        item = proposal
        self._run(lambda execute: execute(
            """INSERT INTO jarvis_app.personalization_proposals
               (id, owner_id, key, scope, value, confidence, evidence_count, basis, status, created_at, decided_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, decided_at = EXCLUDED.decided_at
               WHERE jarvis_app.personalization_proposals.owner_id = EXCLUDED.owner_id""",
            (item.proposal_id, item.owner_id, item.key, item.scope,
             json.dumps(list(item.value) if isinstance(item.value, tuple) else item.value), item.confidence,
             item.evidence_count, item.basis, item.status, item.created_at, item.decided_at)))

    def reset(self, owner_id):
        def work(execute):
            prefs = execute("DELETE FROM jarvis_app.personalization_preferences WHERE owner_id = %s RETURNING key",
                            (owner_id,)).fetchall()
            proposals = execute("DELETE FROM jarvis_app.personalization_proposals WHERE owner_id = %s RETURNING id",
                                (owner_id,)).fetchall()
            execute("DELETE FROM jarvis_app.personalization_settings WHERE owner_id = %s", (owner_id,))
            return len(prefs) + len(proposals)
        return self._run(work)
