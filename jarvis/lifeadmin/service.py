"""Life administration service (v7.5): documents with expiry dates, bills and renewals, reminders.

Scope: owner-entered document metadata (no images or files), bills with recurrence and a payment history,
an upcoming view and an overdue view, and reminders through the assistant service. JARVIS never pays a bill,
moves money, signs in to a provider or bank, or looks anything up: "marking paid" only records what the owner
says happened and advances the next due date. A bill may name one of the owner's finance accounts for context;
the account is checked to belong to the owner and is otherwise only read.

Days are the owner's local days, using the configured ``scheduler_utc_offset_minutes`` (a fixed offset: daylight
saving changes are not followed).

Privacy: ``holder_name`` and ``identifier_last4`` (documents) and ``payee`` and amounts (bills, payments) are
never written to audit records, logs, error messages or reminder titles. Audit records use the existing
``assistant`` category (event types ``lifeadmin.*``) and hold ids, kinds, field names and counts only. A full
identifier number is refused: only the last 4 letters or digits are accepted, and the refusal never echoes it.

Reminders (exact behaviour), one per item, created by ``sync_reminders`` and stored as a link row
(item -> assistant reminder id, due date, planned moment, moment actually scheduled):
* Documents with status ``active`` and an ``expires_on``: planned at 09:00 local on
  ``expires_on - renewal_lead_days``. Bills with status ``active`` and a ``next_due_on``: planned at 09:00 local on
  ``next_due_on - remind_days_before``.
* If the planned moment is in the future, the reminder is set to it. If it has passed but the expiry/due date is
  today or later, the reminder is set to *now* (the scheduler delivers it on its next tick) - once: the link keeps
  the date and planned moment, so later syncs for the same date never re-arm or recreate it, even if the owner
  deleted or dismissed it in the assistant. A future planned moment whose reminder the owner deleted is recreated.
* If the date itself has passed (expired document, overdue bill), nothing new is created and an existing link
  for that same date is kept as it is (``kept_past``); a link made for a different date is removed.
* Editing an item moves its linked reminder (and sets it back to pending) when its date or planned moment changes;
  if both the old and the new planned moment have passed for the same date, nothing is re-armed.
* Archiving, renewing or expiring a document, archiving a bill, clearing the date, or deleting the item removes
  the reminder. Marking a linked bill paid removes the old reminder and creates a new one for the next due date
  (or none when the bill is finished). Items without a link are only picked up by ``sync_reminders``.
* The assistant and life-admin stores are separate, so these steps are not one transaction: a failure in the
  middle can leave an extra reminder (removed best-effort) or a link to a missing reminder (handled by the
  rules above on the next sync).
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import re
import threading
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from jarvis.lifeadmin.models import (
    PASSPORT_GUIDANCE, Bill, BillCreateRequest, BillPage, BillUpdateRequest, BillView, Document,
    DocumentCreateRequest, DocumentPage, DocumentUpdateRequest, DocumentView, LifeAdminBriefing, OverdueItem,
    OverdueResult, PaidRequest, PaidResult, Payment, PaymentPage, ReminderLink, ReminderOutcome, ReminderResult,
    UpcomingItem, UpcomingResult, check_recurrence, default_lead_days,
)
from jarvis.lifeadmin.recurrence import next_due, occurrences
from jarvis.lifeadmin.stores import LifeAdminStore

logger = logging.getLogger(__name__)

AUDIT_CATEGORY = "assistant"
MAX_DOCUMENTS_PER_OWNER = 500
MAX_BILLS_PER_OWNER = 500
MAX_PAYMENTS_PER_BILL = 1000
MAX_UPCOMING_ITEMS = 2000
MAX_OCCURRENCES_PER_BILL = 400
REMINDER_HOUR = time(9, 0)

LAST4_TOO_LONG = ("Only the last 4 characters of an identifier are kept. Send just the last 4 letters or digits; "
                  "the value you sent was not stored.")
LAST4_BAD_CHARACTERS = ("identifier_last4 may contain only letters and digits (the last 4 characters of the "
                        "number); the value you sent was not stored.")
_LAST4 = re.compile(r"^[A-Za-z0-9]{1,4}$")
_KIND_LABELS = {"passport": "passport", "national_id": "national ID", "driving_licence": "driving licence",
                "visa": "visa", "insurance": "insurance", "warranty": "warranty",
                "vehicle_registration": "vehicle registration", "lease": "lease", "subscription": "subscription",
                "other": "document"}
_EVENT_ORDER = {"bill_due": 0, "renewal_window_opens": 1, "document_expires": 2}


class LifeAdminNotFound(LookupError):
    """The document or bill does not exist for this owner."""


class LifeAdminInvalid(ValueError):
    """The request is well-formed but not valid; the message never contains a submitted value."""


class LifeAdminConflict(Exception):
    """A limit was reached, or the item is not in a state that allows the action."""


class LifeAdminUnavailable(Exception):
    """A dependency (assistant service for reminders, finance service for account checks) is not available."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    if not errors:
        return "The request is invalid."
    error = errors[0]
    field = ".".join(str(part) for part in error.get("loc", ()) if part not in ("body",))
    message = str(error.get("msg", "The request is invalid.")).removeprefix("Value error, ")
    return (f"{field}: {message}" if field else message)[:300]


