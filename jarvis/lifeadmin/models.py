"""Contracts for v7.5 life administration: documents with expiry dates, bills and renewals, reminders.

Everything is owner-entered metadata. No document image or file is stored, no payment is made, no bank or
provider account is accessed and nothing is looked up.

Sensitive fields: a document's ``holder_name`` and ``identifier_last4``, and a bill's ``payee`` and amounts.
They are returned to the owner on reads only and never written to audit records, logs, error messages or
reminder titles; ``repr=False`` keeps them out of reprs. A full identifier number is never accepted: only the
last 4 characters (letters and digits) are kept, and a longer value is refused without being echoed.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator, \
    model_validator

from jarvis.travel.models import validate_currency

# ------------------------------------------------------------------ shared

DocumentKind = Literal["passport", "national_id", "driving_licence", "visa", "insurance", "warranty",
                       "vehicle_registration", "lease", "subscription", "other"]
DocumentStatus = Literal["active", "renewed", "expired", "archived"]
ExpiryState = Literal["ok", "renew_soon", "expired"]
Recurrence = Literal["one_time", "weekly", "monthly", "quarterly", "yearly", "every_n_days"]
BillStatus = Literal["active", "archived"]

DEFAULT_LEAD_DAYS: dict[str, int] = {"passport": 270, "visa": 60, "driving_licence": 60, "insurance": 30}
FALLBACK_LEAD_DAYS = 30
MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2200, 12, 31)
MIN_DUE_DATE = date(2000, 1, 1)

PASSPORT_GUIDANCE = ("Many countries require a passport to stay valid for some months (often 6) beyond your "
                     "travel dates. This is general guidance, not a legal rule: check the destination's rules.")


def default_lead_days(kind: str) -> int:
    return DEFAULT_LEAD_DAYS.get(kind, FALLBACK_LEAD_DAYS)


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
HolderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Issuer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Payee = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Category = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
PaymentNote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
# What the owner sends. Its length is checked by the service so that a longer value gets a clear refusal (the
# app's generic 422 handler hides validator messages); the value itself is never echoed.
Last4Input = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# What is stored: 1 to 4 letters or digits.
Last4 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,4}$")]
LeadDays = Annotated[StrictInt, Field(ge=1, le=730)]
RemindDays = Annotated[StrictInt, Field(ge=0, le=30)]
IntervalDays = Annotated[StrictInt, Field(ge=1, le=3650)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"), max_digits=15, decimal_places=3)]


class StrictSchema(BaseModel):
    # hide_input_in_errors: a validation error never carries a sensitive value (holder, identifier, payee, amount).
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, hide_input_in_errors=True)


def _reject_nulls(model: BaseModel, names: tuple[str, ...]) -> None:
    for name in names:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null.")


def _check_range(value: date | None, name: str, low: date = MIN_DATE) -> None:
    if value is not None and not low <= value <= MAX_DATE:
        raise ValueError(f"{name} must be between {low.isoformat()} and {MAX_DATE.isoformat()}.")


# ------------------------------------------------------------------ documents


class _DocumentDates(StrictSchema):
    @model_validator(mode="after")
    def _dates(self):
        issued, expires = getattr(self, "issued_on", None), getattr(self, "expires_on", None)
        _check_range(issued, "issued_on")
        _check_range(expires, "expires_on")
        if issued is not None and expires is not None and expires < issued:
            raise ValueError("expires_on must be on or after issued_on.")
        return self


class DocumentCreateRequest(_DocumentDates):
    kind: DocumentKind
    title: Title
    holder_name: HolderName | None = Field(default=None, repr=False, description="Sensitive; never audited.")
    issuer: Issuer | None = None
    identifier_last4: Last4Input | None = Field(
        default=None, repr=False,
        description="Only the last 4 letters or digits of the document number. A longer value is refused.")
    issued_on: date | None = None
    expires_on: date | None = None
    renewal_lead_days: LeadDays | None = Field(
        default=None, description="Days before expiry to start renewing; defaults by kind (passport 270, visa 60, "
                                  "driving licence 60, others 30).")
    notes: Notes | None = None
    status: DocumentStatus = "active"


class DocumentUpdateRequest(_DocumentDates):
    """Fields left out are unchanged; ``null`` clears an optional field."""

    kind: DocumentKind | None = None
    title: Title | None = None
    holder_name: HolderName | None = Field(default=None, repr=False)
    issuer: Issuer | None = None
    identifier_last4: Last4Input | None = Field(default=None, repr=False)
    issued_on: date | None = None
    expires_on: date | None = None
    renewal_lead_days: LeadDays | None = None
    notes: Notes | None = None
    status: DocumentStatus | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "DocumentUpdateRequest":
        _reject_nulls(self, ("kind", "title", "renewal_lead_days", "status"))
        return self


class Document(_DocumentDates):
    id: UUID
    owner_id: UUID
    kind: DocumentKind
    title: Title
    holder_name: HolderName | None = Field(default=None, repr=False)
    issuer: Issuer | None = None
    identifier_last4: Last4 | None = Field(default=None, repr=False)
    issued_on: date | None = None
    expires_on: date | None = None
    renewal_lead_days: LeadDays
    notes: Notes | None = None
    status: DocumentStatus = "active"
    created_at: datetime
    updated_at: datetime


class DocumentView(Document):
    """A document with values computed for the owner's local today."""

    days_until_expiry: int | None = Field(description="Negative once expired; null without an expiry date.")
    renew_by: date | None = Field(description="expires_on minus renewal_lead_days.")
    state: ExpiryState = Field(description="expired after expires_on; renew_soon from renew_by to expires_on; "
                                           "ok otherwise (also when there is no expiry date).")
    guidance: list[str]


