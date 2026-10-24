"""Research storage: in-memory and PostgreSQL (v3.4)."""

from datetime import datetime, timezone
import json
import threading
from time import monotonic
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)
from jarvis.research.models import Citation, ParsedDocument, ResearchSynthesis


class ResearchStorageError(Exception):
    """Sanitized failure for research storage operations."""


class ResearchStore(Protocol):
    durable: bool

    def save_document(self, doc: ParsedDocument) -> None: ...
    def get_document(self, owner_id: UUID, doc_id: UUID) -> ParsedDocument | None: ...
    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ParsedDocument], int]: ...
    def delete_document(self, owner_id: UUID, doc_id: UUID) -> bool: ...
    # v4.6: replace the metadata of a stored document (for example its knowledge index links).
    def update_metadata(self, owner_id: UUID, doc_id: UUID, metadata: dict) -> bool: ...

    def save_synthesis(self, synthesis: ResearchSynthesis) -> None: ...
    def get_synthesis(self, owner_id: UUID, synthesis_id: UUID) -> ResearchSynthesis | None: ...
    def list_syntheses(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ResearchSynthesis], int]: ...


# ===================================================================
# In-Memory Implementation
# ===================================================================

class MemoryResearchStore:
    durable: bool = False

    def __init__(self) -> None:
        self._documents: dict[UUID, ParsedDocument] = {}
        self._syntheses: dict[UUID, ResearchSynthesis] = {}
        self._lock = threading.Lock()

    def save_document(self, doc: ParsedDocument) -> None:
        with self._lock:
            self._documents[doc.id] = doc

    def get_document(self, owner_id: UUID, doc_id: UUID) -> ParsedDocument | None:
        with self._lock:
            doc = self._documents.get(doc_id)
            return doc if doc and doc.owner_id == owner_id else None

    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ParsedDocument], int]:
        with self._lock:
            matched = [d for d in self._documents.values() if d.owner_id == owner_id]
            matched.sort(key=lambda x: x.created_at, reverse=True)
            return matched[offset : offset + limit], len(matched)

    def delete_document(self, owner_id: UUID, doc_id: UUID) -> bool:
        with self._lock:
            doc = self._documents.get(doc_id)
            if not doc or doc.owner_id != owner_id:
                return False
            del self._documents[doc_id]
            return True

    def update_metadata(self, owner_id: UUID, doc_id: UUID, metadata: dict) -> bool:
        with self._lock:
            doc = self._documents.get(doc_id)
            if not doc or doc.owner_id != owner_id:
                return False
            self._documents[doc_id] = doc.model_copy(update={"metadata": dict(metadata)})
            return True

    def save_synthesis(self, synthesis: ResearchSynthesis) -> None:
        with self._lock:
            self._syntheses[synthesis.id] = synthesis

    def get_synthesis(self, owner_id: UUID, synthesis_id: UUID) -> ResearchSynthesis | None:
        with self._lock:
            synth = self._syntheses.get(synthesis_id)
            return synth if synth and synth.owner_id == owner_id else None

    def list_syntheses(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ResearchSynthesis], int]:
        with self._lock:
            matched = [s for s in self._syntheses.values() if s.owner_id == owner_id]
            matched.sort(key=lambda x: x.created_at, reverse=True)
            return matched[offset : offset + limit], len(matched)


# ===================================================================
# PostgreSQL Implementation
# ===================================================================

