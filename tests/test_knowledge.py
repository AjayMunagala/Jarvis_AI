"""Unit tests for the v3.3 Knowledge Intelligence subsystem.

Covers:
- Text chunking with overlap and boundary preservation
- Deterministic pseudo-embeddings and cosine similarity
- Reciprocal Rank Fusion (RRF) hybrid scoring
- MemoryKnowledgeStore lifecycle and search methods
- KnowledgeService ingestion, search, context budgeting, and audit logging
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from jarvis.audit.service import AuditService
from jarvis.audit.stores import MemoryAuditStore
from jarvis.knowledge.chunking import (
    chunk_text,
    compute_deterministic_embedding,
    cosine_similarity,
    estimate_tokens,
    reciprocal_rank_fusion,
)
from jarvis.knowledge.models import (
    ContextBudgetRequest,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentCreateRequest,
    KnowledgeSearchRequest,
    SourceType,
)
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.stores import MemoryKnowledgeStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ===================================================================
# 1. Chunking & Mathematical Algorithms
# ===================================================================

def test_chunk_text_empty_and_short():
    assert chunk_text("") == []
    assert chunk_text("Short text") == ["Short text"]


def test_chunk_text_boundary_and_overlap():
    long_text = "Sentence one. " * 30  # ~420 chars
    chunks = chunk_text(long_text, chunk_size=150, chunk_overlap=30)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 150
        assert len(chunk) > 0


def test_token_estimation():
    # v4.6: one shared, conservative estimator (about 3 characters per token) for every context budget.
    assert estimate_tokens("hello") == 2
    assert estimate_tokens("a" * 100) == 34


def test_deterministic_embedding_and_cosine_similarity():
    v1 = compute_deterministic_embedding("artificial intelligence and machine learning")
    v2 = compute_deterministic_embedding("artificial intelligence and machine learning")
    v3 = compute_deterministic_embedding("completely unrelated topic about cooking recipes")

    # Identical vectors -> similarity ~ 1.0
    sim_identical = cosine_similarity(v1, v2)
    assert abs(sim_identical - 1.0) < 1e-3

    # Different topics -> low similarity
    sim_diff = cosine_similarity(v1, v3)
    assert sim_diff < sim_identical


def test_reciprocal_rank_fusion():
    now = datetime.now(timezone.utc)
    chunk1 = KnowledgeChunk(id=uuid4(), document_id=uuid4(), owner_id=OWNER, chunk_index=0, content="c1", token_count=5, created_at=now)
    chunk2 = KnowledgeChunk(id=uuid4(), document_id=uuid4(), owner_id=OWNER, chunk_index=1, content="c2", token_count=5, created_at=now)

    # chunk1 is rank 1 in lexical, rank 2 in semantic
    # chunk2 is rank 2 in lexical, rank 1 in semantic
    lexical = [(chunk1, "Doc A"), (chunk2, "Doc B")]
    semantic = [(chunk2, "Doc B"), (chunk1, "Doc A")]

    fused = reciprocal_rank_fusion(lexical, semantic, k=60, top_k=2)
    assert len(fused) == 2
    # Both chunks appear in both rankings -> match_type is hybrid_rrf
    assert fused[0].match_type == "hybrid_rrf"
    assert fused[1].match_type == "hybrid_rrf"
    # Scores are symmetric
    assert abs(fused[0].score - fused[1].score) < 1e-4


# ===================================================================
# 2. Store & Service Tests
# ===================================================================

def test_knowledge_service_ingestion_and_search():
    store = MemoryKnowledgeStore()
    audit_store = MemoryAuditStore()
    audit_service = AuditService(audit_store)
    service = KnowledgeService(store, audit_service)

    doc_text = """
    Project JARVIS Architecture:
    The personal AI operating system uses a modular micro-kernel approach.
    Security policies enforce owner isolation and capability allowlisting.
    Storage utilizes dual memory and PostgreSQL adapters.
    """

    req = KnowledgeDocumentCreateRequest(
        title="JARVIS Architecture Overview",
        content=doc_text,
        source_type=SourceType.MANUAL,
    )

    detail = service.ingest_document(OWNER, req)
    assert detail.document.title == "JARVIS Architecture Overview"
    assert len(detail.chunks) > 0
    assert detail.document.owner_id == OWNER

    # Cross-owner isolation
    cross = service.get_document(OTHER, detail.document.id)
    assert cross is None

    # Hybrid Search
    search_res = service.search(OWNER, KnowledgeSearchRequest(query="micro-kernel architecture", top_k=3))
    assert search_res.total_matched > 0
    assert "Architecture" in search_res.results[0].document_title

    # Audit events emitted
    recent = audit_store.recent(OWNER, limit=10)
    assert any(e.event_type == "knowledge.document.create" for e in recent)
    assert any(e.event_type == "knowledge.search" for e in recent)

    # Delete
    assert service.delete_document(OWNER, detail.document.id) is True
    assert service.get_document(OWNER, detail.document.id) is None


def test_context_budget_allocator():
    store = MemoryKnowledgeStore()
    service = KnowledgeService(store)

    doc = KnowledgeDocumentCreateRequest(
        title="Knowledge Base",
        content="Important fact number one. " * 50,  # ~1350 chars (~338 tokens)
    )
    detail = service.ingest_document(OWNER, doc)

    # Request with generous budget
    budget_req = ContextBudgetRequest(
        prompt="Explain fact number one in detail",
        max_context_tokens=1000,
        document_ids=[detail.document.id],
    )
    allocation = service.allocate_context_budget(OWNER, budget_req)
    assert allocation.max_tokens == 1000
    assert allocation.used_tokens <= 1000
    assert allocation.remaining_tokens >= 0
    assert len(allocation.selected_chunks) > 0
    assert allocation.sections["system_reserve"] == 300

    # Request with tight budget
    tight_req = ContextBudgetRequest(
        prompt="Explain fact number one in detail",
        max_context_tokens=500,  # Prompt (~8 tokens) + Reserve (300) leaves ~192 tokens
        document_ids=[detail.document.id],
    )
    tight_alloc = service.allocate_context_budget(OWNER, tight_req)
    assert tight_alloc.used_tokens <= 500
    assert tight_alloc.remaining_tokens >= 0
