"""Tests for KnowledgeGraphDocumentAdapter.

Validates:
1. Document existence and ownership validation before ingestion.
2. Mapping document-extracted entities and relationships into KnowledgeRecord / RecordEntity.
3. Preservation of document source_ref across all entities and relationships.
4. Strict enforcement of upstream validator (model extractions MUST be 'inferred' with confidence < 1.0).
5. Safe handling of duplicate entity ingestion (resolve/merge, not fail).
6. Atomic rollback on graph ingestion errors (zero partial writes).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from jarvis.knowledge.models import KnowledgeDocument, SourceType
from jarvis.knowledge.stores import MemoryKnowledgeStore
from jarvis.knowledge_graph.adapters import (
    DocumentNotFoundError,
    DocumentOwnershipError,
    KnowledgeGraphDocumentAdapter,
)
from jarvis.knowledge_graph.models import RecordEntity, RecordRelationship
from jarvis.knowledge_graph.service import GraphError, GraphService
from jarvis.knowledge_graph.stores import MemoryGraphStore

OWNER = UUID("11111111-1111-1111-1111-111111111111")
OTHER_OWNER = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def doc_store():
    return MemoryKnowledgeStore()


@pytest.fixture
def graph_store():
    return MemoryGraphStore()


@pytest.fixture
def graph_service(graph_store):
    return GraphService(graph_store)


@pytest.fixture
def adapter(graph_service, doc_store):
    return KnowledgeGraphDocumentAdapter(graph_service, doc_store)


def _create_sample_doc(doc_store, owner_id: UUID, title: str = "Architecture Doc") -> KnowledgeDocument:
    now = datetime.now(timezone.utc)
    doc = KnowledgeDocument(
        id=uuid4(),
        owner_id=owner_id,
        title=title,
        source_type=SourceType.MANUAL,
        source_uri=None,
        content="Service A depends on Database B for persistent storage.",
        metadata={},
        created_at=now,
        updated_at=now,
    )
    doc_store.create_document(doc, [])
    return doc


def test_document_validation_success_and_owner_isolation(adapter, doc_store):
    doc = _create_sample_doc(doc_store, OWNER)

    # Valid owner succeeds
    validated = adapter.validate_document(OWNER, doc.id)
    assert validated is not None
    assert validated.id == doc.id

    # Cross-owner access raises DocumentOwnershipError
    with pytest.raises(DocumentOwnershipError) as exc_info:
        adapter.validate_document(OTHER_OWNER, doc.id)
    assert "belongs to a different owner" in exc_info.value.public_message

    # Non-existent document raises DocumentNotFoundError
    with pytest.raises(DocumentNotFoundError) as exc_info:
        adapter.validate_document(OWNER, uuid4())
    assert "not found" in exc_info.value.public_message


def test_map_document_extractions_preserves_provenance_and_normalizes_model_relations(adapter):
    doc_ref = "doc:12345678-1234-1234-1234-123456789abc"
    raw_entities = [
        {"ref": "service_a", "entity_type": "service", "name": "Payment Service"},
        {"ref": "db_b", "entity_type": "service", "name": "PostgreSQL DB"},
    ]
    # Input has status="verified" and confidence=1.0, but extractor is "model"
    raw_relationships = [
        {
            "subject_ref": "service_a",
            "relation": "depends_on",
            "object_ref": "db_b",
            "status": "verified",
            "confidence": 1.0,
            "excerpt": "Service A depends on Database B",
        }
    ]

    record = adapter.map_document_extractions(
        document_ref=doc_ref,
        entities=raw_entities,
        relationships=raw_relationships,
        extractor="model",
    )

    assert record.source_kind == "document"
    assert record.source_ref == doc_ref
    assert record.extractor == "model"
    assert len(record.entities) == 2
    assert len(record.relationships) == 1

    rel = record.relationships[0]
    # Upstream validator requirement: model-extracted must be inferred with confidence < 1.0
    assert rel.status == "inferred"
    assert rel.confidence < 1.0
    assert rel.excerpt == "Service A depends on Database B"


def test_map_document_extractions_user_extractor_allows_verified(adapter):
    doc_ref = "doc:user-spec"
    raw_entities = [
        {"ref": "svc", "entity_type": "service", "name": "Core Service"},
        {"ref": "db", "entity_type": "service", "name": "Cache DB"},
    ]
    raw_relationships = [
        {
            "subject_ref": "svc",
            "relation": "depends_on",
            "object_ref": "db",
            "status": "verified",
            "confidence": 1.0,
        }
    ]

    record = adapter.map_document_extractions(
        document_ref=doc_ref,
        entities=raw_entities,
        relationships=raw_relationships,
        extractor="user",
    )
    assert record.relationships[0].status == "verified"
    assert record.relationships[0].confidence == 1.0


def test_ingest_document_extraction_end_to_end(adapter, doc_store, graph_service):
    doc = _create_sample_doc(doc_store, OWNER, "Design Spec")
    entities = [
        {"ref": "auth_svc", "entity_type": "service", "name": "Auth Service", "attributes": {"port": 8080}},
        {"ref": "redis_db", "entity_type": "service", "name": "Redis Session Store"},
    ]
    relationships = [
        {
            "subject_ref": "auth_svc",
            "relation": "depends_on",
            "object_ref": "redis_db",
            "status": "inferred",
            "confidence": 0.85,
        }
    ]

    result = adapter.ingest_document_extraction(
        owner_id=OWNER,
        document_id=doc.id,
        entities=entities,
        relationships=relationships,
        extractor="model",
    )

    assert "auth_svc" in result.entity_ids
    assert "redis_db" in result.entity_ids
    assert len(result.relationships) == 1

    rel = result.relationships[0]
    assert rel.relation == "depends_on"
    assert rel.status == "inferred"
    assert rel.provenance[0].source_kind == "document"
    assert rel.provenance[0].source_ref == f"doc:{doc.id}"


def test_duplicate_entity_ingestion_updates_and_merges_provenance(adapter, doc_store, graph_service):
    doc1 = _create_sample_doc(doc_store, OWNER, "Doc 1")
    doc2 = _create_sample_doc(doc_store, OWNER, "Doc 2")

    # First ingestion with doc1
    adapter.ingest_document_extraction(
        owner_id=OWNER,
        document_id=doc1.id,
        entities=[{"ref": "api", "entity_type": "service", "name": "API Gateway", "attributes": {"v": 1}}],
        relationships=[],
        extractor="model",
    )

    entities_after_first = graph_service.search(OWNER, text="API Gateway")
    assert len(entities_after_first) == 1
    first_entity_id = entities_after_first[0].entity_id
    assert entities_after_first[0].attributes == {"v": 1}

    # Second ingestion with doc2 touching the same entity (by entity_type and key/name)
    adapter.ingest_document_extraction(
        owner_id=OWNER,
        document_id=doc2.id,
        entities=[{"ref": "api", "entity_type": "service", "name": "API Gateway", "attributes": {"timeout": 30}}],
        relationships=[],
        extractor="model",
    )

    entities_after_second = graph_service.search(OWNER, text="API Gateway")
    assert len(entities_after_second) == 1
    # ID should be identical (deduplicated / resolved)
    assert entities_after_second[0].entity_id == first_entity_id
    # Attributes should be merged
    assert entities_after_second[0].attributes == {"v": 1, "timeout": 30}
    # Provenance should have both doc references
    provenance_refs = [p.source_ref for p in entities_after_second[0].provenance]
    assert f"doc:{doc1.id}" in provenance_refs
    assert f"doc:{doc2.id}" in provenance_refs


def test_atomic_rollback_on_failed_ingestion(adapter, doc_store, graph_service):
    doc = _create_sample_doc(doc_store, OWNER, "Invalid Relations Doc")

    initial_entity_count, initial_rel_count = graph_service.store.count(OWNER)

    # Ingest entities with an invalid relation (person depends_on service is invalid in schema)
    invalid_entities = [
        {"ref": "alice", "entity_type": "person", "name": "Alice Developer"},
        {"ref": "backend", "entity_type": "service", "name": "Backend Service"},
    ]
    invalid_relationships = [
        {
            "subject_ref": "alice",
            "relation": "depends_on",  # Schema requires subject to be work type, not person
            "object_ref": "backend",
            "status": "inferred",
            "confidence": 0.5,
        }
    ]

    with pytest.raises(GraphError) as exc_info:
        adapter.ingest_document_extraction(
            owner_id=OWNER,
            document_id=doc.id,
            entities=invalid_entities,
            relationships=invalid_relationships,
            extractor="model",
        )
    assert exc_info.value.status_code == 422

    # Verify atomic rollback: no entities or relationships were persisted
    after_entity_count, after_rel_count = graph_service.store.count(OWNER)
    assert after_entity_count == initial_entity_count
    assert after_rel_count == initial_rel_count
