"""Knowledge graph storage: in-memory and PostgreSQL (migration 0052).

Every query is owner-scoped. In PostgreSQL, composite foreign keys
``(owner_id, entity_id)`` make a cross-owner edge impossible at the database
level, not only in application code.

Concurrency: multi-step graph updates run inside ``atomic(owner_id, keys)``.

- PostgreSQL: one transaction on one connection; each key is serialized with a
  transaction-scoped advisory lock (``pg_advisory_xact_lock``) derived from the
  owner and the key, acquired in sorted order. Locks are released at commit or
  rollback; any exception inside the block rolls the whole update back.
  Writers in different server processes therefore serialize on the same keys
  (for example one subject/relation pair), while unrelated updates proceed in
  parallel. Unique-key and foreign-key violations are raised as
  ``StorageConflict`` (the update lost a race and changed nothing).
- Memory: per-key re-entrant locks within the process.
"""

import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from time import monotonic
from typing import Iterable, Protocol
from uuid import UUID

from jarvis.audit.stores import StorageError, _PostgresBase
from jarvis.knowledge_graph.models import Entity, Provenance, Relationship
from jarvis.persistence.operation_timeout import bounded_connect_timeout, execute_with_deadline, operation_deadline

__all__ = ["GraphStore", "MemoryGraphStore", "PostgresGraphStore", "StorageConflict", "StorageError", "lock_id"]


class StorageConflict(StorageError):
    """A uniqueness or foreign-key constraint rejected a concurrent change."""


