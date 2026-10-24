"""v7.5 live PostgreSQL validation: life-admin documents and bills, mark paid, payment cascade, persisted reminder
links, CHECK constraints and persistence across store instances."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def _service(url, *, finance=None):
    from jarvis.assistant.service import AssistantService
    from jarvis.assistant.stores import PostgresAssistantStore
    from jarvis.lifeadmin.service import LifeAdminService
    from jarvis.lifeadmin.stores import PostgresLifeAdminStore

    return LifeAdminService(PostgresLifeAdminStore(url), assistant_service=AssistantService(PostgresAssistantStore(url)),
                            finance_service=finance, utc_offset_minutes=330)


def _document(**extra):
    from jarvis.lifeadmin.models import DocumentCreateRequest

    return DocumentCreateRequest.model_validate({
        "kind": "passport", "title": "Passport", "holder_name": "A. Owner", "issuer": "Passport office",
        "identifier_last4": "4821", "issued_on": "2018-03-01", "expires_on": "2028-02-29", "notes": "drawer", **extra})


def _bill(**extra):
    from jarvis.lifeadmin.models import BillCreateRequest

    return BillCreateRequest.model_validate({
        "name": "Rent", "payee": "Landlord", "amount": "1250.125", "currency": "INR", "recurrence": "monthly",
        "next_due_on": "2027-01-31", "category": "home", **extra})


def test_documents_round_trip_and_persist(isolated_postgres):
    from jarvis.lifeadmin.models import DocumentUpdateRequest
    from jarvis.lifeadmin.service import LifeAdminInvalid, LifeAdminNotFound

    url, owner, other = isolated_postgres.database_url, uuid4(), uuid4()
    service = _service(url)
    created = service.create_document(owner, _document(), now=NOW)
    plain = service.create_document(owner, _document(kind="warranty", title="Laptop", holder_name=None,
                                                     identifier_last4=None, issued_on=None, expires_on=None,
                                                     notes=None), now=NOW)
    fresh = _service(url)  # a new store instance reads what the first one wrote
    stored = fresh.get_document(owner, created.id, now=NOW)
    assert stored == created and stored.identifier_last4 == "4821" and stored.renewal_lead_days == 270
    assert stored.expires_on == date(2028, 2, 29) and stored.state == "ok"
    assert [item.id for item in fresh.list_documents(owner, now=NOW).items] == [created.id, plain.id]  # nulls last
    with pytest.raises(LifeAdminNotFound):
        fresh.get_document(other, created.id)
    with pytest.raises(LifeAdminInvalid):
        fresh.create_document(owner, _document(identifier_last4="X12345678"), now=NOW)
    updated = fresh.update_document(owner, created.id, DocumentUpdateRequest(status="archived", notes=None), now=NOW)
    assert _service(url).get_document(owner, created.id, now=NOW) == updated and updated.notes is None
    assert fresh.list_documents(owner, status="archived").total == 1
    fresh.delete_document(owner, plain.id)
    assert fresh.list_documents(owner).total == 1


def test_bills_mark_paid_history_reminder_links_and_cascade(isolated_postgres):
    import psycopg

    from jarvis.finance.models import AccountCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore
    from jarvis.lifeadmin.models import BillUpdateRequest, PaidRequest
    from jarvis.lifeadmin.service import LifeAdminInvalid

    url, owner = isolated_postgres.database_url, uuid4()
    finance = FinanceService(PostgresFinanceStore(url))
    account = finance.create_account(owner, AccountCreateRequest(name="Savings", account_type="savings",
                                                                 institution="Bank", currency="INR"))
    service = _service(url, finance=finance)
    bill = service.create_bill(owner, _bill(account_id=str(account.id)), now=NOW)
    with pytest.raises(LifeAdminInvalid):
        service.create_bill(owner, _bill(account_id=str(uuid4())), now=NOW)
    document = service.create_document(owner, _document(expires_on="2027-12-31"), now=NOW)
    fresh = _service(url, finance=finance)
    stored = fresh.get_bill(owner, bill.id, now=NOW)
    assert stored == bill and stored.amount == Decimal("1250.125") and stored.anchor_day == 31

    first = fresh.sync_reminders(owner, now=NOW)
    assert sorted(item.action for item in first.items) == ["created", "created"]
    links = {link.item_id: link for link in _service(url).store.list_links(owner)}
    assert set(links) == {bill.id, document.id} and links[bill.id].item_kind == "bill"
    # 09:00 at UTC+05:30 on Jan 28 (3 days before Jan 31) is 03:30 UTC
    assert links[bill.id].remind_at == datetime(2027, 1, 28, 3, 30, tzinfo=timezone.utc)
    assert [item.action for item in _service(url).sync_reminders(owner, now=NOW).items] == ["unchanged"] * 2
    assert fresh.assistant_service.list_reminders(owner).total == 2

    paid = fresh.mark_paid(owner, bill.id, PaidRequest(note="transfer"), now=NOW)
    assert paid.bill.next_due_on == date(2027, 2, 28) and paid.reminder == "recreated"
    paid = fresh.mark_paid(owner, bill.id, PaidRequest(amount="1300"), now=NOW)
    assert paid.bill.next_due_on == date(2027, 3, 31)
    history = _service(url).list_payments(owner, bill.id)
    assert history.total == 2 and {p.amount for p in history.items} == {Decimal("1250.125"), Decimal("1300.000")}
    link = _service(url).store.get_link(owner, bill.id)
    first_bill_reminder = next(item.reminder_id for item in first.items if item.item_id == bill.id)
    assert link.due_on == date(2027, 3, 31) and link.reminder_id != first_bill_reminder
    assert fresh.assistant_service.get_reminder(owner, first_bill_reminder) is None
    assert fresh.assistant_service.list_reminders(owner).total == 2
    moved = fresh.update_bill(owner, bill.id, BillUpdateRequest(next_due_on="2027-04-10"), now=NOW)
    assert moved.anchor_day == 10 and _service(url).store.get_link(owner, bill.id).due_on == date(2027, 4, 10)

    with psycopg.connect(url) as conn:
        counts = lambda: [conn.execute(f"SELECT count(*) FROM jarvis_app.{table} WHERE owner_id = %s",  # noqa: E731
                                       (owner,)).fetchone()[0]
                          for table in ("lifeadmin_bills", "lifeadmin_payments", "lifeadmin_reminder_links")]
        assert counts() == [1, 2, 2]
        conn.commit()
        fresh.delete_bill(owner, bill.id)
        assert counts() == [0, 0, 1]  # payments and the bill's link cascade with the bill
        conn.commit()
        conn.execute("DELETE FROM jarvis_app.lifeadmin_documents WHERE id = %s", (document.id,))
        assert counts()[2] == 0  # the document's link cascades too
        conn.rollback()
    assert fresh.assistant_service.list_reminders(owner).total == 1  # the bill's reminder was deleted first
    assert finance.get_account(owner, account.id).balance == account.balance  # nothing was moved


def test_checks_mirror_validation_and_caps_hold(isolated_postgres, monkeypatch):
    import psycopg

    from jarvis.lifeadmin import service as module
    from jarvis.lifeadmin.service import LifeAdminConflict

    url, owner = isolated_postgres.database_url, uuid4()
    with psycopg.connect(url) as conn:
        for sql, params in (
            ("INSERT INTO jarvis_app.lifeadmin_documents (id, owner_id, kind, title, identifier_last4, "
             "renewal_lead_days, created_at, updated_at) VALUES (%s, %s, 'passport', 't', '12345', 30, now(), now())",
             (uuid4(), owner)),
            ("INSERT INTO jarvis_app.lifeadmin_documents (id, owner_id, kind, title, renewal_lead_days, created_at, "
             "updated_at) VALUES (%s, %s, 'passport', 't', 731, now(), now())", (uuid4(), owner)),
            ("INSERT INTO jarvis_app.lifeadmin_bills (id, owner_id, name, currency, recurrence, interval_days, "
             "anchor_day, created_at, updated_at) VALUES (%s, %s, 'b', 'EUR', 'monthly', 5, 1, now(), now())",
             (uuid4(), owner)),
            ("INSERT INTO jarvis_app.lifeadmin_bills (id, owner_id, name, currency, recurrence, anchor_day, "
             "remind_days_before, created_at, updated_at) VALUES (%s, %s, 'b', 'eur', 'weekly', 1, 3, now(), now())",
             (uuid4(), owner)),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(sql, params)
            conn.rollback()
    monkeypatch.setattr(module, "MAX_BILLS_PER_OWNER", 1)
    monkeypatch.setattr(module, "MAX_PAYMENTS_PER_BILL", 2)
    service = _service(url)
    bill = service.create_bill(owner, _bill(recurrence="weekly", next_due_on="2027-01-01"), now=NOW)
    with pytest.raises(LifeAdminConflict):
        service.create_bill(owner, _bill(), now=NOW)
    pruned = [service.mark_paid(owner, bill.id, now=NOW + timedelta(seconds=i)).payments_pruned for i in range(3)]
    assert pruned == [0, 0, 1]
    assert [p.due_on for p in service.list_payments(owner, bill.id).items] == [date(2027, 1, 15), date(2027, 1, 8)]
