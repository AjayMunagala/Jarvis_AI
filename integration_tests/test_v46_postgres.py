"""v4.6 live PostgreSQL validation: stale embeddings are replaced, research documents are indexed durably."""

import base64
import json
from uuid import uuid4


def test_vectors_from_the_old_process_hash_are_recomputed(isolated_postgres):
    import psycopg

    from jarvis.knowledge.chunking import EMBEDDING_MODEL
    from jarvis.knowledge.models import KnowledgeDocumentCreateRequest, KnowledgeSearchRequest
    from jarvis.knowledge.service import KnowledgeService
    from jarvis.knowledge.stores import PostgresKnowledgeStore

    owner = uuid4()
    service = KnowledgeService(PostgresKnowledgeStore(isolated_postgres.database_url))
    doc = service.ingest_document(owner, KnowledgeDocumentCreateRequest(
        title="Runbook", content="Rotate the signing keys every ninety days."))
    with psycopg.connect(isolated_postgres.database_url) as connection:  # simulate a pre-v4.6 vector
        connection.execute("UPDATE jarvis_app.knowledge_embeddings SET model_name = 'deterministic-hash-64', "
                           "embedding = %s WHERE owner_id = %s", (json.dumps([0.0] * 64), owner))
    results = service.search(owner, KnowledgeSearchRequest(query="signing keys rotation", top_k=3)).results
    assert results and results[0].chunk.document_id == doc.document.id
    with psycopg.connect(isolated_postgres.database_url) as connection:
        models = {row[0] for row in connection.execute(
            "SELECT model_name FROM jarvis_app.knowledge_embeddings WHERE owner_id = %s", (owner,)).fetchall()}
    assert models == {EMBEDDING_MODEL}


def test_research_indexing_is_durable(isolated_postgres):
    with isolated_postgres.client() as client:
        parsed = client.post("/research/documents/parse", json={
            "filename": "notes.txt", "text_content": "The quarterly review is on the third Monday.",
            "index_in_knowledge": True}).json()
    assert parsed["metadata"]["knowledge_index"] == "indexed"
    with isolated_postgres.client() as restarted:
        hits = restarted.post("/knowledge/search", json={"query": "quarterly review", "top_k": 3}).json()["results"]
        assert any(hit["chunk"]["document_id"] == parsed["metadata"]["knowledge_document_id"] for hit in hits)
        assert restarted.delete(f"/research/documents/{parsed['id']}").status_code == 204
        assert restarted.get(f"/knowledge/documents/{parsed['metadata']['knowledge_document_id']}").status_code == 404