def lock_id(owner_id: UUID, key: str) -> int:
    """Stable signed 64-bit advisory-lock id for (owner, key)."""
    digest = hashlib.sha256(f"jarvis-kg|{owner_id}|{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class GraphStore(Protocol):
    durable: bool

    def atomic(self, owner_id: UUID, keys: Iterable[str]): ...
    def count(self, owner_id: UUID) -> tuple[int, int]: ...
    def find_entity(self, owner_id: UUID, entity_type: str, key: str) -> Entity | None: ...
    def get_entity(self, owner_id: UUID, entity_id: str) -> Entity | None: ...
    def save_entity(self, entity: Entity) -> None: ...
    def search_entities(self, owner_id: UUID, *, text: str | None, entity_type: str | None, limit: int) -> tuple[Entity, ...]: ...
    def delete_entity(self, owner_id: UUID, entity_id: str) -> int: ...
    def get_relationship(self, owner_id: UUID, relationship_id: str) -> Relationship | None: ...
    def find_relationships(self, owner_id: UUID, subject_id: str, relation: str) -> tuple[Relationship, ...]: ...
    def save_relationship(self, relationship: Relationship) -> None: ...
    def edges(self, owner_id: UUID, entity_id: str, *, limit: int) -> tuple[Relationship, ...]: ...


# ------------------------------------------------------------------ memory

_MISSING = object()


class _MemoryTx:
    """View returned by ``MemoryGraphStore.atomic``; can take further keys.

    Writes are recorded in an undo log so an exception inside the block
    restores exactly the rows this transaction touched (all-or-nothing, like
    the PostgreSQL transaction) without disturbing other keys' writers.
    """

    def __init__(self, store: "MemoryGraphStore", owner_id: UUID, held: list):
        self._store, self._owner, self._held = store, owner_id, held
        self._undo: list[tuple[dict, str, object]] = []

    def lock(self, keys: Iterable[str]) -> None:
        for key in sorted(set(keys)):
            lock = self._store._key_lock(self._owner, key)
            lock.acquire()
            self._held.append(lock)

    def _remember(self, table: dict, key: str) -> None:
        self._undo.append((table, key, table.get(key, _MISSING)))

    def save_entity(self, entity):
        self._remember(self._store._entities, entity.entity_id)
        self._store.save_entity(entity)

    def save_relationship(self, relationship):
        self._remember(self._store._relationships, relationship.relationship_id)
        self._store.save_relationship(relationship)

    def delete_entity(self, owner_id, entity_id):
        self._remember(self._store._entities, entity_id)
        for key, item in list(self._store._relationships.items()):
            if item.owner_id == owner_id and entity_id in (item.subject_id, item.object_id):
                self._remember(self._store._relationships, key)
        return self._store.delete_entity(owner_id, entity_id)

    def rollback(self) -> None:
        with self._store._lock:
            for table, key, previous in reversed(self._undo):
                if previous is _MISSING:
                    table.pop(key, None)
                else:
                    table[key] = previous
        self._undo.clear()

    def __getattr__(self, name):
        return getattr(self._store, name)


class MemoryGraphStore:
    durable = False

    def __init__(self):
        self._entities: dict[str, Entity] = {}
        self._relationships: dict[str, Relationship] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[tuple, threading.RLock] = {}

    def _key_lock(self, owner_id, key):
        with self._lock:
            return self._key_locks.setdefault((owner_id, key), threading.RLock())

    @contextmanager
    def atomic(self, owner_id, keys=()):
        held: list = []
        tx = _MemoryTx(self, owner_id, held)
        try:
            tx.lock(keys)
            yield tx
        except BaseException:
            tx.rollback()
            raise
        finally:
            for lock in reversed(held):
                lock.release()

    def lock(self, keys):  # outside atomic(): nothing to hold
        return None

    def count(self, owner_id):
        with self._lock:
            return (sum(item.owner_id == owner_id for item in self._entities.values()),
                    sum(item.owner_id == owner_id for item in self._relationships.values()))

    def find_entity(self, owner_id, entity_type, key):
        with self._lock:
            return next((item for item in self._entities.values() if item.owner_id == owner_id
                         and item.entity_type == entity_type and item.key == key), None)

    def get_entity(self, owner_id, entity_id):
        item = self._entities.get(entity_id)
        return item if item is not None and item.owner_id == owner_id else None

    def save_entity(self, entity):
        with self._lock:
            current = self._entities.get(entity.entity_id)
            if current is not None and current.owner_id != entity.owner_id:
                raise StorageError()
            duplicate = next((item for item in self._entities.values() if item.owner_id == entity.owner_id
                              and item.entity_type == entity.entity_type and item.key == entity.key
                              and item.entity_id != entity.entity_id), None)
            if duplicate is not None:
                raise StorageConflict()
            self._entities[entity.entity_id] = entity

    def search_entities(self, owner_id, *, text, entity_type, limit):
        needle = (text or "").strip().lower()
        with self._lock:
            items = [item for item in self._entities.values() if item.owner_id == owner_id
                     and (entity_type is None or item.entity_type == entity_type)
                     and (not needle or needle in item.key or any(needle in alias.lower() for alias in item.aliases))]
        return tuple(sorted(items, key=lambda item: (item.key, item.entity_id))[:limit])

    def delete_entity(self, owner_id, entity_id):
        with self._lock:
            item = self._entities.get(entity_id)
            if item is None or item.owner_id != owner_id:
                return 0
            del self._entities[entity_id]
            doomed = [key for key, rel in self._relationships.items() if rel.owner_id == owner_id
                      and entity_id in (rel.subject_id, rel.object_id)]
            for key in doomed:
                del self._relationships[key]
            return 1 + len(doomed)

    def get_relationship(self, owner_id, relationship_id):
        item = self._relationships.get(relationship_id)
        return item if item is not None and item.owner_id == owner_id else None

    def find_relationships(self, owner_id, subject_id, relation):
        with self._lock:
            return tuple(sorted((item for item in self._relationships.values() if item.owner_id == owner_id
                                 and item.subject_id == subject_id and item.relation == relation),
                                key=lambda item: item.created_at))

    def save_relationship(self, relationship):
        with self._lock:
            current = self._relationships.get(relationship.relationship_id)
            if current is not None and current.owner_id != relationship.owner_id:
                raise StorageError()
            for endpoint in (relationship.subject_id, relationship.object_id):
                entity = self._entities.get(endpoint)
                if entity is None or entity.owner_id != relationship.owner_id:
                    raise StorageConflict()  # mirrors the PostgreSQL composite foreign keys
            self._relationships[relationship.relationship_id] = relationship

    def edges(self, owner_id, entity_id, *, limit):
        with self._lock:
            items = [item for item in self._relationships.values() if item.owner_id == owner_id
                     and entity_id in (item.subject_id, item.object_id)]
        return tuple(sorted(items, key=lambda item: (item.created_at, item.relationship_id))[:limit])


# --------------------------------------------------------------- postgres

_ENTITY_COLUMNS = "id, owner_id, entity_type, key, name, aliases, attributes, provenance, created_at, updated_at"
_REL_COLUMNS = ("id, owner_id, subject_id, relation, object_id, status, confidence, provenance, conflicts_with, "
                "superseded_by, created_at, updated_at")
_CONFLICT_STATES = {"23505", "23503"}  # unique_violation, foreign_key_violation


def _json(value):
    return value if isinstance(value, (list, dict)) else json.loads(value or "null")


def _iso(value):
    return value.isoformat(timespec="milliseconds") if hasattr(value, "isoformat") else str(value)


def _entity(row) -> Entity:
    return Entity(entity_id=row[0], owner_id=row[1], entity_type=row[2], key=row[3], name=row[4],
                  aliases=tuple(_json(row[5]) or ()), attributes=_json(row[6]) or {},
                  provenance=tuple(Provenance(**item) for item in _json(row[7]) or ()),
                  created_at=_iso(row[8]), updated_at=_iso(row[9]))


def _relationship(row) -> Relationship:
    return Relationship(relationship_id=row[0], owner_id=row[1], subject_id=row[2], relation=row[3],
                        object_id=row[4], status=row[5], confidence=float(row[6]),
                        provenance=tuple(Provenance(**item) for item in _json(row[7])),
                        conflicts_with=tuple(_json(row[8]) or ()), superseded_by=row[9],
                        created_at=_iso(row[10]), updated_at=_iso(row[11]))


class _SqlOps:
    """SQL operations over an ``execute(query, params)`` callable."""

    def __init__(self, execute):
        self.execute = execute

    def count(self, owner_id):
        row = self.execute(
            """SELECT (SELECT count(*) FROM jarvis_app.kg_entities WHERE owner_id = %s),
                      (SELECT count(*) FROM jarvis_app.kg_relationships WHERE owner_id = %s)""",
            (owner_id, owner_id)).fetchone()
        return int(row[0]), int(row[1])

    def find_entity(self, owner_id, entity_type, key):
        row = self.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM jarvis_app.kg_entities WHERE owner_id = %s AND entity_type = %s AND key = %s",
            (owner_id, entity_type, key)).fetchone()
        return _entity(row) if row else None

    def get_entity(self, owner_id, entity_id):
        row = self.execute(f"SELECT {_ENTITY_COLUMNS} FROM jarvis_app.kg_entities WHERE owner_id = %s AND id = %s",
                           (owner_id, entity_id)).fetchone()
        return _entity(row) if row else None

    def save_entity(self, entity):
        self.execute(
            f"""INSERT INTO jarvis_app.kg_entities ({_ENTITY_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, aliases = EXCLUDED.aliases,
                    attributes = EXCLUDED.attributes, provenance = EXCLUDED.provenance, updated_at = EXCLUDED.updated_at
                WHERE jarvis_app.kg_entities.owner_id = EXCLUDED.owner_id""",
            (entity.entity_id, entity.owner_id, entity.entity_type, entity.key, entity.name,
             json.dumps(list(entity.aliases)), json.dumps(entity.attributes),
             json.dumps([item.model_dump() for item in entity.provenance]), entity.created_at, entity.updated_at))

    def search_entities(self, owner_id, *, text, entity_type, limit):
        clauses, params = ["owner_id = %s"], [owner_id]
        if entity_type is not None:
            clauses.append("entity_type = %s")
            params.append(entity_type)
        if text:
            clauses.append("(key LIKE %s ESCAPE '\\' OR aliases::text ILIKE %s ESCAPE '\\')")
            escaped = text.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        params.append(limit)
        rows = self.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM jarvis_app.kg_entities WHERE {' AND '.join(clauses)} ORDER BY key, id LIMIT %s",
            tuple(params)).fetchall()
        return tuple(_entity(row) for row in rows)

    def delete_entity(self, owner_id, entity_id):
        removed = self.execute("""DELETE FROM jarvis_app.kg_relationships WHERE owner_id = %s
                                  AND (subject_id = %s OR object_id = %s) RETURNING id""",
                               (owner_id, entity_id, entity_id)).fetchall()
        entity = self.execute("DELETE FROM jarvis_app.kg_entities WHERE owner_id = %s AND id = %s RETURNING id",
                              (owner_id, entity_id)).fetchall()
        return len(entity) + len(removed) if entity else 0

    def get_relationship(self, owner_id, relationship_id):
        row = self.execute(f"SELECT {_REL_COLUMNS} FROM jarvis_app.kg_relationships WHERE owner_id = %s AND id = %s",
                           (owner_id, relationship_id)).fetchone()
        return _relationship(row) if row else None

    def find_relationships(self, owner_id, subject_id, relation):
        rows = self.execute(
            f"""SELECT {_REL_COLUMNS} FROM jarvis_app.kg_relationships
                WHERE owner_id = %s AND subject_id = %s AND relation = %s ORDER BY created_at, id""",
            (owner_id, subject_id, relation)).fetchall()
        return tuple(_relationship(row) for row in rows)

    def save_relationship(self, relationship):
        item = relationship
        self.execute(
            f"""INSERT INTO jarvis_app.kg_relationships ({_REL_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, confidence = EXCLUDED.confidence,
                    provenance = EXCLUDED.provenance, conflicts_with = EXCLUDED.conflicts_with,
                    superseded_by = EXCLUDED.superseded_by, updated_at = EXCLUDED.updated_at
                WHERE jarvis_app.kg_relationships.owner_id = EXCLUDED.owner_id""",
            (item.relationship_id, item.owner_id, item.subject_id, item.relation, item.object_id, item.status,
             item.confidence, json.dumps([entry.model_dump() for entry in item.provenance]),
             json.dumps(list(item.conflicts_with)), item.superseded_by, item.created_at, item.updated_at))

    def edges(self, owner_id, entity_id, *, limit):
        rows = self.execute(
            f"""SELECT {_REL_COLUMNS} FROM jarvis_app.kg_relationships
                WHERE owner_id = %s AND (subject_id = %s OR object_id = %s) ORDER BY created_at, id LIMIT %s""",
            (owner_id, entity_id, entity_id, limit)).fetchall()
        return tuple(_relationship(row) for row in rows)


