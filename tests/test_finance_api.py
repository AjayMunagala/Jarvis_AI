"""API integration tests for the v3.7 Finance & External Data Intelligence routes.

Tests /finance/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Account registration, listing, retrieval, deletion
- Transaction recording, automatic categorization, listing, deletion
- Net worth snapshot generation and analytics reporting
- 404 error handling
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "finance-test-token-at-least-32-chars"
TEST_OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(TEST_OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def make_app(**overrides):
    settings = Settings(
        _env_file=None,
        environment="test",
        log_level="ERROR",
        local_auth_token=SecretStr(TEST_TOKEN),
        owner_id=TEST_OWNER,
        **overrides,
    )
    return create_app(settings=settings)


@pytest.fixture
def client():
    app = make_app()
    with TestClient(app, headers=AUTH_HEADERS) as c:
        c.app_ref = app
        yield c


# ===================================================================
# 1. Authentication & Capability Enforcement
# ===================================================================

def test_unauthenticated_requests_fail(client):
    no_auth_client = TestClient(client.app_ref)
    res = no_auth_client.get("/finance/accounts")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/finance/accounts",
        json={"name": "Checking", "account_type": "checking", "institution": "Chase", "balance": 100.0},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.FINANCE_WRITE})

    # Read should succeed
    res = client.get("/finance/accounts")
    assert res.status_code == 200

    # Write should fail (403)
    res = client.post(
        "/finance/accounts",
        json={"name": "Checking", "account_type": "checking", "institution": "Chase", "balance": 100.0},
    )
    assert res.status_code == 403


# ===================================================================
# 2. Account & Transaction CRUD
# ===================================================================

def test_account_and_transaction_lifecycle(client):
    # 1. Create checking account
    acc_payload = {
        "name": "Everyday Checking",
        "account_type": "checking",
        "institution": "Fidelity",
        "balance": 5000.0,
    }
    acc_res = client.post("/finance/accounts", json=acc_payload)
    assert acc_res.status_code == 201
    account = acc_res.json()
    acc_id = account["id"]
    assert account["balance"] == 5000.0

    # Get account
    get_res = client.get(f"/finance/accounts/{acc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Everyday Checking"

    # List accounts
    list_res = client.get("/finance/accounts")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1

    # 2. Record Transaction with auto-categorization
    tx_payload = {
        "account_id": acc_id,
        "amount": -120.50,
        "description": "Trader Joe's Supermarket Groceries",
    }
    tx_res = client.post("/finance/transactions", json=tx_payload)
    assert tx_res.status_code == 201
    tx = tx_res.json()
    tx_id = tx["id"]
    assert tx["category"] == "food"
    assert tx["amount"] == -120.50

    # Check updated balance
    acc_updated = client.get(f"/finance/accounts/{acc_id}").json()
    assert acc_updated["balance"] == 4879.50

    # List transactions
    tx_list_res = client.get(f"/finance/transactions?account_id={acc_id}")
    assert tx_list_res.status_code == 200
    assert tx_list_res.json()["total"] == 1

    # 3. Analytics: Snapshot & Report
    snap_res = client.post("/finance/analytics/snapshot")
    assert snap_res.status_code == 201
    snapshot = snap_res.json()
    assert snapshot["net_worth"] == 4879.50
    assert snapshot["monthly_cash_flow"] == -120.50

    report_res = client.get("/finance/analytics/report")
    assert report_res.status_code == 200
    report = report_res.json()
    assert report["accounts_count"] == 1
    assert len(report["insights"]) > 0

    # 4. Delete transaction (reverses balance)
    del_tx = client.delete(f"/finance/transactions/{tx_id}")
    assert del_tx.status_code == 204
    reverted_acc = client.get(f"/finance/accounts/{acc_id}").json()
    assert reverted_acc["balance"] == 5000.0

    # 5. Delete account
    del_acc = client.delete(f"/finance/accounts/{acc_id}")
    assert del_acc.status_code == 204
    assert client.get(f"/finance/accounts/{acc_id}").status_code == 404
