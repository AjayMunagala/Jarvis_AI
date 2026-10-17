"""Optional PostgreSQL adapter for explicitly managed long-term memories."""

from time import monotonic
from typing import Callable, Sequence
from uuid import UUID, uuid4

from jarvis.memory.base import MemoryDeadlineExceeded, MemoryPersistenceError
from jarvis.memory.models import MemoryPage, MemoryRecord, MemoryUpdate, SourceKind
from jarvis.memory.query import MAX_QUERY_TERMS, SAFE_TERM
from jarvis.persistence.operation_timeout import (
    OperationBudgetExceeded,
    bounded_connect_timeout,
    execute_with_deadline,
    operation_deadline,
)


_RETURNING = "id, content, category, source_reference, created_at, updated_at, source_kind"
_SOURCE_KINDS = ("memory", "document")


class PostgresMemoryRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
        operation_timeout_seconds: int = 5,
        connect: Callable | None = None,
    ) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL memory persistence requires the optional 'persistence' dependency."
                ) from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout_seconds = connect_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds

    def _open_connection(self, deadline: float):
        connect_timeout = bounded_connect_timeout(
            self._connect_timeout_seconds, deadline, clock=monotonic
        )
        return self._connect(
            self._database_url,
            connect_timeout=connect_timeout,
        )

    def _deadline(self, outer_deadline: float | None = None) -> float:
        return operation_deadline(
            self._operation_timeout_seconds,
            outer_deadline=outer_deadline,
            clock=monotonic,
        )

    @staticmethod
    def _execute(connection, deadline: float, query: str, parameters=()):
        return execute_with_deadline(
            connection, deadline, query, parameters, clock=monotonic
        )

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(
            id=row[0], content=row[1], category=row[2], source_reference=row[3],
            created_at=row[4], updated_at=row[5], source_kind=row[6],
        )

    def create(
        self, owner_id: UUID, content: str, category: str | None = None, source_reference: str | None = None
    ) -> MemoryRecord:
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                row = self._execute(
                    connection, deadline,
                    f"""INSERT INTO jarvis_app.memories
                        (id, owner_id, content, category, source_reference, source_kind)
                        VALUES (%s, %s, %s, %s, %s, 'memory') RETURNING {_RETURNING}""",
                    (uuid4(), owner_id, content, category, source_reference),
                ).fetchone()
            return self._record(row)
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def create_document(self, owner_id: UUID, content: str, filename: str) -> tuple[MemoryRecord, bool]:
        """Insert one document record unless the owner already stored identical content."""
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                # Serialize same-owner, same-content imports within this transaction
                # so concurrent duplicate uploads cannot both insert.
                self._execute(
                    connection, deadline,
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s || ':' || md5(%s), 0))",
                    (str(owner_id), content),
                )
                existing = self._execute(
                    connection, deadline,
                    f"""SELECT {_RETURNING} FROM jarvis_app.memories
                        WHERE owner_id = %s AND source_kind = 'document'
                          AND md5(content) = md5(%s) AND content = %s
                        ORDER BY created_at ASC, id ASC LIMIT 1""",
                    (owner_id, content, content),
                ).fetchone()
                if existing is not None:
                    return self._record(existing), False
                row = self._execute(
                    connection, deadline,
                    f"""INSERT INTO jarvis_app.memories
                        (id, owner_id, content, category, source_reference, source_kind)
                        VALUES (%s, %s, %s, NULL, %s, 'document') RETURNING {_RETURNING}""",
                    (uuid4(), owner_id, content, filename),
                ).fetchone()
            return self._record(row), True
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def list(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0, category: str | None = None,
        source_kind: SourceKind | None = None,
    ) -> MemoryPage:
        if source_kind is not None and source_kind not in _SOURCE_KINDS:
            raise MemoryPersistenceError()
        # Fixed predicate fragments only; every value is a bound parameter.
        predicates = ["owner_id = %s"]
        parameters: list[object] = [owner_id]
        if category is not None:
            predicates.append("category = %s")
            parameters.append(category)
        if source_kind is not None:
            predicates.append("source_kind = %s")
            parameters.append(source_kind)
        where = " AND ".join(predicates)
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                total = self._execute(
                    connection, deadline,
                    f"SELECT count(*) FROM jarvis_app.memories WHERE {where}", tuple(parameters),
                ).fetchone()[0]
                rows = self._execute(
                    connection, deadline,
                    f"""SELECT {_RETURNING} FROM jarvis_app.memories WHERE {where}
                        ORDER BY created_at DESC, id ASC LIMIT %s OFFSET %s""",
                    (*parameters, limit, offset),
                ).fetchall()
            items = tuple(self._record(row) for row in rows)
            return MemoryPage(
                items=items, total=total, limit=limit, offset=offset,
                has_more=offset + len(items) < total,
            )
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def get(self, owner_id: UUID, memory_id: UUID) -> MemoryRecord | None:
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                row = self._execute(
                    connection, deadline,
                    f"SELECT {_RETURNING} FROM jarvis_app.memories WHERE id = %s AND owner_id = %s",
                    (memory_id, owner_id),
                ).fetchone()
            return self._record(row) if row is not None else None
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def update(self, owner_id: UUID, memory_id: UUID, changes: MemoryUpdate) -> MemoryRecord | None:
        column_names = {
            "content": "content",
            "category": "category",
            "source_reference": "source_reference",
        }
        supplied = [name for name in column_names if name in changes.model_fields_set]
        if not supplied:
            return None
        assignments = [f"{column_names[name]} = %s" for name in supplied]
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        parameters = [getattr(changes, name) for name in supplied]
        parameters.extend((memory_id, owner_id))
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                row = self._execute(
                    connection, deadline,
                    f"""UPDATE jarvis_app.memories SET {', '.join(assignments)}
                        WHERE id = %s AND owner_id = %s RETURNING {_RETURNING}""",
                    tuple(parameters),
                ).fetchone()
            return self._record(row) if row is not None else None
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def delete(self, owner_id: UUID, memory_id: UUID) -> bool:
        deadline = self._deadline()
        try:
            with self._open_connection(deadline) as connection:
                row = self._execute(
                    connection, deadline,
                    "DELETE FROM jarvis_app.memories WHERE id = %s AND owner_id = %s RETURNING id",
                    (memory_id, owner_id),
                ).fetchone()
            return row is not None
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def search_context(
        self,
        owner_id: UUID,
        terms: Sequence[str],
        *,
        deadline: float | None,
        candidate_limit: int,
        source_kinds: Sequence[SourceKind],
    ) -> tuple[MemoryRecord, ...]:
        """Return owner-scoped candidates matching any term as a lexeme prefix.

        Terms must be letter/digit runs, so the joined tsquery cannot contain
        operators supplied by the user. Final ranking happens in Python.
        """
        terms = tuple(terms)
        kinds = tuple(source_kinds)
        if (
            not terms or len(terms) > MAX_QUERY_TERMS
            or any(not isinstance(term, str) or not SAFE_TERM.fullmatch(term) for term in terms)
            or not kinds or any(kind not in _SOURCE_KINDS for kind in kinds)
            or not 1 <= candidate_limit <= 100
        ):
            raise MemoryPersistenceError()
        tsquery = " | ".join(f"{term}:*" for term in terms)
        request_deadline = deadline
        operation_deadline_value = self._deadline(deadline)
        try:
            with self._open_connection(operation_deadline_value) as connection:
                rows = self._execute(
                    connection, operation_deadline_value,
                    f"""SELECT {_RETURNING} FROM jarvis_app.memories
                        WHERE owner_id = %s AND source_kind = ANY(%s)
                          AND to_tsvector('simple', content) @@ to_tsquery('simple', %s)
                        ORDER BY ts_rank_cd(to_tsvector('simple', content), to_tsquery('simple', %s)) DESC,
                                 updated_at DESC, id ASC
                        LIMIT %s""",
                    (owner_id, list(kinds), tsquery, tsquery, candidate_limit),
                ).fetchall()
            return tuple(self._record(row) for row in rows)
        except MemoryDeadlineExceeded:
            raise
        except OperationBudgetExceeded as exc:
            if request_deadline is not None and request_deadline - monotonic() < 1:
                raise MemoryDeadlineExceeded() from None
            raise MemoryPersistenceError() from exc
        except Exception as exc:
            if request_deadline is not None and request_deadline - monotonic() <= 0:
                raise MemoryDeadlineExceeded() from None
            raise MemoryPersistenceError() from exc

    def search(self, owner_id: UUID, query: str, *, limit: int = 10, offset: int = 0) -> MemoryPage:
        return self._search(owner_id, query, limit=limit, offset=offset, deadline=None)

    def search_with_deadline(
        self, owner_id: UUID, query: str, *, deadline: float, limit: int = 10, offset: int = 0
    ) -> MemoryPage:
        return self._search(owner_id, query, limit=limit, offset=offset, deadline=deadline)

    def _search(
        self, owner_id: UUID, query: str, *, limit: int, offset: int, deadline: float | None
    ) -> MemoryPage:
        request_deadline = deadline
        operation_deadline_value = self._deadline(deadline)
        try:
            with self._open_connection(operation_deadline_value) as connection:
                total = self._execute(
                    connection, operation_deadline_value,
                    """SELECT count(*) FROM jarvis_app.memories
                       WHERE owner_id = %s
                         AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)""",
                    (owner_id, query),
                ).fetchone()[0]
                rows = self._execute(
                    connection, operation_deadline_value,
                    f"""SELECT {_RETURNING} FROM jarvis_app.memories
                        WHERE owner_id = %s
                          AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                        ORDER BY ts_rank_cd(to_tsvector('simple', content),
                                            plainto_tsquery('simple', %s)) DESC,
                                 updated_at DESC, id ASC
                        LIMIT %s OFFSET %s""",
                    (owner_id, query, query, limit, offset),
                ).fetchall()
            items = tuple(self._record(row) for row in rows)
            return MemoryPage(
                items=items, total=total, limit=limit, offset=offset,
                has_more=offset + len(items) < total,
            )
        except MemoryDeadlineExceeded:
            raise
        except OperationBudgetExceeded as exc:
            # A sub-second libpq connect window is not representable. The v1.5
            # shared request budget treats that case as exhausted rather than
            # allowing retrieval to consume a longer rounded-up connection.
            if request_deadline is not None and request_deadline - monotonic() < 1:
                raise MemoryDeadlineExceeded() from None
            raise MemoryPersistenceError() from exc
        except Exception as exc:
            if request_deadline is not None and request_deadline - monotonic() <= 0:
                raise MemoryDeadlineExceeded() from None
            raise MemoryPersistenceError() from exc