def clean_last4(value: str | None) -> str | None:
    """Return the accepted last-4 value, or raise ``LifeAdminInvalid`` without echoing what was sent."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > 4:
        raise LifeAdminInvalid(LAST4_TOO_LONG)
    if not _LAST4.fullmatch(value):
        raise LifeAdminInvalid(LAST4_BAD_CHARACTERS)
    return value


def _totals(pairs: list[tuple[Decimal | None, str]]) -> tuple[dict[str, Decimal], int]:
    totals: dict[str, Decimal] = {}
    missing = 0
    for amount, currency in pairs:
        if amount is None:
            missing += 1
        else:
            totals[currency] = totals.get(currency, Decimal("0")) + amount
    return dict(sorted(totals.items())), missing


class LifeAdminService:
    def __init__(self, store: LifeAdminStore, audit_service: Any = None, *, assistant_service: Any = None,
                 finance_service: Any = None, utc_offset_minutes: int = 0) -> None:
        self.store = store
        self.audit_service = audit_service
        self.assistant_service = assistant_service
        self.finance_service = finance_service
        self.utc_offset_minutes = utc_offset_minutes
        self._reminder_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"LifeAdminService(store={self.store!r})"

    # ================================================================ helpers

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is None:
            return
        try:
            self.audit_service.record(owner_id=owner_id, category=AUDIT_CATEGORY, event_type=event_type,
                                      decision="allowed", status="succeeded", subject=subject[:200], details=details)
        except Exception as exc:  # noqa: BLE001 - auditing never fails the operation
            logger.warning("Failed to record a life-admin audit event: %s", type(exc).__name__)

    @property
    def _zone(self) -> timezone:
        return timezone(timedelta(minutes=self.utc_offset_minutes))

    def today(self, now: datetime | None = None) -> date:
        return (now or _now()).astimezone(self._zone).date()

    def _moment(self, day: date) -> datetime:
        return datetime.combine(day, REMINDER_HOUR, tzinfo=self._zone).astimezone(timezone.utc)

    # ================================================================ documents

    def document_view(self, document: Document, today: date) -> DocumentView:
        days = renew_by = None
        state = "ok"
        if document.expires_on is not None:
            days = (document.expires_on - today).days
            renew_by = document.expires_on - timedelta(days=document.renewal_lead_days)
            if today > document.expires_on:
                state = "expired"
            elif today >= renew_by:
                state = "renew_soon"
        guidance = [PASSPORT_GUIDANCE] if document.kind == "passport" else []
        return DocumentView(**dict(document), days_until_expiry=days, renew_by=renew_by, state=state,
                            guidance=guidance)

    def _document(self, owner_id: UUID, document_id: UUID) -> Document:
        document = self.store.get_document(owner_id, document_id)
        if document is None:
            raise LifeAdminNotFound("The requested document was not found.")
        return document

    def create_document(self, owner_id: UUID, req: DocumentCreateRequest, *, now: datetime | None = None) -> DocumentView:
        now = now or _now()
        fields = dict(req)
        fields["identifier_last4"] = clean_last4(req.identifier_last4)
        if fields["renewal_lead_days"] is None:
            fields["renewal_lead_days"] = default_lead_days(req.kind)
        try:
            document = Document(**fields, id=uuid4(), owner_id=owner_id, created_at=now, updated_at=now)
        except ValidationError as exc:
            raise LifeAdminInvalid(_first_error(exc)) from exc
        if not self.store.save_document(document, max_documents=MAX_DOCUMENTS_PER_OWNER):
            raise LifeAdminConflict(f"At most {MAX_DOCUMENTS_PER_OWNER} documents can be kept; delete some first.")
        self._audit(owner_id, "lifeadmin.document.create", f"lifeadmin:document:{document.id}",
                    {"kind": document.kind, "has_expiry": document.expires_on is not None})
        return self.document_view(document, self.today(now))

    def get_document(self, owner_id: UUID, document_id: UUID, *, now: datetime | None = None) -> DocumentView:
        return self.document_view(self._document(owner_id, document_id), self.today(now))

    def list_documents(self, owner_id: UUID, *, status: str | None = None, limit: int = 50, offset: int = 0,
                       now: datetime | None = None) -> DocumentPage:
        items, total = self.store.list_documents(owner_id, limit=max(1, min(limit, 200)), offset=max(0, offset),
                                                 status=status)
        today = self.today(now)
        return DocumentPage(items=[self.document_view(item, today) for item in items], total=total)

    def update_document(self, owner_id: UUID, document_id: UUID, req: DocumentUpdateRequest, *,
                        now: datetime | None = None) -> DocumentView:
        now = now or _now()
        document = self._document(owner_id, document_id)
        changes = {name: getattr(req, name) for name in req.model_fields_set}
        if "identifier_last4" in changes:
            changes["identifier_last4"] = clean_last4(changes["identifier_last4"])
        if not changes:
            return self.document_view(document, self.today(now))
        if ("kind" in changes and "renewal_lead_days" not in changes
                and document.renewal_lead_days == default_lead_days(document.kind)):
            changes["renewal_lead_days"] = default_lead_days(changes["kind"])  # follow the new kind's default
        try:
            updated = Document.model_validate({**dict(document), **changes, "updated_at": now})
        except ValidationError as exc:
            raise LifeAdminInvalid(_first_error(exc)) from exc
        with self._reminder_lock:
            if not self.store.update_document(updated):
                raise LifeAdminNotFound("The requested document was not found.")
            reminder = self._follow(owner_id, "document", updated, now)
        self._audit(owner_id, "lifeadmin.document.update", f"lifeadmin:document:{document_id}",
                    {"fields": ",".join(sorted(changes)), "reminder": reminder})
        return self.document_view(updated, self.today(now))

    def delete_document(self, owner_id: UUID, document_id: UUID) -> None:
        self._document(owner_id, document_id)
        with self._reminder_lock:
            removed = self._remove_linked(owner_id, document_id)
            if not self.store.delete_document(owner_id, document_id):
                raise LifeAdminNotFound("The requested document was not found.")
        self._audit(owner_id, "lifeadmin.document.delete", f"lifeadmin:document:{document_id}",
                    {"reminders_removed": removed})

    # ================================================================ bills

    def bill_view(self, bill: Bill, today: date) -> BillView:
        days = (bill.next_due_on - today).days if bill.next_due_on is not None else None
        return BillView(**dict(bill), days_until_due=days,
                        overdue=bill.status == "active" and days is not None and days < 0)

    def _bill(self, owner_id: UUID, bill_id: UUID) -> Bill:
        bill = self.store.get_bill(owner_id, bill_id)
        if bill is None:
            raise LifeAdminNotFound("The requested bill was not found.")
        return bill

    def _check_account(self, owner_id: UUID, account_id: UUID | None) -> None:
        """The account must be one of the owner's finance accounts. Read-only: nothing about it is changed."""
        if account_id is None:
            return
        if self.finance_service is None:
            raise LifeAdminUnavailable("Finance accounts are unavailable, so account_id cannot be checked.")
        try:
            account = self.finance_service.get_account(owner_id, account_id)
        except Exception as exc:  # noqa: BLE001 - sanitized; the finance store may raise driver errors
            logger.warning("Failed to check a finance account for a bill: %s", type(exc).__name__)
            raise LifeAdminUnavailable("Finance accounts could not be checked.") from exc
        if account is None:
            raise LifeAdminInvalid("account_id is not one of your finance accounts.")

    def create_bill(self, owner_id: UUID, req: BillCreateRequest, *, now: datetime | None = None) -> BillView:
        now = now or _now()
        self._check_account(owner_id, req.account_id)
        try:
            bill = Bill(**dict(req), id=uuid4(), owner_id=owner_id, anchor_day=req.next_due_on.day,
                        created_at=now, updated_at=now)
        except ValidationError as exc:
            raise LifeAdminInvalid(_first_error(exc)) from exc
        if not self.store.save_bill(bill, max_bills=MAX_BILLS_PER_OWNER):
            raise LifeAdminConflict(f"At most {MAX_BILLS_PER_OWNER} bills can be kept; delete some first.")
        self._audit(owner_id, "lifeadmin.bill.create", f"lifeadmin:bill:{bill.id}",
                    {"recurrence": bill.recurrence, "autopay": bill.autopay, "has_amount": bill.amount is not None,
                     "has_account": bill.account_id is not None})
        return self.bill_view(bill, self.today(now))

    def get_bill(self, owner_id: UUID, bill_id: UUID, *, now: datetime | None = None) -> BillView:
        return self.bill_view(self._bill(owner_id, bill_id), self.today(now))

    def list_bills(self, owner_id: UUID, *, status: str | None = None, limit: int = 50, offset: int = 0,
                   now: datetime | None = None) -> BillPage:
        items, total = self.store.list_bills(owner_id, limit=max(1, min(limit, 200)), offset=max(0, offset),
                                             status=status)
        today = self.today(now)
        return BillPage(items=[self.bill_view(item, today) for item in items], total=total)

    def update_bill(self, owner_id: UUID, bill_id: UUID, req: BillUpdateRequest, *,
                    now: datetime | None = None) -> BillView:
        now = now or _now()
        bill = self._bill(owner_id, bill_id)
        changes = {name: getattr(req, name) for name in req.model_fields_set}
        if not changes:
            return self.bill_view(bill, self.today(now))
        if changes.get("account_id") is not None:
            self._check_account(owner_id, changes["account_id"])
        if "next_due_on" in changes:
            changes["anchor_day"] = changes["next_due_on"].day
        if "recurrence" in changes and changes["recurrence"] != "every_n_days" and "interval_days" not in changes:
            changes["interval_days"] = None
        merged = {**dict(bill), **changes, "updated_at": now}
        try:
            check_recurrence(merged["recurrence"], merged["interval_days"])
            if merged["end_on"] is not None and merged["next_due_on"] is not None \
                    and merged["end_on"] < merged["next_due_on"]:
                raise ValueError("end_on must be on or after next_due_on.")
            updated = Bill.model_validate(merged)
        except ValidationError as exc:
            raise LifeAdminInvalid(_first_error(exc)) from exc
        except ValueError as exc:
            raise LifeAdminInvalid(str(exc)) from exc
        with self._reminder_lock:
            if not self.store.update_bill(updated):
                raise LifeAdminNotFound("The requested bill was not found.")
            reminder = self._follow(owner_id, "bill", updated, now)
        self._audit(owner_id, "lifeadmin.bill.update", f"lifeadmin:bill:{bill_id}",
                    {"fields": ",".join(sorted(name for name in changes if name != "anchor_day")),
                     "reminder": reminder})
        return self.bill_view(updated, self.today(now))

    def delete_bill(self, owner_id: UUID, bill_id: UUID) -> None:
        self._bill(owner_id, bill_id)
        with self._reminder_lock:
            removed = self._remove_linked(owner_id, bill_id)
            if not self.store.delete_bill(owner_id, bill_id):
                raise LifeAdminNotFound("The requested bill was not found.")
        self._audit(owner_id, "lifeadmin.bill.delete", f"lifeadmin:bill:{bill_id}", {"reminders_removed": removed})

    def mark_paid(self, owner_id: UUID, bill_id: UUID, req: PaidRequest | None = None, *,
                  now: datetime | None = None) -> PaidResult:
        """Record a payment the owner made elsewhere and advance the next due date. Nothing is paid here."""
        req = req or PaidRequest()
        now = now or _now()
        today = self.today(now)
        bill = self._bill(owner_id, bill_id)
        if bill.status != "active":
            raise LifeAdminConflict("This bill is archived; set its status to active before marking it paid.")
        if bill.next_due_on is None:
            raise LifeAdminConflict("Nothing is due on this bill (a one-time bill already paid, or past its end "
                                    "date). Set a new next_due_on to use it again.")
        paid_on = req.paid_on or today
        if paid_on > today:
            raise LifeAdminInvalid("paid_on cannot be in the future (your local today is used).")
        following = next_due(bill.next_due_on, bill.recurrence, interval_days=bill.interval_days,
                             anchor_day=bill.anchor_day, end_on=bill.end_on)
        updated = bill.model_copy(update={"next_due_on": following, "updated_at": now})
        payment = Payment(id=uuid4(), owner_id=owner_id, bill_id=bill.id, paid_on=paid_on, due_on=bill.next_due_on,
                          amount=req.amount if req.amount is not None else bill.amount, currency=bill.currency,
                          note=req.note, created_at=now)
        outcome = self.store.record_payment(updated, payment, expected_due=bill.next_due_on,
                                            max_payments=MAX_PAYMENTS_PER_BILL)
        if outcome == "missing":
            raise LifeAdminNotFound("The requested bill was not found.")
        if outcome == "stale":
            raise LifeAdminConflict("The bill changed in the meantime (perhaps already marked paid); reload it and "
                                    "try again.")
        with self._reminder_lock:
            reminder = self._renew_after_payment(owner_id, updated, now)
        self._audit(owner_id, "lifeadmin.bill.paid", f"lifeadmin:bill:{bill_id}",
                    {"advanced": following is not None, "payments_pruned": int(outcome), "reminder": reminder})
        return PaidResult(bill=self.bill_view(updated, today), payment=payment, payments_pruned=int(outcome),
                          reminder=reminder)

    def list_payments(self, owner_id: UUID, bill_id: UUID, *, limit: int = 50, offset: int = 0) -> PaymentPage:
        self._bill(owner_id, bill_id)
        items, total = self.store.list_payments(owner_id, bill_id, limit=max(1, min(limit, 200)),
                                                offset=max(0, offset))
        return PaymentPage(items=items, total=total)

    # ================================================================ upcoming, overdue, briefing

    def _all_documents(self, owner_id: UUID, status: str | None = None) -> list[Document]:
        return self.store.list_documents(owner_id, limit=MAX_DOCUMENTS_PER_OWNER, offset=0, status=status)[0]

    def _all_bills(self, owner_id: UUID, status: str | None = None) -> list[Bill]:
        return self.store.list_bills(owner_id, limit=MAX_BILLS_PER_OWNER, offset=0, status=status)[0]

    def upcoming(self, owner_id: UUID, days: int = 30, *, now: datetime | None = None) -> UpcomingResult:
        """Everything from local today through today + ``days`` (inclusive), soonest first."""
        if not 1 <= days <= 366:
            raise LifeAdminInvalid("days must be between 1 and 366.")
        today = self.today(now)
        until = today + timedelta(days=days)
        items: list[UpcomingItem] = []
        money: list[tuple[Decimal | None, str]] = []
        for bill in self._all_bills(owner_id, "active"):
            dates = occurrences(bill.next_due_on, bill.recurrence, interval_days=bill.interval_days,
                                anchor_day=bill.anchor_day, end_on=bill.end_on, until=until,
                                limit=MAX_OCCURRENCES_PER_BILL)
            for number, due in enumerate(dates, start=1):
                if due < today:  # overdue: listed by the overdue view
                    continue
                money.append((bill.amount, bill.currency))
                items.append(UpcomingItem(item_kind="bill", item_id=bill.id, title=bill.name, event="bill_due",
                                          date=due, days_from_today=(due - today).days, amount=bill.amount,
                                          currency=bill.currency, autopay=bill.autopay, occurrence=number))
        renew_now: list[DocumentView] = []
        for document in self._all_documents(owner_id, "active"):
            if document.expires_on is None:
                continue
            view = self.document_view(document, today)
            if view.state == "renew_soon":
                renew_now.append(view)
            if view.renew_by is not None and today < view.renew_by <= until:
                items.append(UpcomingItem(item_kind="document", item_id=document.id, title=document.title,
                                          event="renewal_window_opens", date=view.renew_by,
                                          days_from_today=(view.renew_by - today).days))
            if today <= document.expires_on <= until:
                items.append(UpcomingItem(item_kind="document", item_id=document.id, title=document.title,
                                          event="document_expires", date=document.expires_on,
                                          days_from_today=(document.expires_on - today).days))
        items.sort(key=lambda item: (item.date, _EVENT_ORDER[item.event], item.title.casefold(), str(item.item_id),
                                     item.occurrence or 0))
        totals, missing = _totals(money)
        renew_now.sort(key=lambda view: (view.expires_on, view.title.casefold(), str(view.id)))
        return UpcomingResult(today=today, days=days, items=items[:MAX_UPCOMING_ITEMS],
                              truncated=len(items) > MAX_UPCOMING_ITEMS, renew_now=renew_now,
                              totals_by_currency=totals, bills_without_amount=missing)

    def overdue(self, owner_id: UUID, *, now: datetime | None = None) -> OverdueResult:
        """Active bills whose next due date is before local today (not marked paid)."""
        today = self.today(now)
        late = [bill for bill in self._all_bills(owner_id, "active")
                if bill.next_due_on is not None and bill.next_due_on < today]
        late.sort(key=lambda bill: (bill.next_due_on, bill.name.casefold(), str(bill.id)))
        totals, missing = _totals([(bill.amount, bill.currency) for bill in late])
        return OverdueResult(today=today, items=[OverdueItem(bill=self.bill_view(bill, today),
                                                             days_overdue=(today - bill.next_due_on).days)
                                                 for bill in late],
                             totals_by_currency=totals, bills_without_amount=missing)

    def briefing_summary(self, owner_id: UUID, *, days: int = 7, now: datetime | None = None) -> LifeAdminBriefing:
        """Counts and short lines a daily briefing could include later (v6.3's briefing does not call this yet).
        Lines name bills and documents by their titles only: no amounts, payees, holders or identifiers."""
        upcoming = self.upcoming(owner_id, days, now=now)
        overdue = self.overdue(owner_id, now=now)
        bills = [item for item in upcoming.items if item.event == "bill_due"]
        expiring = [item for item in upcoming.items if item.event == "document_expires"]
        lines = [f"Overdue: {item.bill.name} (due {item.bill.next_due_on.isoformat()})" for item in overdue.items[:5]]
        lines += [f"Bill due {item.date.isoformat()}: {item.title}" for item in bills[:5]]
        lines += [f"Renew now: {view.title} (expires {view.expires_on.isoformat()})" for view in upcoming.renew_now[:5]]
        lines += [f"Expires {item.date.isoformat()}: {item.title}" for item in expiring[:5]]
        return LifeAdminBriefing(today=upcoming.today, bills_due=len(bills), bills_overdue=len(overdue.items),
                                 documents_expiring=len(expiring), documents_renew_now=len(upcoming.renew_now),
                                 lines=lines)

    # ================================================================ reminders

    def _assistant(self) -> Any:
        if self.assistant_service is None:
            raise LifeAdminUnavailable("Reminders need the assistant service, which is unavailable.")
        return self.assistant_service

    def _target(self, kind: str, item: Document | Bill) -> tuple[date, datetime, str] | None:
        """(due date, planned moment, reminder title) or None when the item should have no reminder.
        Titles never contain holder names, identifiers, payees or amounts."""
        if kind == "document":
            if item.status != "active" or item.expires_on is None:
                return None
            label = _KIND_LABELS.get(item.kind, "document")
            title = f"Renew {label}: {item.title} (expires {item.expires_on.isoformat()})"
            return item.expires_on, self._moment(item.expires_on - timedelta(days=item.renewal_lead_days)), title[:200]
        if item.status != "active" or item.next_due_on is None:
            return None
        title = f"Bill due {item.next_due_on.isoformat()}: {item.name}"
        return (item.next_due_on, self._moment(item.next_due_on - timedelta(days=item.remind_days_before)),
                title[:200])

    def _delete_reminder(self, owner_id: UUID, reminder_id: UUID) -> int:
        if self.assistant_service is None:
            return 0
        try:
            return 1 if self.assistant_service.delete_reminder(owner_id, reminder_id) else 0
        except Exception as exc:  # noqa: BLE001 - best effort; the link row goes away anyway
            logger.warning("Failed to delete a life-admin reminder: %s", type(exc).__name__)
            return 0

    def _remove_linked(self, owner_id: UUID, item_id: UUID) -> int:
        link = self.store.get_link(owner_id, item_id)
        return self._delete_reminder(owner_id, link.reminder_id) if link is not None else 0

    def _drop(self, owner_id: UUID, link: ReminderLink) -> ReminderOutcome:
        self._delete_reminder(owner_id, link.reminder_id)
        self.store.delete_link(owner_id, link.item_id)
        return ReminderOutcome(item_kind=link.item_kind, item_id=link.item_id, reminder_id=None, action="removed")

    def _create(self, owner_id: UUID, kind: str, item_id: UUID, link: ReminderLink | None, due_on: date,
                remind_at: datetime, title: str, now: datetime) -> ReminderOutcome:
        from jarvis.assistant.models import ReminderCreateRequest

        scheduled = max(remind_at, now)
        reminder = self._assistant().create_reminder(owner_id, ReminderCreateRequest(title=title, remind_at=scheduled))
        try:
            if link is not None:
                stored = self.store.update_link(link.model_copy(update={
                    "reminder_id": reminder.id, "due_on": due_on, "remind_at": remind_at, "scheduled_at": scheduled,
                    "updated_at": now}))
                action = "recreated"
            else:
                stored = self.store.save_link(ReminderLink(
                    owner_id=owner_id, item_kind=kind, item_id=item_id, reminder_id=reminder.id, due_on=due_on,
                    remind_at=remind_at, scheduled_at=scheduled, created_at=now, updated_at=now))
                action = "created"
        except Exception:
            self._delete_reminder(owner_id, reminder.id)
            raise
        if not stored:  # the item was deleted, or linked, in the meantime
            self._delete_reminder(owner_id, reminder.id)
            existing = self.store.get_link(owner_id, item_id)
            return ReminderOutcome(item_kind=kind, item_id=item_id,
                                   reminder_id=existing.reminder_id if existing else None,
                                   action="unchanged" if existing else "removed",
                                   remind_at=existing.scheduled_at if existing else None)
        return ReminderOutcome(item_kind=kind, item_id=item_id, reminder_id=reminder.id, action=action,
                               remind_at=scheduled)

    def _reconcile(self, owner_id: UUID, kind: str, item: Document | Bill, link: ReminderLink | None,
                   now: datetime) -> ReminderOutcome | None:
        """Bring one item's reminder in line with the rules in the module docstring. Caller holds the lock."""
        from jarvis.assistant.models import ReminderStatus, ReminderUpdateRequest

        target = self._target(kind, item)
        if target is None:
            return self._drop(owner_id, link) if link is not None else None
        due_on, remind_at, title = target
        if due_on < self.today(now):
            if link is None:
                return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=None, action="skipped_past")
            if link.due_on == due_on:
                return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=link.reminder_id,
                                       action="kept_past", remind_at=link.scheduled_at)
            return self._drop(owner_id, link)
        assistant = self._assistant()
        current = assistant.get_reminder(owner_id, link.reminder_id) if link is not None else None
        same_date = link is not None and link.due_on == due_on
        # Same date and either the same planned moment, or both the old and new moments already passed: the
        # reminder has had (or is having) its one chance; never re-arm it.
        settled = same_date and (link.remind_at == remind_at or (link.remind_at <= now and remind_at <= now))
        if settled:
            if current is None:
                if remind_at <= now:
                    return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=None, action="skipped_past")
                return self._create(owner_id, kind, item.id, link, due_on, remind_at, title, now)
            if current.title != title:
                assistant.update_reminder(owner_id, current.id, ReminderUpdateRequest(title=title))
                return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=current.id, action="updated",
                                       remind_at=current.remind_at)
            if link.remind_at != remind_at:
                self.store.update_link(link.model_copy(update={"remind_at": remind_at, "updated_at": now}))
            return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=current.id, action="unchanged",
                                   remind_at=current.remind_at)
        if current is None:
            return self._create(owner_id, kind, item.id, link, due_on, remind_at, title, now)
        scheduled = max(remind_at, now)
        changes: dict[str, Any] = {"title": title, "remind_at": scheduled}
        if current.status != ReminderStatus.PENDING:
            changes["status"] = ReminderStatus.PENDING
        assistant.update_reminder(owner_id, current.id, ReminderUpdateRequest(**changes))
        self.store.update_link(link.model_copy(update={"due_on": due_on, "remind_at": remind_at,
                                                       "scheduled_at": scheduled, "updated_at": now}))
        return ReminderOutcome(item_kind=kind, item_id=item.id, reminder_id=current.id, action="updated",
                               remind_at=scheduled)

    def _follow(self, owner_id: UUID, kind: str, item: Document | Bill, now: datetime) -> str:
        """Keep an existing reminder in step with an edited item. Caller holds the lock."""
        link = self.store.get_link(owner_id, item.id)
        if link is None:
            return "none"
        if self.assistant_service is None:
            return "unavailable"
        outcome = self._reconcile(owner_id, kind, item, link, now)
        return outcome.action if outcome is not None else "none"

    def _renew_after_payment(self, owner_id: UUID, bill: Bill, now: datetime) -> str:
        """A linked bill gets a fresh reminder for its next due date. Caller holds the lock."""
        link = self.store.get_link(owner_id, bill.id)
        if link is None:
            return "none"
        if self.assistant_service is None:
            return "unavailable"
        self._drop(owner_id, link)
        outcome = self._reconcile(owner_id, "bill", bill, None, now)
        if outcome is None or outcome.action == "skipped_past":
            return "removed"
        return "recreated" if outcome.action == "created" else outcome.action

    def sync_reminders(self, owner_id: UUID, *, now: datetime | None = None) -> ReminderResult:
        self._assistant()
        now = now or _now()
        outcomes: list[ReminderOutcome] = []
        with self._reminder_lock:
            links = {link.item_id: link for link in self.store.list_links(owner_id)}
            for kind, items in (("document", self._all_documents(owner_id)), ("bill", self._all_bills(owner_id))):
                for item in items:
                    outcome = self._reconcile(owner_id, kind, item, links.pop(item.id, None), now)
                    if outcome is not None:
                        outcomes.append(outcome)
            for link in links.values():  # links whose item is gone
                outcomes.append(self._drop(owner_id, link))
        counts: dict[str, int] = {}
        for item in outcomes:
            counts[item.action] = counts.get(item.action, 0) + 1
        self._audit(owner_id, "lifeadmin.reminders.sync", "lifeadmin:reminders", counts)
        return ReminderResult(items=outcomes)
