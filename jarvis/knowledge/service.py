"""Knowledge intelligence service coordinating ingestion, hybrid search, and context budgeting (v3.3)."""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.knowledge.chunking import (
    chunk_text,
    compute_deterministic_embedding,
    estimate_tokens,
    reciprocal_rank_fusion,
)
from jarvis.knowledge.models import (
    ContextBudgetAllocation,
    ContextBudgetRequest,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentCreateRequest,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from jarvis.knowledge.stores import KnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, audit_service: Any = None) -> None:
        self.store = store
        self.audit_service = audit_service

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="tool",
                    event_type=event_type,
                    decision="allow",
                    status="success",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record knowledge audit event: %s", exc)

    # ------------------------------------------------------------- Ingestion

    def ingest_document(
        self, owner_id: UUID, req: KnowledgeDocumentCreateRequest
    ) -> KnowledgeDocumentDetail:
        now = datetime.now(timezone.utc)
        doc_id = uuid4()
        raw_chunks = chunk_text(req.content, chunk_size=500, chunk_overlap=50)

        chunks: list[KnowledgeChunk] = []
        for idx, text in enumerate(raw_chunks):
            chunks.append(
                KnowledgeChunk(
                    id=uuid4(),
                    document_id=doc_id,
                    owner_id=owner_id,
                    chunk_index=idx,
                    content=text,
                    token_count=estimate_tokens(text),
                    created_at=now,
                )
            )

        doc = KnowledgeDocument(
            id=doc_id,
            owner_id=owner_id,
            title=req.title,
            source_type=req.source_type,
            source_uri=req.source_uri,
            content=req.content,
            metadata=req.metadata,
            created_at=now,
            updated_at=now,
        )

        self.store.create_document(doc, chunks)
        self._audit(
            owner_id,
            "knowledge.document.create",
            f"knowledge:doc:{doc.id}",
            {"title": doc.title, "chunk_count": len(chunks)},
        )
        return KnowledgeDocumentDetail(document=doc, chunks=chunks)

    def get_document(
        self, owner_id: UUID, document_id: UUID
    ) -> KnowledgeDocumentDetail | None:
        doc = self.store.get_document(owner_id, document_id)
        if not doc:
            return None
        chunks = self.store.get_document_chunks(owner_id, document_id)
        return KnowledgeDocumentDetail(document=doc, chunks=chunks)

    def list_documents(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> KnowledgeDocumentPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_documents(owner_id, limit=limit, offset=offset)
        return KnowledgeDocumentPage(items=items, total=total)

    def delete_document(self, owner_id: UUID, document_id: UUID) -> bool:
        deleted = self.store.delete_document(owner_id, document_id)
        if deleted:
            self._audit(
                owner_id,
                "knowledge.document.delete",
                f"knowledge:doc:{document_id}",
                {"deleted": True},
            )
        return deleted

    # ------------------------------------------------------------- Hybrid Search

    def search(self, owner_id: UUID, req: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        lexical_matches = self.store.search_chunks_lexical(
            owner_id, req.query, limit=req.top_k * 2
        )
        query_vec = compute_deterministic_embedding(req.query)
        semantic_matches = self.store.search_chunks_semantic(
            owner_id, query_vec, limit=req.top_k * 2
        )

        scored_results = reciprocal_rank_fusion(
            lexical_matches, semantic_matches, k=60, top_k=req.top_k
        )

        self._audit(
            owner_id,
            "knowledge.search",
            f"knowledge:query:{req.query[:50]}",
            {"query": req.query, "matched": len(scored_results)},
        )

        return KnowledgeSearchResponse(
            query=req.query,
            results=scored_results,
            total_matched=len(scored_results),
        )

    # ------------------------------------------------------------- Context Budgeting

    def allocate_context_budget(
        self, owner_id: UUID, req: ContextBudgetRequest
    ) -> ContextBudgetAllocation:
        prompt_tokens = estimate_tokens(req.prompt)
        system_reserve = 300
        budget_for_chunks = max(0, req.max_context_tokens - prompt_tokens - system_reserve)

        # Retrieve candidates from specified docs or hybrid search using prompt
        candidates: list[KnowledgeChunk] = []
        if req.document_ids:
            for doc_id in req.document_ids:
                candidates.extend(self.store.get_document_chunks(owner_id, doc_id))
        else:
            search_res = self.search(
                owner_id, KnowledgeSearchRequest(query=req.prompt[:200], top_k=10)
            )
            candidates = [r.chunk for r in search_res.results]

        selected: list[KnowledgeChunk] = []
        accumulated_tokens = 0

        for chunk in candidates:
            if accumulated_tokens + chunk.token_count <= budget_for_chunks:
                selected.append(chunk)
                accumulated_tokens += chunk.token_count

        total_used = prompt_tokens + system_reserve + accumulated_tokens
        remaining = max(0, req.max_context_tokens - total_used)

        return ContextBudgetAllocation(
            max_tokens=req.max_context_tokens,
            used_tokens=total_used,
            remaining_tokens=remaining,
            sections={
                "system_reserve": system_reserve,
                "prompt": prompt_tokens,
                "knowledge_context": accumulated_tokens,
            },
            selected_chunks=selected,
        )
