"""Knowledge store implementations: in-memory and PostgreSQL (v3.3)."""

from datetime import datetime, timezone
import json
import threading
from time import monotonic
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.knowledge.chunking import EMBEDDING_MODEL, compute_deterministic_embedding, cosine_similarity

SEMANTIC_SCAN_LIMIT = 5000  # most recent chunks scanned per semantic search (v4.6; was an unordered 500)
EMBEDDING_BACKFILL_LIMIT = 200  # stale vectors replaced per search
from jarvis.knowledge.models import KnowledgeChunk, KnowledgeDocument, SourceType
from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)


class KnowledgeStorageError(Exception):
    """Sanitized failure for knowledge storage operations."""


class KnowledgeStore(Protocol):
    durable: bool

    def create_document(
        self, document: KnowledgeDocument, chunks: list[KnowledgeChunk]
    ) -> None: ...

    def get_document(self, owner_id: UUID, document_id: UUID) -> KnowledgeDocument | None: ...

    def get_document_chunks(self, owner_id: UUID, document_id: UUID) -> list[KnowledgeChunk]: ...

    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[KnowledgeDocument], int]: ...

    def delete_document(self, owner_id: UUID, document_id: UUID) -> bool: ...

    def search_chunks_lexical(
        self, owner_id: UUID, query: str, limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]: ...

    def search_chunks_semantic(
        self, owner_id: UUID, query_vector: list[float], limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]: ...


# ===================================================================
# In-Memory Implementation
# ===================================================================

class MemoryKnowledgeStore:
    durable: bool = False

    def __init__(self) -> None:
        self._documents: dict[UUID, KnowledgeDocument] = {}
        self._chunks: dict[UUID, KnowledgeChunk] = {}
        self._embeddings: dict[UUID, list[float]] = {}
        self._doc_chunks: dict[UUID, list[UUID]] = {}
        self._lock = threading.Lock()

    def create_document(
        self, document: KnowledgeDocument, chunks: list[KnowledgeChunk]
    ) -> None:
        with self._lock:
            self._documents[document.id] = document
            chunk_ids = []
            for chunk in chunks:
                self._chunks[chunk.id] = chunk
                self._embeddings[chunk.id] = compute_deterministic_embedding(chunk.content)
                chunk_ids.append(chunk.id)
            self._doc_chunks[document.id] = chunk_ids

    def get_document(self, owner_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        with self._lock:
            doc = self._documents.get(document_id)
            return doc if doc and doc.owner_id == owner_id else None

    def get_document_chunks(self, owner_id: UUID, document_id: UUID) -> list[KnowledgeChunk]:
        with self._lock:
            cids = self._doc_chunks.get(document_id, [])
            # v4.1: owner-scoped like every other read; another owner's document yields nothing.
            return [self._chunks[cid] for cid in cids if cid in self._chunks and self._chunks[cid].owner_id == owner_id]

    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[KnowledgeDocument], int]:
        with self._lock:
            matched = [d for d in self._documents.values() if d.owner_id == owner_id]
            matched.sort(key=lambda x: x.created_at, reverse=True)
            return matched[offset : offset + limit], len(matched)

    def delete_document(self, owner_id: UUID, document_id: UUID) -> bool:
        with self._lock:
            doc = self._documents.get(document_id)
            if not doc or doc.owner_id != owner_id:
                return False
            del self._documents[document_id]
            cids = self._doc_chunks.pop(document_id, [])
            for cid in cids:
                self._chunks.pop(cid, None)
                self._embeddings.pop(cid, None)
            return True

    def search_chunks_lexical(
        self, owner_id: UUID, query: str, limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]:
        terms = [t.lower() for t in query.split() if t.strip()]
        results: list[tuple[int, KnowledgeChunk, str]] = []

        with self._lock:
            for chunk in self._chunks.values():
                if chunk.owner_id != owner_id:
                    continue
                doc = self._documents.get(chunk.document_id)
                if not doc:
                    continue
                content_lower = chunk.content.lower()
                title_lower = doc.title.lower()
                match_count = sum(1 for term in terms if term in content_lower or term in title_lower)
                if match_count > 0:
                    results.append((match_count, chunk, doc.title))

        results.sort(key=lambda x: x[0], reverse=True)
        return [(item[1], item[2]) for item in results[:limit]]

    def search_chunks_semantic(
        self, owner_id: UUID, query_vector: list[float], limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]:
        scored: list[tuple[float, KnowledgeChunk, str]] = []

        with self._lock:
            for chunk_id, vec in self._embeddings.items():
                chunk = self._chunks.get(chunk_id)
                if not chunk or chunk.owner_id != owner_id:
                    continue
                doc = self._documents.get(chunk.document_id)
                if not doc:
                    continue
                sim = cosine_similarity(query_vector, vec)
                if sim > 0.05:
                    scored.append((sim, chunk, doc.title))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(item[1], item[2]) for item in scored[:limit]]