def _translate(exc: Exception) -> StorageError:
    state = getattr(exc, "sqlstate", None) or getattr(getattr(exc, "diag", None), "sqlstate", None)
    return StorageConflict() if state in _CONFLICT_STATES else StorageError()


class _PostgresTx(_SqlOps):
    def __init__(self, execute, owner_id: UUID):
        super().__init__(execute)
        self._owner = owner_id

    def lock(self, keys: Iterable[str]) -> None:
        for key in sorted(set(keys)):
            self.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id(self._owner, key),))


class _Guarded:
    """Wrap every operation so database errors surface as sanitized storage errors."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            try:
                return attribute(*args, **kwargs)
            except StorageError:
                raise
            except Exception as exc:  # noqa: BLE001 - sanitized
                raise _translate(exc) from exc
        return call


class PostgresGraphStore(_PostgresBase):
    def _single(self, method: str, *args, **kwargs):
        def work(execute):
            return getattr(_SqlOps(execute), method)(*args, **kwargs)
        try:
            return self._run(work)
        except StorageError as exc:
            raise _translate(exc.__cause__) if exc.__cause__ is not None else exc

    @contextmanager
    def atomic(self, owner_id, keys=()):
        """One transaction holding advisory locks for ``keys`` (sorted)."""
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        try:
            manager = self._connect(self._url, connect_timeout=bounded_connect_timeout(
                self._connect_timeout, deadline, clock=monotonic))
            connection = manager.__enter__()
        except Exception as exc:  # noqa: BLE001 - sanitized
            raise StorageError() from exc
        succeeded = False
        try:
            tx = _Guarded(_PostgresTx(
                lambda query, params=(): execute_with_deadline(connection, deadline, query, params, clock=monotonic),
                owner_id))
            tx.lock(keys)
            yield tx
            succeeded = True
        finally:
            info = (None, None, None) if succeeded else sys.exc_info()
            try:
                manager.__exit__(*info)  # commit on success, roll back otherwise
            except Exception as exc:  # noqa: BLE001
                if succeeded:
                    raise _translate(exc) from exc

    def lock(self, keys):  # outside atomic(): nothing to hold
        return None

    def count(self, owner_id):
        return self._single("count", owner_id)

    def find_entity(self, owner_id, entity_type, key):
        return self._single("find_entity", owner_id, entity_type, key)

    def get_entity(self, owner_id, entity_id):
        return self._single("get_entity", owner_id, entity_id)

    def save_entity(self, entity):
        return self._single("save_entity", entity)

    def search_entities(self, owner_id, *, text, entity_type, limit):
        return self._single("search_entities", owner_id, text=text, entity_type=entity_type, limit=limit)

    def delete_entity(self, owner_id, entity_id):
        return self._single("delete_entity", owner_id, entity_id)

    def get_relationship(self, owner_id, relationship_id):
        return self._single("get_relationship", owner_id, relationship_id)

    def find_relationships(self, owner_id, subject_id, relation):
        return self._single("find_relationships", owner_id, subject_id, relation)

    def save_relationship(self, relationship):
        return self._single("save_relationship", relationship)

    def edges(self, owner_id, entity_id, *, limit):
        return self._single("edges", owner_id, entity_id, limit=limit)
