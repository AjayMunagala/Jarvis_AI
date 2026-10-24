"""v4.1 live PostgreSQL validation of the v3.1-v3.7 stores (migrations 0007-0013).

Before v4.1 these migrations could not be installed (they referenced a
jarvis_app.users table that no migration creates), so the PostgreSQL stores of
the assistant, workflow, knowledge, research, scheduler, health and finance
services had never run against a real database. Each test writes through the
authenticated API with persistence enabled, then reads back through a NEW
application instance (a fresh process-level stand-in for a restart).
Opt-in like the rest of this suite (JARVIS_RUN_POSTGRES_INTEGRATION=1).
"""

from uuid import UUID, uuid4

OTHER_OWNER = UUID("b5bff5ad-042a-4132-913a-4da0cfe24710")


def created(response):
    assert response.status_code == 201, response.text
    return response.json()


def test_assistant_tasks_and_notes_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        task = created(client.post("/assistant/tasks", json={"title": "Live v4.1 task", "priority": "high"}))
        note = created(client.post("/assistant/notes", json={"title": "Live note", "content": "Stored in PostgreSQL"}))
    with isolated_postgres.client() as restarted:
        assert restarted.get(f"/assistant/tasks/{task['id']}").json()["title"] == "Live v4.1 task"
        assert restarted.get(f"/assistant/notes/{note['id']}").status_code == 200


def test_workflow_definitions_runs_and_steps_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        definition = created(client.post("/workflows/definitions", json={"name": "Live DAG", "dag_spec": {"steps": [
            {"step_id": "a", "name": "A", "step_type": "noop"},
            {"step_id": "b", "name": "B", "step_type": "transform", "depends_on": ["a"]}]}}))
        run = created(client.post(f"/workflows/definitions/{definition['id']}/run", json={"inputs": {}}))
        assert run["run"]["status"] == "completed"
    with isolated_postgres.client() as restarted:
        detail = restarted.get(f"/workflows/runs/{run['run']['id']}").json()
        assert [step["status"] for step in detail["steps"]] == ["completed", "completed"]


def test_knowledge_search_persists_and_chunks_are_owner_scoped(isolated_postgres):
    from jarvis.knowledge.stores import PostgresKnowledgeStore

    with isolated_postgres.client() as client:
        doc = created(client.post("/knowledge/documents", json={
            "title": "Live knowledge", "content": "Superposition and entanglement in live PostgreSQL storage."}))
        doc_id = doc["document"]["id"]
        results = client.post("/knowledge/search", json={"query": "entanglement", "top_k": 3}).json()["results"]
        assert results and results[0]["document_title"] == "Live knowledge"
    with isolated_postgres.client() as restarted:
        assert restarted.get(f"/knowledge/documents/{doc_id}").json()["chunks"]
    store = PostgresKnowledgeStore(isolated_postgres.database_url)
    assert store.get_document_chunks(OTHER_OWNER, UUID(doc_id)) == []  # cross-owner read refused in SQL


def test_research_documents_and_syntheses_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        doc = created(client.post("/research/documents/parse", json={
            "filename": "live.txt", "content_type": "text/plain", "text_content": "Live research text"}))
        synthesis = created(client.post("/research/synthesize", json={
            "topic": "Live topic", "document_ids": [doc["id"]], "web_urls": ["https://example.com/spec"]}))
        quotes = [item["quote"] for item in synthesis["citations"]]
        assert any(quote.startswith("Not fetched") for quote in quotes)
    with isolated_postgres.client() as restarted:
        assert restarted.get(f"/research/documents/{doc['id']}").status_code == 200
        assert restarted.get(f"/research/syntheses/{synthesis['id']}").status_code == 200


def test_scheduler_jobs_and_notifications_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        job = created(client.post("/scheduler/jobs", json={
            "name": "Live reminder", "schedule_type": "interval", "schedule_expr": "3600", "action_type": "reminder",
            "payload": {"message": "live"}}))
        assert client.post(f"/scheduler/jobs/{job['id']}/run").status_code in {200, 201}
    with isolated_postgres.client() as restarted:
        assert restarted.get(f"/scheduler/jobs/{job['id']}").json()["name"] == "Live reminder"
        assert restarted.get("/scheduler/notifications").status_code == 200


def test_health_snapshots_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        snapshot = created(client.post("/health/diagnostic/snapshots"))
    with isolated_postgres.client() as restarted:
        ids = [item["id"] for item in restarted.get("/health/diagnostic/snapshots").json()["items"]]
        assert snapshot["id"] in ids


def test_finance_accounts_transactions_and_snapshots_persist(isolated_postgres):
    with isolated_postgres.client() as client:
        account = created(client.post("/finance/accounts", json={
            "name": "Live checking", "account_type": "checking", "institution": "Test", "balance": 100.0}))
        created(client.post("/finance/transactions", json={
            "account_id": account["id"], "amount": -20.5, "description": "Live groceries"}))
        created(client.post("/finance/analytics/snapshot"))
    with isolated_postgres.client() as restarted:
        stored = restarted.get(f"/finance/accounts/{account['id']}").json()
        assert abs(float(stored["balance"]) - 79.5) < 1e-6
        assert restarted.get("/finance/transactions").json()["total"] >= 1


def test_unknown_ids_are_not_found_not_errors(isolated_postgres):
    with isolated_postgres.client() as client:
        for path in (f"/assistant/tasks/{uuid4()}", f"/workflows/runs/{uuid4()}", f"/knowledge/documents/{uuid4()}",
                     f"/research/documents/{uuid4()}", f"/scheduler/jobs/{uuid4()}", f"/finance/accounts/{uuid4()}"):
            assert client.get(path).status_code == 404, path
