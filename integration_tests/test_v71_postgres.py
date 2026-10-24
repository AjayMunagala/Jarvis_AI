"""v7.1 live PostgreSQL validation: bank CSV import, re-import adds nothing, undo, the fingerprint index."""

import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


def _request(account_id, text):
    from jarvis.finance.models import ImportMapping, ImportPreviewRequest

    return ImportPreviewRequest(
        account_id=account_id, filename="live.csv", data_base64=base64.b64encode(text.encode("utf-8")).decode(),
        mapping=ImportMapping(date_column="Date", date_format="YYYY-MM-DD", description_column="Description",
                              amount_column="Amount"))


def _csv(*rows):
    return "Date,Description,Amount\n" + "\n".join(rows) + "\n"


def _day(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


def test_import_reimport_and_undo_in_postgresql(isolated_postgres):
    from jarvis.finance.models import AccountCreateRequest, ImportCommitRequest, TransactionCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore

    owner, other = uuid4(), uuid4()
    service = FinanceService(PostgresFinanceStore(isolated_postgres.database_url))
    account = service.create_account(owner, AccountCreateRequest(
        name="Live import", account_type="checking", institution="Test", balance=1000.0))
    manual = service.record_transaction(owner, TransactionCreateRequest(
        account_id=account.id, amount=-12.5, description="Manual lunch",
        transaction_date=datetime.now(timezone.utc) - timedelta(days=9)))
    data = _csv(f"{_day(5)},Grocery store,-40.10", f"{_day(4)},Coffee,-3.50", f"{_day(4)},Coffee,-3.50",
                f"{_day(3)},Payroll,2500.00", "bad,Broken,1", f"{_day(9)},manual  LUNCH,-12.50")

    preview = service.preview_import(owner, _request(account.id, data))
    assert (preview.counts.valid, preview.counts.errors, preview.counts.duplicates_in_file,
            preview.counts.duplicates_existing) == (3, 1, 1, 1)
    done = service.commit_import(owner, preview.id, ImportCommitRequest(skip_duplicates=False))
    assert done.inserted == 4 and done.skipped_duplicates_existing == 1 and done.skipped_errors == 1
    assert done.balance_change == 2452.9 and done.account_balance == round(1000 - 12.5 + 2452.9, 2)

    restarted = FinanceService(PostgresFinanceStore(isolated_postgres.database_url))
    items = restarted.list_transactions(owner, account_id=account.id, limit=50).items
    assert len(items) == 5 and sum(1 for tx in items if tx.import_batch_id == done.batch_id) == 4
    batches = restarted.list_import_batches(owner)
    assert batches.total == 1 and batches.items[0].rows_inserted == 4 and batches.items[0].rows_skipped == 2
    assert restarted.list_import_batches(other).total == 0

    again = restarted.preview_import(owner, _request(account.id, data))
    assert again.counts.duplicates_existing == 5 and again.counts.valid == 0
    second = restarted.commit_import(owner, again.id, ImportCommitRequest(skip_duplicates=False))
    assert second.inserted == 0 and second.batch_id is None
    assert restarted.list_transactions(owner, account_id=account.id).total == 5
    assert restarted.get_account(owner, account.id).balance == round(1000 - 12.5 + 2452.9, 2)

    assert restarted.undo_import(other, done.batch_id) is None
    undone = restarted.undo_import(owner, done.batch_id)
    assert undone.removed == 4 and undone.balance_change == -2452.9
    assert restarted.undo_import(owner, done.batch_id) is None
    remaining = restarted.list_transactions(owner, account_id=account.id).items
    assert [tx.id for tx in remaining] == [manual.id]
    assert restarted.get_account(owner, account.id).balance == 987.5
    assert restarted.list_import_batches(owner).total == 0


def test_fingerprint_index_is_unique_per_owner_and_account(isolated_postgres):
    import psycopg

    from jarvis.finance.models import AccountCreateRequest, ImportCommitRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore

    owner = uuid4()
    service = FinanceService(PostgresFinanceStore(isolated_postgres.database_url))
    first = service.create_account(owner, AccountCreateRequest(name="A", account_type="checking",
                                                               institution="T", balance=0.0))
    second = service.create_account(owner, AccountCreateRequest(name="B", account_type="checking",
                                                                institution="T", balance=0.0))
    data = _csv(f"{_day(2)},Same row,-1.00")
    for account in (first, second):  # the same row may live in two accounts
        preview = service.preview_import(owner, _request(account.id, data))
        assert service.commit_import(owner, preview.id, ImportCommitRequest()).inserted == 1

    with psycopg.connect(isolated_postgres.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT indexdef FROM pg_indexes WHERE schemaname = 'jarvis_app'
                           AND indexname = 'financial_transactions_owner_account_fingerprint'""")
            definition = cur.fetchone()[0]
            assert "UNIQUE" in definition and "import_fingerprint IS NOT NULL" in definition
            cur.execute("""SELECT id, account_id, owner_id, import_fingerprint, import_batch_id
                           FROM jarvis_app.financial_transactions WHERE account_id = %s""", (first.id,))
            row = cur.fetchone()
            assert len(row[3]) == 64 and row[4] is not None
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis_app.financial_transactions (id, account_id, owner_id, amount, category,
                           description, transaction_date, created_at, import_fingerprint)
                       VALUES (%s, %s, %s, -1.00, 'other', 'copy', now(), now(), %s)""",
                    (uuid4(), row[1], row[2], row[3]))
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis_app.financial_transactions (id, account_id, owner_id, amount, category,
                           description, transaction_date, created_at, import_fingerprint)
                       VALUES (%s, %s, %s, -1.00, 'other', 'bad', now(), now(), 'not-a-fingerprint')""",
                    (uuid4(), row[1], row[2]))
        conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # a batch of another owner cannot be referenced
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis_app.financial_transactions (id, account_id, owner_id, amount, category,
                           description, transaction_date, created_at, import_batch_id)
                       VALUES (%s, %s, %s, -1.00, 'other', 'x', now(), now(), %s)""",
                    (uuid4(), row[1], uuid4(), row[4]))
        conn.rollback()
        with conn.cursor() as cur:  # manual rows (no fingerprint) are never constrained by the index
            for _ in range(2):
                cur.execute(
                    """INSERT INTO jarvis_app.financial_transactions (id, account_id, owner_id, amount, category,
                           description, transaction_date, created_at)
                       VALUES (%s, %s, %s, -1.00, 'other', 'manual', now(), now())""",
                    (uuid4(), row[1], row[2]))
        conn.rollback()


def test_import_api_against_postgresql(isolated_postgres):
    with isolated_postgres.client() as client:
        account = client.post("/finance/accounts", json={"name": "API import", "account_type": "checking",
                                                         "institution": "Bank", "balance": 50.0}).json()
        body = {"account_id": account["id"], "filename": "api.csv",
                "data_base64": base64.b64encode(_csv(f"{_day(1)},Tea,-2.00").encode()).decode(),
                "mapping": {"date_column": "Date", "date_format": "YYYY-MM-DD", "description_column": "Description",
                            "amount_column": "Amount"}}
        preview = client.post("/finance/imports/preview", json=body).json()
        committed = client.post(f"/finance/imports/{preview['id']}/commit", json={}).json()
        assert committed["inserted"] == 1 and committed["account_balance"] == 48.0
        assert any(b["id"] == committed["batch_id"] for b in client.get("/finance/imports").json()["items"])
        assert client.delete(f"/finance/imports/{committed['batch_id']}").json()["removed"] == 1
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 50.0
