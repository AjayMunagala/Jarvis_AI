"""v4.4 live PostgreSQL validation: atomic finance writes, approved workflow runs, restart recovery."""

from datetime import datetime, timezone
from uuid import uuid4

from test_postgres_integration import TEST_OWNER_ID


def test_finance_transaction_and_balance_are_atomic_in_postgresql(isolated_postgres):
    from jarvis.finance.models import FinancialTransaction
    from jarvis.finance.stores import PostgresFinanceStore

    with isolated_postgres.client() as client:
        account = client.post("/finance/accounts", json={"name": "Cash", "account_type": "checking",
                                                         "institution": "Bank", "balance": 100.0}).json()
        tx = client.post("/finance/transactions", json={"account_id": account["id"], "amount": -40.0,
                                                        "description": "groceries"}).json()
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 60.0
        assert client.delete(f"/finance/transactions/{tx['id']}").status_code == 204
        assert client.delete(f"/finance/transactions/{tx['id']}").status_code == 404
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 100.0  # reversed once
    store = PostgresFinanceStore(isolated_postgres.database_url)
    orphan = FinancialTransaction(id=uuid4(), account_id=uuid4(), owner_id=TEST_OWNER_ID, amount=5.0, category="other",
                                  description="no account", transaction_date=datetime.now(timezone.utc),
                                  created_at=datetime.now(timezone.utc))
    try:
        store.record_transaction(orphan)
    except ValueError:
        pass
    assert store.get_transaction(TEST_OWNER_ID, orphan.id) is None  # nothing half-written


def test_approved_workflow_run_and_restart_recovery(isolated_postgres):
    with isolated_postgres.client() as client:
        definition = client.post("/workflows/definitions", json={"name": "Live write", "dag_spec": {"steps": [
            {"step_id": "t", "name": "Task", "step_type": "assistant", "action": "tasks.create",
             "parameters": {"title": "From an approved workflow"}}]}}).json()
        url = f"/workflows/definitions/{definition['id']}/run"
        approval_id = client.post(url, json={}).json()["approval"]["approval_id"]
        client.post(f"/approvals/{approval_id}/approve", json={})
        ran = client.post(url, json={"approval_id": approval_id})
        assert ran.status_code == 201 and ran.json()["run"]["status"] == "completed"
        assert client.post(url, json={"approval_id": approval_id}).status_code == 409
        service = client.app.state.workflow_service
        from jarvis.workflow.models import WorkflowRun, WorkflowStatus
        now = datetime.now(timezone.utc)
        run_id = uuid4()
        service.store.create_run(WorkflowRun(id=run_id, workflow_id=definition["id"], owner_id=TEST_OWNER_ID,
                                             status=WorkflowStatus.RUNNING, inputs={}, outputs={}, error_message=None,
                                             created_at=now, started_at=now, completed_at=None), [])
    with isolated_postgres.client() as restarted:  # startup marks the orphaned run as interrupted, in the background
        assert restarted.app.state.workflow_service.recovery_done.wait(10)
        detail = restarted.get(f"/workflows/runs/{run_id}").json()
    assert detail["run"]["status"] == "failed" and "Interrupted" in detail["run"]["error_message"]