class DocumentPage(StrictSchema):
    items: list[DocumentView]
    total: int = Field(ge=0)


# ------------------------------------------------------------------ bills


class _BillFields(StrictSchema):
    @field_validator("currency", mode="before", check_fields=False)
    @classmethod
    def _currency(cls, value):
        return validate_currency(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _dates(self):
        _check_range(getattr(self, "next_due_on", None), "next_due_on", MIN_DUE_DATE)
        _check_range(getattr(self, "end_on", None), "end_on", MIN_DUE_DATE)
        return self


def check_recurrence(recurrence: str, interval_days: int | None) -> None:
    if recurrence == "every_n_days" and interval_days is None:
        raise ValueError("interval_days (1 to 3650) is required when recurrence is every_n_days.")
    if recurrence != "every_n_days" and interval_days is not None:
        raise ValueError("interval_days is only used when recurrence is every_n_days.")


class BillCreateRequest(_BillFields):
    name: Title
    payee: Payee | None = Field(default=None, repr=False, description="Sensitive; never audited.")
    amount: Money | None = Field(default=None, repr=False, description="Leave empty for a variable bill.")
    currency: str = Field(description="ISO 4217 code. There is no currency conversion.")
    recurrence: Recurrence
    interval_days: IntervalDays | None = None
    next_due_on: date
    end_on: date | None = Field(default=None, description="No due date after this one.")
    autopay: StrictBool = Field(default=False, description="Informational only: JARVIS never pays anything.")
    account_id: UUID | None = Field(default=None, description="Your finance account this is paid from (context "
                                                              "only; read-only, nothing is moved).")
    category: Category | None = None
    notes: Notes | None = None
    remind_days_before: RemindDays = 3

    @model_validator(mode="after")
    def _consistent(self) -> "BillCreateRequest":
        check_recurrence(self.recurrence, self.interval_days)
        if self.end_on is not None and self.end_on < self.next_due_on:
            raise ValueError("end_on must be on or after next_due_on.")
        return self


class BillUpdateRequest(_BillFields):
    """Fields left out are unchanged; ``null`` clears an optional field. Setting ``next_due_on`` also makes its
    day of the month the new anchor for monthly, quarterly and yearly bills."""

    name: Title | None = None
    payee: Payee | None = Field(default=None, repr=False)
    amount: Money | None = Field(default=None, repr=False)
    currency: str | None = None
    recurrence: Recurrence | None = None
    interval_days: IntervalDays | None = None
    next_due_on: date | None = None
    end_on: date | None = None
    autopay: StrictBool | None = None
    account_id: UUID | None = None
    category: Category | None = None
    notes: Notes | None = None
    remind_days_before: RemindDays | None = None
    status: BillStatus | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "BillUpdateRequest":
        _reject_nulls(self, ("name", "currency", "recurrence", "next_due_on", "autopay", "remind_days_before",
                             "status"))
        return self


class Bill(_BillFields):
    id: UUID
    owner_id: UUID
    name: Title
    payee: Payee | None = Field(default=None, repr=False)
    amount: Money | None = Field(default=None, repr=False)
    currency: str
    recurrence: Recurrence
    interval_days: IntervalDays | None = None
    next_due_on: date | None = Field(description="null once a one-time bill is paid or the end date is passed.")
    anchor_day: StrictInt = Field(ge=1, le=31, description="Day of the month monthly, quarterly and yearly bills "
                                                           "return to (Jan 31 -> Feb 28/29 -> Mar 31).")
    end_on: date | None = None
    autopay: StrictBool = False
    account_id: UUID | None = None
    category: Category | None = None
    notes: Notes | None = None
    remind_days_before: RemindDays = 3
    status: BillStatus = "active"
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> "Bill":
        check_recurrence(self.recurrence, self.interval_days)
        return self


class BillView(Bill):
    days_until_due: int | None = Field(description="Negative when overdue; null when nothing is due.")
    overdue: bool


class BillPage(StrictSchema):
    items: list[BillView]
    total: int = Field(ge=0)


class PaidRequest(StrictSchema):
    paid_on: date | None = Field(default=None, description="Defaults to your local today; cannot be in the future.")
    amount: Money | None = Field(default=None, repr=False, description="Defaults to the bill amount.")
    note: PaymentNote | None = None


class Payment(StrictSchema):
    id: UUID
    owner_id: UUID
    bill_id: UUID
    paid_on: date
    due_on: date = Field(description="The due date this payment settled.")
    amount: Money | None = Field(default=None, repr=False)
    currency: str
    note: PaymentNote | None = None
    created_at: datetime


class PaymentPage(StrictSchema):
    items: list[Payment]
    total: int = Field(ge=0)


class PaidResult(StrictSchema):
    bill: BillView
    payment: Payment
    payments_pruned: int = Field(ge=0, description="Oldest payment entries removed to stay within the cap.")
    reminder: str
    note: str = "Recorded for your history only. JARVIS did not pay anything or move any money."


# ------------------------------------------------------------------ upcoming and overdue


UpcomingEvent = Literal["bill_due", "document_expires", "renewal_window_opens"]


class UpcomingItem(StrictSchema):
    item_kind: Literal["bill", "document"]
    item_id: UUID
    title: str
    event: UpcomingEvent
    date: date
    days_from_today: int
    amount: Decimal | None = None
    currency: str | None = None
    autopay: bool | None = None
    occurrence: int | None = Field(default=None, description="1 for a bill's next due date, 2 for the one after.")


class UpcomingResult(StrictSchema):
    today: date
    days: int
    items: list[UpcomingItem]
    truncated: bool = False
    renew_now: list[DocumentView] = Field(description="Active documents already inside their renewal window.")
    totals_by_currency: dict[str, Decimal] = Field(description="Bill amounts due in the window, per currency; "
                                                               "no conversion.")
    bills_without_amount: int = Field(description="Bill due dates in the window with no amount (variable bills).")


class OverdueItem(StrictSchema):
    bill: BillView
    days_overdue: int = Field(ge=1)


class OverdueResult(StrictSchema):
    today: date
    items: list[OverdueItem]
    totals_by_currency: dict[str, Decimal]
    bills_without_amount: int


class LifeAdminBriefing(StrictSchema):
    """Counts and short lines a daily briefing could show (no amounts, payees, holders or identifiers)."""

    today: date
    bills_due: int
    bills_overdue: int
    documents_expiring: int
    documents_renew_now: int
    lines: list[str]


# ------------------------------------------------------------------ reminders

ItemKind = Literal["document", "bill"]


class ReminderLink(StrictSchema):
    owner_id: UUID
    item_kind: ItemKind
    item_id: UUID
    reminder_id: UUID
    due_on: date = Field(description="The expiry or due date the reminder was made for.")
    remind_at: datetime = Field(description="The planned moment: 09:00 local on the reminder day.")
    scheduled_at: datetime = Field(description="What the assistant reminder was set to (now, once, when the planned "
                                               "moment had already passed).")
    created_at: datetime
    updated_at: datetime


ReminderAction = Literal["created", "recreated", "updated", "unchanged", "removed", "kept_past", "skipped_past"]


class ReminderOutcome(StrictSchema):
    item_kind: ItemKind
    item_id: UUID
    reminder_id: UUID | None
    action: ReminderAction
    remind_at: datetime | None = None


class ReminderResult(StrictSchema):
    items: list[ReminderOutcome]
    note: str = (
        "One assistant reminder per active document with an expiry date (at 09:00 local, renewal_lead_days before "
        "expiry) and per active bill with a due date (remind_days_before the due date). Running this again does not "
        "duplicate reminders. When the planned moment has already passed but the date is today or later, the "
        "reminder is set to now once, and is not created or re-armed again for the same date. Reminders follow date "
        "changes and are removed when the item is archived, renewed, expired or deleted.")
