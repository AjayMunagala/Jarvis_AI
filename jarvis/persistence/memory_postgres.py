"""Optional PostgreSQL adapter for explicitly managed long-term memories."""

from typing import Callable
from uuid import UUID, uuid4

from jarvis.memory.base import MemoryPersistenceError
from jarvis.memory.models import MemoryPage, MemoryRecord, MemoryUpdate


_RETURNING = "id, content, category, source_reference, created_at, updated_at"


class PostgresMemoryRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
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

    def _open_connection(self):
        return self._connect(
            self._database_url,
            connect_timeout=self._connect_timeout_seconds,
        )

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(
            id=row[0], content=row[1], category=row[2], source_reference=row[3],
            created_at=row[4], updated_at=row[5],
        )

    def create(
        self, owner_id: UUID, content: str, category: str | None = None, source_reference: str | None = None
    ) -> MemoryRecord:
        try:
            with self._open_connection() as connection:
                row = connection.execute(
                    f"""INSERT INTO jarvis_app.memories
                        (id, owner_id, content, category, source_reference)
                        VALUES (%s, %s, %s, %s, %s) RETURNING {_RETURNING}""",
                    (uuid4(), owner_id, content, category, source_reference),
                ).fetchone()
            return self._record(row)
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def list(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0, category: str | None = None
    ) -> MemoryPage:
        try:
            with self._open_connection() as connection:
                if category is None:
                    total = connection.execute(
                        "SELECT count(*) FROM jarvis_app.memories WHERE owner_id = %s", (owner_id,)
                    ).fetchone()[0]
                    rows = connection.execute(
                        f"""SELECT {_RETURNING} FROM jarvis_app.memories WHERE owner_id = %s
                            ORDER BY created_at DESC, id ASC LIMIT %s OFFSET %s""", (owner_id, limit, offset),
                    ).fetchall()
                else:
                    total = connection.execute(
                        "SELECT count(*) FROM jarvis_app.memories WHERE owner_id = %s AND category = %s",
                        (owner_id, category),
                    ).fetchone()[0]
                    rows = connection.execute(
                        f"""SELECT {_RETURNING} FROM jarvis_app.memories
                            WHERE owner_id = %s AND category = %s
                            ORDER BY created_at DESC, id ASC LIMIT %s OFFSET %s""",
                        (owner_id, category, limit, offset),
                    ).fetchall()
            items = tuple(self._record(row) for row in rows)
            return MemoryPage(
                items=items, total=total, limit=limit, offset=offset,
                has_more=offset + len(items) < total,
            )
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def get(self, owner_id: UUID, memory_id: UUID) -> MemoryRecord | None:
        try:
            with self._open_connection() as connection:
                row = connection.execute(
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
        try:
            with self._open_connection() as connection:
                row = connection.execute(
                    f"""UPDATE jarvis_app.memories SET {', '.join(assignments)}
                        WHERE id = %s AND owner_id = %s RETURNING {_RETURNING}""",
                    tuple(parameters),
                ).fetchone()
            return self._record(row) if row is not None else None
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def delete(self, owner_id: UUID, memory_id: UUID) -> bool:
        try:
            with self._open_connection() as connection:
                row = connection.execute(
                    "DELETE FROM jarvis_app.memories WHERE id = %s AND owner_id = %s RETURNING id",
                    (memory_id, owner_id),
                ).fetchone()
            return row is not None
        except Exception as exc:
            raise MemoryPersistenceError() from exc

    def search(self, owner_id: UUID, query: str, *, limit: int = 10, offset: int = 0) -> MemoryPage:
        try:
            with self._open_connection() as connection:
                total = connection.execute(
                    """SELECT count(*) FROM jarvis_app.memories
                       WHERE owner_id = %s
                         AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)""",
                    (owner_id, query),
                ).fetchone()[0]
                rows = connection.execute(
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
        except Exception as exc:
            raise MemoryPersistenceError() from exc