class PostgresResearchStore:
    durable: bool = True

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
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _open(self, deadline: float):
        timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
        return self._connect(self._database_url, connect_timeout=timeout)

    def save_document(self, doc: ParsedDocument) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            INSERT INTO jarvis_app.parsed_documents
                (id, owner_id, filename, content_type, file_size_bytes, page_count, extracted_text, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (doc.id, doc.owner_id, doc.filename, doc.content_type, doc.file_size_bytes,
                     doc.page_count, doc.extracted_text, json.dumps(doc.metadata), doc.created_at),
                    clock=monotonic,
                )
        except Exception as exc:
            raise ResearchStorageError("Failed to save parsed document in PostgreSQL.") from exc

    def get_document(self, owner_id: UUID, doc_id: UUID) -> ParsedDocument | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, owner_id, filename, content_type, file_size_bytes, page_count, extracted_text, metadata, created_at
            FROM jarvis_app.parsed_documents
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (doc_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return ParsedDocument(
                    id=row[0],
                    owner_id=row[1],
                    filename=row[2],
                    content_type=row[3],
                    file_size_bytes=row[4],
                    page_count=row[5],
                    extracted_text=row[6],
                    metadata=json.loads(row[7]) if isinstance(row[7], str) else row[7],
                    created_at=row[8],
                )
        except Exception as exc:
            raise ResearchStorageError("Failed to get parsed document from PostgreSQL.") from exc

    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ParsedDocument], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        count_sql = "SELECT COUNT(*) FROM jarvis_app.parsed_documents WHERE owner_id = %s"
        data_sql = """
            SELECT id, owner_id, filename, content_type, file_size_bytes, page_count, extracted_text, metadata, created_at
            FROM jarvis_app.parsed_documents
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, (owner_id,), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, (owner_id, limit, offset), clock=monotonic).fetchall()
                items = [
                    ParsedDocument(
                        id=r[0],
                        owner_id=r[1],
                        filename=r[2],
                        content_type=r[3],
                        file_size_bytes=r[4],
                        page_count=r[5],
                        extracted_text=r[6],
                        metadata=json.loads(r[7]) if isinstance(r[7], str) else r[7],
                        created_at=r[8],
                    )
                    for r in rows
                ]
                return items, total
        except Exception as exc:
            raise ResearchStorageError("Failed to list parsed documents from PostgreSQL.") from exc

    def delete_document(self, owner_id: UUID, doc_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.parsed_documents WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                res = execute_with_deadline(conn, deadline, sql, (doc_id, owner_id), clock=monotonic)
                return getattr(res, "rowcount", 1) > 0
        except Exception as exc:
            raise ResearchStorageError("Failed to delete parsed document from PostgreSQL.") from exc

    def update_metadata(self, owner_id: UUID, doc_id: UUID, metadata: dict) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "UPDATE jarvis_app.parsed_documents SET metadata = %s WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                res = execute_with_deadline(conn, deadline, sql, (json.dumps(metadata), doc_id, owner_id), clock=monotonic)
                return getattr(res, "rowcount", 1) > 0
        except Exception as exc:
            raise ResearchStorageError("Failed to update parsed document metadata in PostgreSQL.") from exc

    def save_synthesis(self, synthesis: ResearchSynthesis) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            INSERT INTO jarvis_app.research_syntheses
                (id, owner_id, topic, source_urls, source_document_ids, synthesis, citations, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        citations_data = [c.model_dump(mode="json") for c in synthesis.citations]
        doc_ids_data = [str(did) for did in synthesis.source_document_ids]
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (synthesis.id, synthesis.owner_id, synthesis.topic, json.dumps(synthesis.source_urls),
                     json.dumps(doc_ids_data), synthesis.synthesis, json.dumps(citations_data), synthesis.created_at),
                    clock=monotonic,
                )
        except Exception as exc:
            raise ResearchStorageError("Failed to save research synthesis in PostgreSQL.") from exc

    def get_synthesis(self, owner_id: UUID, synthesis_id: UUID) -> ResearchSynthesis | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, owner_id, topic, source_urls, source_document_ids, synthesis, citations, created_at
            FROM jarvis_app.research_syntheses
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (synthesis_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                source_urls = json.loads(row[3]) if isinstance(row[3], str) else row[3]
                doc_ids = [UUID(d) for d in (json.loads(row[4]) if isinstance(row[4], str) else row[4])]
                raw_cites = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                citations = [Citation.model_validate(c) for c in raw_cites]
                return ResearchSynthesis(
                    id=row[0],
                    owner_id=row[1],
                    topic=row[2],
                    source_urls=source_urls,
                    source_document_ids=doc_ids,
                    synthesis=row[5],
                    citations=citations,
                    created_at=row[7],
                )
        except Exception as exc:
            raise ResearchStorageError("Failed to get research synthesis from PostgreSQL.") from exc

    def list_syntheses(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ResearchSynthesis], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        count_sql = "SELECT COUNT(*) FROM jarvis_app.research_syntheses WHERE owner_id = %s"
        data_sql = """
            SELECT id, owner_id, topic, source_urls, source_document_ids, synthesis, citations, created_at
            FROM jarvis_app.research_syntheses
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, (owner_id,), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, (owner_id, limit, offset), clock=monotonic).fetchall()
                items = []
                for r in rows:
                    source_urls = json.loads(r[3]) if isinstance(r[3], str) else r[3]
                    doc_ids = [UUID(d) for d in (json.loads(r[4]) if isinstance(r[4], str) else r[4])]
                    raw_cites = json.loads(r[6]) if isinstance(r[6], str) else r[6]
                    citations = [Citation.model_validate(c) for c in raw_cites]
                    items.append(
                        ResearchSynthesis(
                            id=r[0],
                            owner_id=r[1],
                            topic=r[2],
                            source_urls=source_urls,
                            source_document_ids=doc_ids,
                            synthesis=r[5],
                            citations=citations,
                            created_at=r[7],
                        )
                    )
                return items, total
        except Exception as exc:
            raise ResearchStorageError("Failed to list research syntheses from PostgreSQL.") from exc