# ===================================================================
# PostgreSQL Implementation
# ===================================================================

class PostgresKnowledgeStore:
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

    def create_document(
        self, document: KnowledgeDocument, chunks: list[KnowledgeChunk]
    ) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        doc_sql = """
            INSERT INTO jarvis_app.knowledge_documents
                (id, owner_id, title, source_type, source_uri, content, metadata, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        chunk_sql = """
            INSERT INTO jarvis_app.knowledge_chunks
                (id, document_id, owner_id, chunk_index, content, token_count, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        emb_sql = """
            INSERT INTO jarvis_app.knowledge_embeddings
                (chunk_id, owner_id, model_name, embedding, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, doc_sql,
                    (document.id, document.owner_id, document.title, document.source_type.value,
                     document.source_uri, document.content, json.dumps(document.metadata),
                     document.created_at, document.updated_at),
                    clock=monotonic,
                )
                for chunk in chunks:
                    execute_with_deadline(
                        conn, deadline, chunk_sql,
                        (chunk.id, chunk.document_id, chunk.owner_id, chunk.chunk_index,
                         chunk.content, chunk.token_count, chunk.created_at),
                        clock=monotonic,
                    )
                    emb = compute_deterministic_embedding(chunk.content)
                    execute_with_deadline(
                        conn, deadline, emb_sql,
                        (chunk.id, chunk.owner_id, EMBEDDING_MODEL, json.dumps(emb), chunk.created_at),
                        clock=monotonic,
                    )
        except Exception as exc:
            raise KnowledgeStorageError("Failed to store knowledge document in PostgreSQL.") from exc

    def get_document(self, owner_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, owner_id, title, source_type, source_uri, content, metadata, created_at, updated_at
            FROM jarvis_app.knowledge_documents
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (document_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return KnowledgeDocument(
                    id=row[0],
                    owner_id=row[1],
                    title=row[2],
                    source_type=SourceType(row[3]),
                    source_uri=row[4],
                    content=row[5],
                    metadata=json.loads(row[6]) if isinstance(row[6], str) else row[6],
                    created_at=row[7],
                    updated_at=row[8],
                )
        except Exception as exc:
            raise KnowledgeStorageError("Failed to get knowledge document from PostgreSQL.") from exc

    def get_document_chunks(self, owner_id: UUID, document_id: UUID) -> list[KnowledgeChunk]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, document_id, owner_id, chunk_index, content, token_count, created_at
            FROM jarvis_app.knowledge_chunks
            WHERE document_id = %s AND owner_id = %s
            ORDER BY chunk_index ASC
        """
        try:
            with self._open(deadline) as conn:
                rows = execute_with_deadline(conn, deadline, sql, (document_id, owner_id), clock=monotonic).fetchall()
                return [
                    KnowledgeChunk(
                        id=r[0],
                        document_id=r[1],
                        owner_id=r[2],
                        chunk_index=r[3],
                        content=r[4],
                        token_count=r[5],
                        created_at=r[6],
                    )
                    for r in rows
                ]
        except Exception as exc:
            raise KnowledgeStorageError("Failed to get knowledge chunks from PostgreSQL.") from exc

    def list_documents(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[KnowledgeDocument], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        count_sql = "SELECT COUNT(*) FROM jarvis_app.knowledge_documents WHERE owner_id = %s"
        data_sql = """
            SELECT id, owner_id, title, source_type, source_uri, content, metadata, created_at, updated_at
            FROM jarvis_app.knowledge_documents
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, (owner_id,), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, (owner_id, limit, offset), clock=monotonic).fetchall()
                items = [
                    KnowledgeDocument(
                        id=r[0],
                        owner_id=r[1],
                        title=r[2],
                        source_type=SourceType(r[3]),
                        source_uri=r[4],
                        content=r[5],
                        metadata=json.loads(r[6]) if isinstance(r[6], str) else r[6],
                        created_at=r[7],
                        updated_at=r[8],
                    )
                    for r in rows
                ]
                return items, total
        except Exception as exc:
            raise KnowledgeStorageError("Failed to list knowledge documents from PostgreSQL.") from exc

    def delete_document(self, owner_id: UUID, document_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.knowledge_documents WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                res = execute_with_deadline(conn, deadline, sql, (document_id, owner_id), clock=monotonic)
                return getattr(res, "rowcount", 1) > 0
        except Exception as exc:
            raise KnowledgeStorageError("Failed to delete knowledge document from PostgreSQL.") from exc

    def search_chunks_lexical(
        self, owner_id: UUID, query: str, limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT c.id, c.document_id, c.owner_id, c.chunk_index, c.content, c.token_count, c.created_at, d.title
            FROM jarvis_app.knowledge_chunks c
            JOIN jarvis_app.knowledge_documents d ON c.document_id = d.id
            WHERE c.owner_id = %s AND (
                to_tsvector('english', c.content) @@ plainto_tsquery('english', %s)
                OR c.content ILIKE %s
            )
            LIMIT %s
        """
        pattern = f"%{query}%"
        try:
            with self._open(deadline) as conn:
                rows = execute_with_deadline(conn, deadline, sql, (owner_id, query, pattern, limit), clock=monotonic).fetchall()
                return [
                    (
                        KnowledgeChunk(
                            id=r[0],
                            document_id=r[1],
                            owner_id=r[2],
                            chunk_index=r[3],
                            content=r[4],
                            token_count=r[5],
                            created_at=r[6],
                        ),
                        r[7],
                    )
                    for r in rows
                ]
        except Exception as exc:
            raise KnowledgeStorageError("Failed to perform lexical search in PostgreSQL.") from exc

    def search_chunks_semantic(
        self, owner_id: UUID, query_vector: list[float], limit: int = 20
    ) -> list[tuple[KnowledgeChunk, str]]:
        # Fetch embeddings and compute cosine similarity in application memory for maximum portable compatibility
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        # v4.6: the most recent chunks, ordered; a vector stored under another model (for example the old
        # process-specific hash) is recomputed from the chunk text and replaced.
        sql = """
            SELECT c.id, c.document_id, c.owner_id, c.chunk_index, c.content, c.token_count, c.created_at, d.title,
                   e.embedding, e.model_name
            FROM jarvis_app.knowledge_chunks c
            JOIN jarvis_app.knowledge_documents d ON c.document_id = d.id AND d.owner_id = c.owner_id
            LEFT JOIN jarvis_app.knowledge_embeddings e ON c.id = e.chunk_id
            WHERE c.owner_id = %s
            ORDER BY c.created_at DESC, c.id
            LIMIT %s
        """
        upsert_sql = """
            INSERT INTO jarvis_app.knowledge_embeddings (chunk_id, owner_id, model_name, embedding, created_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (chunk_id) DO UPDATE SET model_name = EXCLUDED.model_name, embedding = EXCLUDED.embedding
        """
        try:
            with self._open(deadline) as conn:
                rows = execute_with_deadline(conn, deadline, sql, (owner_id, SEMANTIC_SCAN_LIMIT),
                                             clock=monotonic).fetchall()
                scored: list[tuple[float, KnowledgeChunk, str]] = []
                refreshed = 0
                for r in rows:
                    if r[9] == EMBEDDING_MODEL and r[8] is not None:
                        vec = json.loads(r[8]) if isinstance(r[8], str) else r[8]
                    else:
                        vec = compute_deterministic_embedding(r[4])
                        if refreshed < EMBEDDING_BACKFILL_LIMIT:
                            execute_with_deadline(conn, deadline, upsert_sql,
                                                  (r[0], owner_id, EMBEDDING_MODEL, json.dumps(vec)), clock=monotonic)
                            refreshed += 1
                    sim = cosine_similarity(query_vector, vec)
                    if sim > 0.05:
                        chunk = KnowledgeChunk(
                            id=r[0],
                            document_id=r[1],
                            owner_id=r[2],
                            chunk_index=r[3],
                            content=r[4],
                            token_count=r[5],
                            created_at=r[6],
                        )
                        scored.append((sim, chunk, r[7]))
                scored.sort(key=lambda x: x[0], reverse=True)
                return [(item[1], item[2]) for item in scored[:limit]]
        except Exception as exc:
            raise KnowledgeStorageError("Failed to perform semantic search in PostgreSQL.") from exc
