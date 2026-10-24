"""v7.5 life administration: expiry states and lead days, the last-4 rule, recurrence math, payments, upcoming and
overdue views, reminders (idempotent, following changes, never re-firing past moments), finance account checks,
caps, privacy, owner isolation and the API. No network, no payments and no document files anywhere."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from database import runner
from jarvis.assistant.models import ReminderStatus, ReminderUpdateRequest
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.core.config import Settings
from jarvis.core.failure_audit import category_for
from jarvis.finance.models import AccountCreateRequest
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.lifeadmin import service as lifeadmin_module
from jarvis.lifeadmin.models import (
    PASSPORT_GUIDANCE, BillCreateRequest, BillUpdateRequest, DocumentCreateRequest, DocumentUpdateRequest,
    PaidRequest, Payment,
)
from jarvis.lifeadmin.recurrence import add_months, next_due, occurrences
from jarvis.lifeadmin.service import (
    LAST4_TOO_LONG, LifeAdminConflict, LifeAdminInvalid, LifeAdminNotFound, LifeAdminService, LifeAdminUnavailable,
)
from jarvis.lifeadmin.stores import MemoryLifeAdminStore
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOKEN = "v75-lifeadmin-token-with-at-least-32-chars"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
HOLDER = "Zebulon Quartermaine"
LAST4 = "7Q9Z"
FULL_NUMBER = "P1234567Q9Z"
PAYEE = "Acme Water Utility Qx"
AMOUNT = "98765.43"
SECRETS = (HOLDER, LAST4, FULL_NUMBER, PAYEE, AMOUNT)


class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def make_client():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    return app, TestClient(app, headers=AUTH)


def make_service(*, offset=0, assistant=True, finance=None):
    audit = RecordingAudit()
    assistant_service = AssistantService(MemoryAssistantStore(), audit_service=audit) if assistant else None
    service = LifeAdminService(MemoryLifeAdminStore(), audit, assistant_service=assistant_service,
                               finance_service=finance, utc_offset_minutes=offset)
    return service, audit, assistant_service


def doc_request(**extra) -> DocumentCreateRequest:
    return DocumentCreateRequest.model_validate({"kind": "passport", "title": "My passport", **extra})


def bill_request(**extra) -> BillCreateRequest:
    return BillCreateRequest.model_validate({"name": "Water", "currency": "EUR", "recurrence": "monthly",
                                             "next_due_on": (TODAY + timedelta(days=10)).isoformat(), **extra})


def reminders(assistant):
    return assistant.list_reminders(OWNER, limit=200).items


# ------------------------------------------------------------------ documents


def test_default_lead_days_by_kind_and_bounds():
    service, _, _ = make_service()
    leads = {kind: service.create_document(OWNER, doc_request(kind=kind), now=NOW).renewal_lead_days
             for kind in ("passport", "visa", "driving_licence", "insurance", "warranty", "lease", "other")}
    assert leads == {"passport": 270, "visa": 60, "driving_licence": 60, "insurance": 30, "warranty": 30,
                     "lease": 30, "other": 30}
    assert service.create_document(OWNER, doc_request(renewal_lead_days=400), now=NOW).renewal_lead_days == 400
    for bad in (0, 731, 1.5, "30"):
        with pytest.raises(ValidationError):
            doc_request(renewal_lead_days=bad)
    with pytest.raises(ValidationError):
        doc_request(kind="birth_certificate")
    with pytest.raises(ValidationError):
        doc_request(issued_on="2027-01-02", expires_on="2027-01-01")
    with pytest.raises(ValidationError):
        doc_request(holder_name="x" * 101)


def test_expiry_states_days_and_passport_guidance():
    service, _, _ = make_service()

    def state(days, **extra):
        view = service.create_document(OWNER, doc_request(expires_on=(TODAY + timedelta(days=days)).isoformat(),
                                                          **extra), now=NOW)
        return view.state, view.days_until_expiry, view.renew_by

    assert state(300) == ("ok", 300, TODAY + timedelta(days=30))
    assert state(270) == ("renew_soon", 270, TODAY)  # the renewal window opens on renew_by
    assert state(100)[0] == "renew_soon"
    assert state(0) == ("renew_soon", 0, TODAY - timedelta(days=270))  # still valid on its expiry date
    assert state(-1)[0] == "expired"
    assert state(40, kind="insurance") == ("ok", 40, TODAY + timedelta(days=10))
    assert state(30, kind="insurance")[0] == "renew_soon"
    no_expiry = service.create_document(OWNER, doc_request(kind="warranty"), now=NOW)
    assert (no_expiry.state, no_expiry.days_until_expiry, no_expiry.renew_by) == ("ok", None, None)
    passport = service.create_document(OWNER, doc_request(), now=NOW)
    assert passport.guidance == [PASSPORT_GUIDANCE] and "check the destination's rules" in PASSPORT_GUIDANCE
    assert "not a legal rule" in PASSPORT_GUIDANCE
    assert service.create_document(OWNER, doc_request(kind="visa"), now=NOW).guidance == []


def test_local_today_uses_the_configured_offset():
    late_evening_utc = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
    ist, _, _ = make_service(offset=330)  # 01:30 on 25 October in India
    utc, _, _ = make_service()
    body = doc_request(expires_on="2026-10-24")
    assert ist.create_document(OWNER, body, now=late_evening_utc).state == "expired"
    assert utc.create_document(OWNER, body, now=late_evening_utc).state == "renew_soon"


def test_kind_change_follows_the_default_lead_unless_customised():
    service, _, _ = make_service()
    default = service.create_document(OWNER, doc_request(), now=NOW)
    assert service.update_document(OWNER, default.id, DocumentUpdateRequest(kind="visa"), now=NOW).renewal_lead_days == 60
    custom = service.create_document(OWNER, doc_request(renewal_lead_days=100, expires_on="2029-01-01"), now=NOW)
    assert service.update_document(OWNER, custom.id, DocumentUpdateRequest(kind="visa"), now=NOW).renewal_lead_days == 100
    with pytest.raises(ValidationError):
        DocumentUpdateRequest(title=None)
    with pytest.raises(LifeAdminInvalid, match="expires_on must be on or after issued_on"):
        service.update_document(OWNER, custom.id, DocumentUpdateRequest(issued_on="2030-01-01"), now=NOW)


def test_a_full_identifier_is_refused_without_echo_and_not_stored():
    service, audit, _ = make_service()
    for value in (FULL_NUMBER, "12345", "1234 5678 9012"):
        with pytest.raises(LifeAdminInvalid) as caught:
            service.create_document(OWNER, doc_request(identifier_last4=value), now=NOW)
        assert str(caught.value) == LAST4_TOO_LONG and value not in str(caught.value)
        assert "last 4" in str(caught.value)
    with pytest.raises(LifeAdminInvalid, match="only letters and digits") as caught:
        service.create_document(OWNER, doc_request(identifier_last4="12-4"), now=NOW)
    assert "12-4" not in str(caught.value)
    assert service.list_documents(OWNER, now=NOW).total == 0 and audit.records == []
    kept = service.create_document(OWNER, doc_request(identifier_last4=f" {LAST4} "), now=NOW)
    assert kept.identifier_last4 == LAST4
    with pytest.raises(LifeAdminInvalid) as caught:
        service.update_document(OWNER, kept.id, DocumentUpdateRequest(identifier_last4=FULL_NUMBER), now=NOW)
    assert FULL_NUMBER not in str(caught.value)
    assert service.get_document(OWNER, kept.id, now=NOW).identifier_last4 == LAST4
    cleared = service.update_document(OWNER, kept.id, DocumentUpdateRequest(identifier_last4=None), now=NOW)
    assert cleared.identifier_last4 is None


# ------------------------------------------------------------------ recurrence


def test_month_end_anchoring_and_leap_years():
    chain, day = [], date(2027, 1, 31)
    for _ in range(5):
        chain.append(day)
        day = next_due(day, "monthly", interval_days=None, anchor_day=31)
    assert chain == [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30), date(2027, 5, 31)]
    assert next_due(date(2028, 1, 31), "monthly", interval_days=None, anchor_day=31) == date(2028, 2, 29)
    assert next_due(date(2028, 2, 29), "monthly", interval_days=None, anchor_day=31) == date(2028, 3, 31)
    assert next_due(date(2027, 12, 15), "monthly", interval_days=None, anchor_day=15) == date(2028, 1, 15)
    yearly = occurrences(date(2028, 2, 29), "yearly", interval_days=None, anchor_day=29, end_on=None,
                         until=date(2033, 1, 1), limit=10)
    assert yearly == [date(2028, 2, 29), date(2029, 2, 28), date(2030, 2, 28), date(2031, 2, 28), date(2032, 2, 29)]
    quarterly = occurrences(date(2026, 11, 30), "quarterly", interval_days=None, anchor_day=30, end_on=None,
                            until=date(2027, 12, 1), limit=10)
    assert quarterly == [date(2026, 11, 30), date(2027, 2, 28), date(2027, 5, 30), date(2027, 8, 30),
                         date(2027, 11, 30)]
    assert add_months(date(2027, 1, 31), 1, 30) == date(2027, 2, 28)


def test_weekly_every_n_days_one_time_and_end_dates():
    assert next_due(date(2026, 12, 29), "weekly", interval_days=None, anchor_day=29) == date(2027, 1, 5)
    assert next_due(date(2028, 2, 25), "every_n_days", interval_days=10, anchor_day=25) == date(2028, 3, 6)
    assert next_due(date(2026, 1, 1), "every_n_days", interval_days=3650, anchor_day=1) == date(2035, 12, 30)
    assert next_due(date(2026, 1, 1), "one_time", interval_days=None, anchor_day=1) is None
    assert next_due(date(2027, 1, 31), "monthly", interval_days=None, anchor_day=31, end_on=date(2027, 2, 27)) is None
    assert next_due(date(2027, 1, 31), "monthly", interval_days=None, anchor_day=31,
                    end_on=date(2027, 2, 28)) == date(2027, 2, 28)
    assert next_due(date(2200, 12, 1), "monthly", interval_days=None, anchor_day=1) is None  # past the supported range
    assert occurrences(date(2027, 1, 1), "weekly", interval_days=None, anchor_day=1, end_on=date(2027, 1, 20),
                       until=date(2027, 3, 1), limit=10) == [date(2027, 1, 1), date(2027, 1, 8), date(2027, 1, 15)]
    with pytest.raises(ValidationError, match="interval_days"):
        bill_request(recurrence="every_n_days")
    with pytest.raises(ValidationError, match="interval_days"):
        bill_request(recurrence="weekly", interval_days=5)
    for bad in (0, 3651):
        with pytest.raises(ValidationError):
            bill_request(recurrence="every_n_days", interval_days=bad)
    with pytest.raises(ValidationError, match="end_on"):
        bill_request(end_on=TODAY.isoformat())
    with pytest.raises(ValidationError):
        bill_request(currency="XYZ")
    with pytest.raises(ValidationError):
        bill_request(remind_days_before=31)
    with pytest.raises(ValidationError):
        bill_request(amount="-1")


# ------------------------------------------------------------------ bills and payments


def test_mark_paid_advances_with_month_end_anchoring_and_records_history():
    service, _, _ = make_service()
    bill = service.create_bill(OWNER, bill_request(next_due_on="2027-01-31", amount="40.5", payee=PAYEE), now=NOW)
    assert bill.anchor_day == 31 and bill.days_until_due == (date(2027, 1, 31) - TODAY).days and not bill.overdue
    first = service.mark_paid(OWNER, bill.id, now=NOW)
    assert first.bill.next_due_on == date(2027, 2, 28)
    assert first.payment.amount == Decimal("40.5") and first.payment.due_on == date(2027, 1, 31)
    assert first.payment.paid_on == TODAY and first.payment.currency == "EUR" and first.payments_pruned == 0
    assert "did not pay" in first.note and first.reminder == "none"
    second = service.mark_paid(OWNER, bill.id, PaidRequest(amount="42", note="higher this month",
                                                           paid_on=TODAY - timedelta(days=1)), now=NOW)
    assert second.bill.next_due_on == date(2027, 3, 31)
    payments = service.list_payments(OWNER, bill.id)
    assert payments.total == 2 and [p.due_on for p in payments.items] == [date(2027, 1, 31), date(2027, 2, 28)][::-1] \
        or [p.amount for p in payments.items] == [Decimal("40.5"), Decimal("42")]
    assert {p.amount for p in payments.items} == {Decimal("40.5"), Decimal("42")}
    with pytest.raises(LifeAdminInvalid, match="future"):
        service.mark_paid(OWNER, bill.id, PaidRequest(paid_on=TODAY + timedelta(days=1)), now=NOW)
    # editing the due date re-anchors
    moved = service.update_bill(OWNER, bill.id, BillUpdateRequest(next_due_on="2027-04-15"), now=NOW)
    assert moved.anchor_day == 15
    assert service.mark_paid(OWNER, bill.id, now=NOW).bill.next_due_on == date(2027, 5, 15)


def test_one_time_variable_end_dated_and_archived_bills():
    service, _, _ = make_service()
    once = service.create_bill(OWNER, bill_request(recurrence="one_time"), now=NOW)
    paid = service.mark_paid(OWNER, once.id, now=NOW)
    assert paid.bill.next_due_on is None and paid.bill.days_until_due is None and paid.payment.amount is None
    with pytest.raises(LifeAdminConflict, match="Nothing is due"):
        service.mark_paid(OWNER, once.id, now=NOW)
    ending = service.create_bill(OWNER, bill_request(recurrence="weekly", next_due_on="2027-01-01",
                                                     end_on="2027-01-08"), now=NOW)
    assert service.mark_paid(OWNER, ending.id, now=NOW).bill.next_due_on == date(2027, 1, 8)
    assert service.mark_paid(OWNER, ending.id, now=NOW).bill.next_due_on is None
    archived = service.create_bill(OWNER, bill_request(), now=NOW)
    service.update_bill(OWNER, archived.id, BillUpdateRequest(status="archived"), now=NOW)
    with pytest.raises(LifeAdminConflict, match="archived"):
        service.mark_paid(OWNER, archived.id, now=NOW)
    changed = service.update_bill(OWNER, archived.id, BillUpdateRequest(recurrence="every_n_days", interval_days=9),
                                  now=NOW)
    assert changed.interval_days == 9
    assert service.update_bill(OWNER, archived.id, BillUpdateRequest(recurrence="weekly"), now=NOW).interval_days is None
    with pytest.raises(LifeAdminInvalid, match="interval_days"):
        service.update_bill(OWNER, archived.id, BillUpdateRequest(recurrence="every_n_days"), now=NOW)
    with pytest.raises(LifeAdminInvalid, match="end_on"):
        service.update_bill(OWNER, archived.id, BillUpdateRequest(end_on=TODAY.isoformat()), now=NOW)


def test_a_stale_payment_does_not_advance_twice(monkeypatch):
    service, _, _ = make_service()
    bill = service.create_bill(OWNER, bill_request(), now=NOW)
    stored = service.store.get_bill(OWNER, bill.id)
    payment = Payment(id=uuid4(), owner_id=OWNER, bill_id=bill.id, paid_on=TODAY, due_on=stored.next_due_on,
                      currency="EUR", created_at=NOW)
    assert service.store.record_payment(stored, payment, expected_due=TODAY, max_payments=10) == "stale"
    service.mark_paid(OWNER, bill.id, now=NOW)
    # a second request that read the bill before the first one was recorded is refused, not applied twice
    monkeypatch.setattr(service.store, "get_bill", lambda owner_id, bill_id: stored)
    with pytest.raises(LifeAdminConflict, match="changed in the meantime"):
        service.mark_paid(OWNER, bill.id, now=NOW)
    monkeypatch.undo()
    assert service.list_payments(OWNER, bill.id).total == 1
    assert service.get_bill(OWNER, bill.id, now=NOW).next_due_on == stored.next_due_on.replace(
        month=stored.next_due_on.month % 12 + 1, year=stored.next_due_on.year + (stored.next_due_on.month == 12))


# ------------------------------------------------------------------ upcoming and overdue


def test_upcoming_is_sorted_with_occurrences_and_per_currency_totals():
    service, _, _ = make_service()
    weekly = service.create_bill(OWNER, bill_request(name="Cleaner", recurrence="weekly", amount="10",
                                                     next_due_on=(TODAY + timedelta(days=2)).isoformat()), now=NOW)
    service.create_bill(OWNER, bill_request(name="Rent", currency="USD", amount="100",
                                            next_due_on=(TODAY + timedelta(days=5)).isoformat()), now=NOW)
    service.create_bill(OWNER, bill_request(name="Electricity", next_due_on=(TODAY + timedelta(days=1)).isoformat()),
                        now=NOW)
    service.create_bill(OWNER, bill_request(name="Late one", amount="7",
                                            next_due_on=(TODAY - timedelta(days=3)).isoformat()), now=NOW)
    later = service.create_bill(OWNER, bill_request(name="Far", amount="1",
                                                    next_due_on=(TODAY + timedelta(days=40)).isoformat()), now=NOW)
    archived = service.create_bill(OWNER, bill_request(name="Gone", amount="5",
                                                       next_due_on=(TODAY + timedelta(days=3)).isoformat()), now=NOW)
    service.update_bill(OWNER, archived.id, BillUpdateRequest(status="archived"), now=NOW)
    passport = service.create_document(OWNER, doc_request(expires_on=(TODAY + timedelta(days=12)).isoformat()), now=NOW)
    insurance = service.create_document(OWNER, doc_request(kind="insurance", title="Car insurance",
                                                           expires_on=(TODAY + timedelta(days=40)).isoformat()),
                                        now=NOW)
    service.create_document(OWNER, doc_request(title="Old", status="renewed",
                                               expires_on=(TODAY + timedelta(days=3)).isoformat()), now=NOW)
    result = service.upcoming(OWNER, 14, now=NOW)
    summary = [(item.title, item.event, (item.date - TODAY).days, item.occurrence) for item in result.items]
    assert summary == [("Electricity", "bill_due", 1, 1), ("Cleaner", "bill_due", 2, 1), ("Rent", "bill_due", 5, 1),
                       ("Cleaner", "bill_due", 9, 2), ("Car insurance", "renewal_window_opens", 10, None),
                       ("My passport", "document_expires", 12, None)]
    assert result.totals_by_currency == {"EUR": Decimal("20"), "USD": Decimal("100")}
    assert result.bills_without_amount == 1 and not result.truncated and result.today == TODAY
    assert [view.id for view in result.renew_now] == [passport.id]
    assert weekly.id in {item.item_id for item in result.items} and later.id not in {i.item_id for i in result.items}
    assert insurance.id in {item.item_id for item in result.items}
    with pytest.raises(LifeAdminInvalid):
        service.upcoming(OWNER, 0, now=NOW)
    with pytest.raises(LifeAdminInvalid):
        service.upcoming(OWNER, 367, now=NOW)
    assert len(service.upcoming(OWNER, 366, now=NOW).items) > len(result.items)


def test_overdue_lists_unpaid_past_bills_sorted_with_totals():
    service, _, _ = make_service()
    service.create_bill(OWNER, bill_request(name="B", amount="5", next_due_on=(TODAY - timedelta(days=2)).isoformat()),
                        now=NOW)
    service.create_bill(OWNER, bill_request(name="A", currency="GBP", amount="3",
                                            next_due_on=(TODAY - timedelta(days=9)).isoformat()), now=NOW)
    variable = service.create_bill(OWNER, bill_request(name="C", next_due_on=(TODAY - timedelta(days=2)).isoformat()),
                                   now=NOW)
    service.create_bill(OWNER, bill_request(name="Today", amount="1", next_due_on=TODAY.isoformat()), now=NOW)
    result = service.overdue(OWNER, now=NOW)
    assert [(item.bill.name, item.days_overdue) for item in result.items] == [("A", 9), ("B", 2), ("C", 2)]
    assert result.totals_by_currency == {"EUR": Decimal("5"), "GBP": Decimal("3")} and result.bills_without_amount == 1
    assert all(item.bill.overdue for item in result.items)
    service.mark_paid(OWNER, variable.id, now=NOW)  # next due is next month: no longer overdue
    assert [item.bill.name for item in service.overdue(OWNER, now=NOW).items] == ["A", "B"]


def test_briefing_summary_has_counts_and_no_sensitive_values():
    service, _, _ = make_service()
    service.create_bill(OWNER, bill_request(payee=PAYEE, amount=AMOUNT, next_due_on=(TODAY + timedelta(days=2)).isoformat()),
                        now=NOW)
    service.create_bill(OWNER, bill_request(name="Late", next_due_on=(TODAY - timedelta(days=2)).isoformat()), now=NOW)
    service.create_document(OWNER, doc_request(holder_name=HOLDER, identifier_last4=LAST4,
                                               expires_on=(TODAY + timedelta(days=5)).isoformat()), now=NOW)
    brief = service.briefing_summary(OWNER, now=NOW)
    assert (brief.bills_due, brief.bills_overdue, brief.documents_expiring, brief.documents_renew_now) == (1, 1, 1, 1)
    text = brief.model_dump_json()
    assert not any(secret in text for secret in SECRETS)


# ------------------------------------------------------------------ reminders


def test_reminders_are_idempotent_at_nine_local_and_follow_changes():
    service, _, assistant = make_service(offset=330)
    document = service.create_document(OWNER, doc_request(kind="visa", expires_on="2027-10-01", holder_name=HOLDER,
                                                          identifier_last4=LAST4), now=NOW)
    bill = service.create_bill(OWNER, bill_request(next_due_on="2027-01-10", payee=PAYEE, amount=AMOUNT), now=NOW)
    first = service.sync_reminders(OWNER, now=NOW)
    assert sorted(item.action for item in first.items) == ["created", "created"]
    by_item = {item.item_id: item for item in first.items}
    # 09:00 at UTC+05:30 is 03:30 UTC; visa lead 60 days; bill 3 days before
    assert by_item[document.id].remind_at == datetime(2027, 8, 2, 3, 30, tzinfo=timezone.utc)
    assert by_item[bill.id].remind_at == datetime(2027, 1, 7, 3, 30, tzinfo=timezone.utc)
    assert len(reminders(assistant)) == 2
    titles = " ".join(item.title for item in reminders(assistant))
    assert "Renew visa" in titles and "Bill due 2027-01-10" in titles
    assert not any(secret in titles for secret in SECRETS)
    again = service.sync_reminders(OWNER, now=NOW)
    assert [item.action for item in again.items] == ["unchanged", "unchanged"] and len(reminders(assistant)) == 2
    # moving the bill moves its reminder
    service.update_bill(OWNER, bill.id, BillUpdateRequest(next_due_on="2027-02-10", remind_days_before=0), now=NOW)
    moved = assistant.get_reminder(OWNER, by_item[bill.id].reminder_id)
    assert moved.remind_at == datetime(2027, 2, 10, 3, 30, tzinfo=timezone.utc)
    # renewing the document removes its reminder; deleting the bill removes the other
    service.update_document(OWNER, document.id, DocumentUpdateRequest(status="renewed"), now=NOW)
    assert assistant.get_reminder(OWNER, by_item[document.id].reminder_id) is None
    service.delete_bill(OWNER, bill.id)
    assert reminders(assistant) == [] and service.store.list_links(OWNER) == []
    assert service.sync_reminders(OWNER, now=NOW).items == []  # renewed documents get no reminder


def test_marking_a_linked_bill_paid_recreates_the_reminder_for_the_next_due_date():
    service, _, assistant = make_service()
    bill = service.create_bill(OWNER, bill_request(next_due_on="2027-01-31"), now=NOW)
    old = service.sync_reminders(OWNER, now=NOW).items[0].reminder_id
    result = service.mark_paid(OWNER, bill.id, now=NOW)
    assert result.reminder == "recreated" and assistant.get_reminder(OWNER, old) is None
    (current,) = reminders(assistant)
    assert current.remind_at == datetime(2027, 2, 25, 9, 0, tzinfo=timezone.utc)  # 3 days before Feb 28
    assert current.status == ReminderStatus.PENDING and "2027-02-28" in current.title
    once = service.create_bill(OWNER, bill_request(name="Once", recurrence="one_time"), now=NOW)
    service.sync_reminders(OWNER, now=NOW)
    assert service.mark_paid(OWNER, once.id, now=NOW).reminder == "removed"
    assert len(reminders(assistant)) == 1 and service.store.get_link(OWNER, once.id) is None
    archived = service.update_bill(OWNER, bill.id, BillUpdateRequest(status="archived"), now=NOW)
    assert archived.status == "archived" and reminders(assistant) == []


def test_past_reminder_moments_fire_once_and_are_never_re_armed():
    service, _, assistant = make_service()
    # The renewal moment (30 days before) passed already, but the expiry date is still ahead.
    document = service.create_document(OWNER, doc_request(kind="insurance",
                                                          expires_on=(TODAY + timedelta(days=10)).isoformat()), now=NOW)
    (outcome,) = service.sync_reminders(OWNER, now=NOW).items
    assert outcome.action == "created" and outcome.remind_at == NOW  # due now, delivered on the next tick
    link = service.store.get_link(OWNER, document.id)
    assert link.scheduled_at == NOW and link.remind_at < NOW
    # the scheduler delivers it
    assistant.update_reminder(OWNER, outcome.reminder_id, ReminderUpdateRequest(status=ReminderStatus.TRIGGERED))
    later = NOW + timedelta(hours=5)
    for moment in (later, later + timedelta(days=1)):
        (again,) = service.sync_reminders(OWNER, now=moment).items
        assert again.action == "unchanged"
    assert assistant.get_reminder(OWNER, outcome.reminder_id).status == ReminderStatus.TRIGGERED
    # a lead change for the same date whose moment is also past does not re-arm it
    service.update_document(OWNER, document.id, DocumentUpdateRequest(renewal_lead_days=20), now=later)
    assert assistant.get_reminder(OWNER, outcome.reminder_id).status == ReminderStatus.TRIGGERED
    # the owner deleted it: it is not recreated for the same date
    assistant.delete_reminder(OWNER, outcome.reminder_id)
    (skipped,) = service.sync_reminders(OWNER, now=later).items
    assert skipped.action == "skipped_past" and reminders(assistant) == []
    # a new expiry date is a new reminder (once again), made as the linked document is edited
    service.update_document(OWNER, document.id, DocumentUpdateRequest(expires_on=(TODAY + timedelta(days=5)).isoformat()),
                            now=later)
    assert service.audit_service.records[-1]["details"]["reminder"] == "recreated"
    (fresh,) = reminders(assistant)
    assert fresh.remind_at == later and fresh.status == ReminderStatus.PENDING
    assert [item.action for item in service.sync_reminders(OWNER, now=later).items] == ["unchanged"]


def test_past_dates_create_nothing_and_existing_links_are_kept():
    service, _, assistant = make_service()
    service.create_bill(OWNER, bill_request(next_due_on=(TODAY - timedelta(days=1)).isoformat()), now=NOW)
    service.create_document(OWNER, doc_request(expires_on=(TODAY - timedelta(days=1)).isoformat()), now=NOW)
    service.create_document(OWNER, doc_request(title="No expiry"), now=NOW)
    assert sorted(item.action for item in service.sync_reminders(OWNER, now=NOW).items) == ["skipped_past"] * 2
    assert reminders(assistant) == []
    due_today = service.create_bill(OWNER, bill_request(name="Today", next_due_on=TODAY.isoformat()), now=NOW)
    made = next(item for item in service.sync_reminders(OWNER, now=NOW).items if item.item_id == due_today.id)
    assert made.action == "created" and made.remind_at == NOW
    tomorrow = NOW + timedelta(days=1)
    kept = next(item for item in service.sync_reminders(OWNER, now=tomorrow).items if item.item_id == due_today.id)
    assert kept.action == "kept_past" and len(reminders(assistant)) == 1


def test_a_future_reminder_the_owner_deleted_is_recreated_and_orphans_are_removed():
    service, _, assistant = make_service()
    bill = service.create_bill(OWNER, bill_request(), now=NOW)
    (made,) = service.sync_reminders(OWNER, now=NOW).items
    assistant.delete_reminder(OWNER, made.reminder_id)
    (again,) = service.sync_reminders(OWNER, now=NOW).items
    assert again.action == "recreated" and again.reminder_id != made.reminder_id
    service.store._bills.pop(bill.id)  # simulate an item gone without its link
    (orphan,) = service.sync_reminders(OWNER, now=NOW).items
    assert orphan.action == "removed" and reminders(assistant) == []


def test_reminders_need_the_assistant_service():
    service, _, _ = make_service(assistant=False)
    document = service.create_document(OWNER, doc_request(expires_on="2028-01-01"), now=NOW)
    with pytest.raises(LifeAdminUnavailable, match="assistant"):
        service.sync_reminders(OWNER, now=NOW)
    linked, _, assistant = make_service()
    bill = linked.create_bill(OWNER, bill_request(), now=NOW)
    linked.sync_reminders(OWNER, now=NOW)
    linked.assistant_service = None
    assert linked.update_bill(OWNER, bill.id, BillUpdateRequest(name="New"), now=NOW).name == "New"
    assert linked.audit_service.records[-1]["details"]["reminder"] == "unavailable"
    assert service.update_document(OWNER, document.id, DocumentUpdateRequest(title="x"), now=NOW).title == "x"


# ------------------------------------------------------------------ finance accounts, caps, isolation


def test_finance_account_must_belong_to_the_owner_and_is_only_read():
    finance = FinanceService(MemoryFinanceStore())
    mine = finance.create_account(OWNER, AccountCreateRequest(name="Current", account_type="checking",
                                                              institution="Bank", balance=100.0, currency="EUR"))
    theirs = finance.create_account(OTHER, AccountCreateRequest(name="Theirs", account_type="checking",
                                                                institution="Bank", balance=5.0))
    service, _, _ = make_service(finance=finance)
    bill = service.create_bill(OWNER, bill_request(account_id=str(mine.id), autopay=True, amount="40"), now=NOW)
    assert bill.account_id == mine.id and bill.autopay is True
    service.mark_paid(OWNER, bill.id, now=NOW)
    assert finance.get_account(OWNER, mine.id).balance == 100.0  # nothing was moved
    for account in (theirs.id, uuid4()):
        with pytest.raises(LifeAdminInvalid, match="not one of your finance accounts"):
            service.create_bill(OWNER, bill_request(account_id=str(account)), now=NOW)
        with pytest.raises(LifeAdminInvalid, match="not one of your finance accounts"):
            service.update_bill(OWNER, bill.id, BillUpdateRequest(account_id=account), now=NOW)
    assert service.update_bill(OWNER, bill.id, BillUpdateRequest(account_id=None), now=NOW).account_id is None
    without, _, _ = make_service()
    with pytest.raises(LifeAdminUnavailable, match="cannot be checked"):
        without.create_bill(OWNER, bill_request(account_id=str(mine.id)), now=NOW)

    class Broken:
        def get_account(self, owner_id, account_id):
            raise RuntimeError("password=hunter2 host=db")

    broken, _, _ = make_service(finance=Broken())
    with pytest.raises(LifeAdminUnavailable) as caught:
        broken.create_bill(OWNER, bill_request(account_id=str(mine.id)), now=NOW)
    assert "hunter2" not in str(caught.value)


def test_caps_and_payment_history_keeps_the_newest(monkeypatch):
    assert (lifeadmin_module.MAX_DOCUMENTS_PER_OWNER, lifeadmin_module.MAX_BILLS_PER_OWNER,
            lifeadmin_module.MAX_PAYMENTS_PER_BILL) == (500, 500, 1000)
    monkeypatch.setattr(lifeadmin_module, "MAX_DOCUMENTS_PER_OWNER", 2)
    monkeypatch.setattr(lifeadmin_module, "MAX_BILLS_PER_OWNER", 2)
    monkeypatch.setattr(lifeadmin_module, "MAX_PAYMENTS_PER_BILL", 3)
    service, _, _ = make_service()
    for _ in range(2):
        service.create_document(OWNER, doc_request(), now=NOW)
    with pytest.raises(LifeAdminConflict, match="At most 2 documents"):
        service.create_document(OWNER, doc_request(), now=NOW)
    service.create_document(OTHER, doc_request(), now=NOW)  # per owner
    bill = service.create_bill(OWNER, bill_request(recurrence="weekly", next_due_on="2027-01-01"), now=NOW)
    service.create_bill(OWNER, bill_request(), now=NOW)
    with pytest.raises(LifeAdminConflict, match="At most 2 bills"):
        service.create_bill(OWNER, bill_request(), now=NOW)
    pruned = [service.mark_paid(OWNER, bill.id, PaidRequest(paid_on=TODAY - timedelta(days=10 - i)), now=NOW)
              .payments_pruned for i in range(5)]
    assert pruned == [0, 0, 0, 1, 1]
    history = service.list_payments(OWNER, bill.id)
    assert history.total == 3
    assert [p.due_on for p in history.items] == [date(2027, 1, 29), date(2027, 1, 22), date(2027, 1, 15)]


def test_owner_isolation():
    service, _, _ = make_service()
    document = service.create_document(OWNER, doc_request(identifier_last4=LAST4, expires_on="2028-01-01"), now=NOW)
    bill = service.create_bill(OWNER, bill_request(), now=NOW)
    service.sync_reminders(OWNER, now=NOW)
    for action in (lambda: service.get_document(OTHER, document.id),
                   lambda: service.update_document(OTHER, document.id, DocumentUpdateRequest(title="x")),
                   lambda: service.delete_document(OTHER, document.id),
                   lambda: service.get_bill(OTHER, bill.id),
                   lambda: service.update_bill(OTHER, bill.id, BillUpdateRequest(name="x")),
                   lambda: service.delete_bill(OTHER, bill.id),
                   lambda: service.mark_paid(OTHER, bill.id),
                   lambda: service.list_payments(OTHER, bill.id)):
        with pytest.raises(LifeAdminNotFound):
            action()
    assert service.list_documents(OTHER).total == 0 and service.list_bills(OTHER).total == 0
    assert service.upcoming(OTHER, 366, now=NOW).items == [] and service.overdue(OTHER, now=NOW).items == []
    assert service.sync_reminders(OTHER, now=NOW).items == [] and service.store.list_links(OTHER) == []
    assert len(service.store.list_links(OWNER)) == 2
    assert service.get_document(OWNER, document.id).identifier_last4 == LAST4


# ------------------------------------------------------------------ privacy


def test_sensitive_values_never_reach_audit_logs_reprs_or_errors(caplog):
    caplog.set_level(logging.DEBUG)
    service, audit, assistant = make_service()
    document = service.create_document(OWNER, doc_request(holder_name=HOLDER, identifier_last4=LAST4,
                                                          expires_on="2027-06-01"), now=NOW)
    bill = service.create_bill(OWNER, bill_request(payee=PAYEE, amount=AMOUNT), now=NOW)
    service.update_document(OWNER, document.id, DocumentUpdateRequest(holder_name=HOLDER + " Jr"), now=NOW)
    service.update_bill(OWNER, bill.id, BillUpdateRequest(payee=PAYEE + " Ltd", amount=AMOUNT), now=NOW)
    service.sync_reminders(OWNER, now=NOW)
    paid = service.mark_paid(OWNER, bill.id, PaidRequest(amount=AMOUNT, note="paid by card"), now=NOW)
    errors = []
    for action in (lambda: service.create_document(OWNER, doc_request(identifier_last4=FULL_NUMBER), now=NOW),
                   lambda: service.update_bill(OWNER, bill.id, BillUpdateRequest(end_on="2000-01-01"), now=NOW)):
        with pytest.raises(LifeAdminInvalid) as caught:
            action()
        errors.append(str(caught.value))
    with pytest.raises(ValidationError) as caught:
        BillCreateRequest.model_validate({"name": "x", "currency": "EUR", "recurrence": "monthly",
                                          "next_due_on": "2027-01-01", "payee": PAYEE * 20, "amount": AMOUNT + "1111"})
    errors.append(str(caught.value))
    with pytest.raises(ValidationError) as caught:
        DocumentCreateRequest.model_validate({"kind": "passport", "title": "t", "holder_name": HOLDER * 10})
    errors.append(str(caught.value))
    service.delete_document(OWNER, document.id)
    service.delete_bill(OWNER, bill.id)
    texts = [json.dumps(audit.records, default=str), caplog.text, " ".join(errors), repr(service), repr(service.store),
             repr(paid.payment), repr(document), repr(bill), repr(doc_request(holder_name=HOLDER, identifier_last4=LAST4)),
             repr(bill_request(payee=PAYEE, amount=AMOUNT)), repr(PaidRequest(amount=AMOUNT)),
             " ".join(item.title for item in assistant.list_reminders(OWNER).items)]
    assert any("lifeadmin.bill.paid" in record["event_type"] for record in audit.records)
    assert {record["category"] for record in audit.records} == {"assistant"}
    for text in texts:
        for secret in SECRETS + ("Zebulon",):
            assert secret not in text, (secret, text[:200])


# ------------------------------------------------------------------ API


def test_api_requires_authentication_and_capabilities():
    app, client = make_client()
    with client:
        anonymous = TestClient(app)
        assert anonymous.get("/lifeadmin/documents").status_code == 401
        assert anonymous.post("/lifeadmin/reminders/sync").status_code == 401
        assert anonymous.get("/lifeadmin/upcoming").status_code == 401
        app.state.authorization_policy = DenyPolicy({Capability.LIFEADMIN_WRITE})
        assert client.get("/lifeadmin/bills").status_code == 200
        assert client.post("/lifeadmin/bills", json={"name": "x", "currency": "EUR", "recurrence": "monthly",
                                                     "next_due_on": "2027-01-01"}).status_code == 403
        assert client.post("/lifeadmin/documents", json={"kind": "visa", "title": "v"}).status_code == 403
        assert client.post(f"/lifeadmin/bills/{uuid4()}/paid").status_code == 403
        assert client.post("/lifeadmin/reminders/sync").status_code == 403
        app.state.authorization_policy = DenyPolicy({Capability.LIFEADMIN_READ})
        assert client.get("/lifeadmin/overdue").status_code == 403
        assert client.get(f"/lifeadmin/bills/{uuid4()}/payments").status_code == 403
    assert CapabilityPolicy(OWNER).allows(OWNER, Capability.LIFEADMIN_READ)
    assert CapabilityPolicy(OWNER).allows(OWNER, Capability.LIFEADMIN_WRITE)


def test_api_crud_paid_views_reminders_and_validation(caplog):
    caplog.set_level(logging.DEBUG)
    app, client = make_client()
    today = datetime.now(timezone.utc).date()  # the test app uses offset 0
    with client:
        refused = client.post("/lifeadmin/documents", json={"kind": "passport", "title": "P",
                                                            "identifier_last4": FULL_NUMBER})
        assert refused.status_code == 422 and "last 4" in refused.json()["detail"] and FULL_NUMBER not in refused.text
        assert client.get("/lifeadmin/documents").json()["total"] == 0
        assert client.post("/lifeadmin/documents", json={"kind": "passport", "title": "P", "extra": 1}).status_code == 422
        doc = client.post("/lifeadmin/documents", json={
            "kind": "passport", "title": "Passport", "holder_name": HOLDER, "identifier_last4": LAST4,
            "issuer": "Passport office", "expires_on": (today + timedelta(days=100)).isoformat()})
        assert doc.status_code == 201, doc.text
        body = doc.json()
        assert body["state"] == "renew_soon" and body["renewal_lead_days"] == 270 and body["guidance"]
        assert body["identifier_last4"] == LAST4 and body["holder_name"] == HOLDER
        doc_id = body["id"]
        assert client.patch(f"/lifeadmin/documents/{doc_id}", json={"title": None}).status_code == 422
        patched = client.patch(f"/lifeadmin/documents/{doc_id}", json={"identifier_last4": "12345678"})
        assert patched.status_code == 422 and "12345678" not in patched.text
        assert client.get(f"/lifeadmin/documents?status=active").json()["total"] == 1
        assert client.get(f"/lifeadmin/documents?status=bogus").status_code == 422
        bill = client.post("/lifeadmin/bills", json={
            "name": "Water", "payee": PAYEE, "amount": AMOUNT, "currency": "eur", "recurrence": "monthly",
            "next_due_on": (today + timedelta(days=4)).isoformat()})
        assert bill.status_code == 201, bill.text
        bill_id = bill.json()["id"]
        assert bill.json()["currency"] == "EUR" and bill.json()["amount"] == AMOUNT
        assert client.post("/lifeadmin/bills", json={"name": "x", "currency": "EUR", "recurrence": "monthly",
                                                     "next_due_on": "2027-01-01",
                                                     "account_id": str(uuid4())}).status_code == 422
        upcoming = client.get("/lifeadmin/upcoming?days=7").json()
        assert [item["title"] for item in upcoming["items"]] == ["Water"]
        assert upcoming["totals_by_currency"] == {"EUR": AMOUNT} and len(upcoming["renew_now"]) == 1
        assert client.get("/lifeadmin/upcoming?days=0").status_code == 422
        assert client.get("/lifeadmin/upcoming?days=367").status_code == 422
        assert client.get("/lifeadmin/overdue").json()["items"] == []
        synced = client.post("/lifeadmin/reminders/sync")
        assert synced.status_code == 200 and sorted(i["action"] for i in synced.json()["items"]) == ["created"] * 2
        assert [i["action"] for i in client.post("/lifeadmin/reminders/sync").json()["items"]] == ["unchanged"] * 2
        assert client.get("/assistant/reminders").json()["total"] == 2
        paid = client.post(f"/lifeadmin/bills/{bill_id}/paid", json={"note": "standing order"})
        assert paid.status_code == 200, paid.text
        assert paid.json()["reminder"] == "recreated" and "did not pay" in paid.json()["note"]
        assert client.post(f"/lifeadmin/bills/{bill_id}/paid").status_code == 200  # no body is fine
        assert client.post(f"/lifeadmin/bills/{bill_id}/paid",
                           json={"paid_on": (today + timedelta(days=2)).isoformat()}).status_code == 422
        payments = client.get(f"/lifeadmin/bills/{bill_id}/payments").json()
        assert payments["total"] == 2 and payments["items"][0]["amount"] == AMOUNT
        assert client.get(f"/lifeadmin/bills/{uuid4()}/payments").status_code == 404
        assert client.patch(f"/lifeadmin/bills/{bill_id}", json={"status": "archived"}).json()["status"] == "archived"
        assert client.post(f"/lifeadmin/bills/{bill_id}/paid").status_code == 409
        assert client.get("/assistant/reminders").json()["total"] == 1
        assert client.delete(f"/lifeadmin/bills/{bill_id}").status_code == 204
        assert client.get(f"/lifeadmin/bills/{bill_id}").status_code == 404
        assert client.delete(f"/lifeadmin/documents/{doc_id}").status_code == 204
        assert client.get("/assistant/reminders").json()["total"] == 0
        assert client.delete(f"/lifeadmin/documents/{doc_id}").status_code == 404
        page = app.state.audit_service.page(OWNER, limit=500)
    dumped = page.model_dump_json()
    assert "lifeadmin.document.create" in dumped and "lifeadmin.bill.paid" in dumped and "request_failed" in dumped
    for secret in SECRETS:
        assert secret not in dumped and secret not in caplog.text


def test_status_migration_and_audit_wiring():
    _, client = make_client()
    with client:
        report = client.get("/status").json()
    entry = next(item for item in report["subsystems"] if item["key"] == "lifeadmin")
    assert entry["state"] == "limited" and entry["storage"] == "memory"
    text = " ".join(entry["limitations"])
    assert "Owner-entered" in text and "No payments or bank access" in text and "No document storage" in text
    assert "scheduler runner" in text
    ids = [migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR)]
    assert ids.index("0073_life_admin") > ids.index("0072_travel")
    sql = (runner.MIGRATIONS_DIR / "0073_life_admin.sql").read_text(encoding="utf-8")
    assert "REFERENCES jarvis_app.lifeadmin_bills (id, owner_id) ON DELETE CASCADE" in sql
    assert "REFERENCES jarvis_app.lifeadmin_documents (id, owner_id) ON DELETE CASCADE" in sql
    assert "char_length(identifier_last4) BETWEEN 1 AND 4" in sql and "BETWEEN 1 AND 730" in sql
    assert "BETWEEN 1 AND 3650" in sql and "BETWEEN 0 AND 30" in sql
    assert "ADD CONSTRAINT audit_events_category_allowed" not in sql  # the 'assistant' category is reused
    assert category_for("/lifeadmin/bills") == "assistant"
