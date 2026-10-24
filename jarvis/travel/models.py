"""Contracts for v7.4 travel and decision planning.

Everything here is owner-entered: options, prices and times. Nothing is booked, bought or looked up.
A segment's booking ``reference`` is sensitive: it is returned to the owner on segment reads only, and is
never written to audit records, logs, summaries or model prompts (``repr=False`` keeps it out of reprs).

Time zones are checked against the time zone database only on input (request models). Stored models built
from database rows check the name's shape only, so a missing or changed database cannot make reads fail.

Money: amounts are exact decimals in the currency's minor units (2 by default, 3 for e.g. KWD, 0 for e.g. JPY;
see ``MINOR_UNITS``). An amount with more decimals than the currency allows is refused, and every amount is
returned at the currency's scale. In JSON, amounts are strings (e.g. ``"379.50"``, ``"12000"`` for JPY) so no
precision is lost; requests may send numbers or strings.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

from pydantic import (
    BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StringConstraints, ValidationInfo, field_validator,
    model_validator,
)

# ------------------------------------------------------------------ shared

MAX_OPTIONS = 10
MAX_CRITERIA = 10
MAX_TRIP_DAYS = 366

SegmentKind = Literal["flight", "train", "bus", "car", "ferry", "stay", "activity", "other"]
TRANSPORT_KINDS = frozenset({"flight", "train", "bus", "car", "ferry"})
Direction = Literal["higher_better", "lower_better"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Place = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Notes = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
OptionNotes = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Value = Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-1e12, le=1e12)]
Weight = Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0, le=100)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"), max_digits=15, decimal_places=3)]
LeadMinutes = Annotated[StrictInt, Field(ge=15, le=1440)]
ConnectionMinutes = Annotated[StrictInt, Field(ge=0, le=1440)]

# Active ISO 4217 currency codes (funds, precious metals and testing codes are left out).
ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF
CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH
UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG
""".split())
# ISO 4217 minor units that differ from the default of 2.
MINOR_UNITS = {
    **{code: 3 for code in ("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND")},
    **{code: 0 for code in ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND",
                            "VUV", "XAF", "XOF", "XPF")},
}

_ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


def timezone_database_available() -> bool:
    try:
        ZoneInfo("UTC")
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def _zone_shape(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 64 or not _ZONE_NAME.fullmatch(name) or name.lower() in {"localtime", "local"}:
        raise ValueError(f"'{name[:64]}' is not an IANA time zone name (a name like 'Region/City').")
    return name


def validate_timezone(name: str) -> str:
    """Return the IANA zone name if ``zoneinfo`` knows it, else raise ``ValueError`` with a plain message."""
    name = _zone_shape(name)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        if not timezone_database_available():
            raise ValueError("Time zones cannot be checked: this system has no time zone database "
                             "(install the 'tzdata' package).") from exc
        raise ValueError(f"'{name}' is not a known IANA time zone name.") from exc
    return name


def validate_currency(value: str) -> str:
    code = value.strip().upper()
    if code not in ISO_4217:
        raise ValueError("Use a three-letter ISO 4217 currency code such as 'EUR', 'GBP', 'INR' or 'USD'.")
    return code


def minor_units(currency: str) -> int:
    return MINOR_UNITS.get(currency, 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-minor_units(currency))


def fit_money(value: Decimal | None, currency: str, what: str) -> Decimal | None:
    """Return ``value`` at the currency's scale; refuse more decimals than the currency has."""
    if value is None:
        return None
    step = quantum(currency)
    if value != value.quantize(step):
        units = minor_units(currency)
        raise ValueError(f"{what} has more decimals than {currency} allows "
                         f"({units} decimal place{'' if units == 1 else 's'}).")
    return value.quantize(step)


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _unique(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value.casefold() in seen:
            raise ValueError(f"{what} must be unique (ignoring case): '{value}' appears twice.")
        seen.add(value.casefold())


def _reject_nulls(model: BaseModel, names: tuple[str, ...]) -> None:
    for name in names:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null.")


# ------------------------------------------------------------------ decisions


class Criterion(StrictSchema):
    name: Name
    weight: Weight
    direction: Direction
    unit: Unit | None = None


class DecisionOption(StrictSchema):
    """``values`` maps a criterion name to a number; a missing key or ``null`` means unknown."""

    name: Name
    values: dict[Name, Value | None] = Field(default_factory=dict, max_length=MAX_CRITERIA)
    notes: OptionNotes | None = None

    def value(self, criterion: str) -> float | None:
        return self.values.get(criterion)


def check_decision(options: list[DecisionOption], criteria: list[Criterion]) -> None:
    _unique([item.name for item in options], "Option names")
    _unique([item.name for item in criteria], "Criterion names")
    names = {item.name for item in criteria}
    for option in options:
        unknown = sorted(set(option.values) - names)
        if unknown:
            raise ValueError(f"Option '{option.name}' has a value for '{unknown[0]}', which is not a criterion "
                             "(names must match exactly).")
    if not any(item.weight > 0 for item in criteria):
        raise ValueError("At least one criterion needs a weight above 0.")


class DecisionCreateRequest(StrictSchema):
    question: Question
    options: list[DecisionOption] = Field(min_length=2, max_length=MAX_OPTIONS)
    criteria: list[Criterion] = Field(min_length=1, max_length=MAX_CRITERIA)
    notes: Notes | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "DecisionCreateRequest":
        check_decision(self.options, self.criteria)
        return self


class DecisionUpdateRequest(StrictSchema):
    """Fields left out are unchanged; ``options`` and ``criteria`` replace the whole list."""

    question: Question | None = None
    options: list[DecisionOption] | None = Field(default=None, min_length=2, max_length=MAX_OPTIONS)
    criteria: list[Criterion] | None = Field(default=None, min_length=1, max_length=MAX_CRITERIA)
    notes: Notes | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "DecisionUpdateRequest":
        _reject_nulls(self, ("question", "options", "criteria"))
        return self


class Decision(StrictSchema):
    id: UUID
    owner_id: UUID
    question: Question
    options: list[DecisionOption] = Field(min_length=2, max_length=MAX_OPTIONS)
    criteria: list[Criterion] = Field(min_length=1, max_length=MAX_CRITERIA)
    notes: Notes | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> "Decision":
        check_decision(self.options, self.criteria)
        return self


class DecisionPage(StrictSchema):
    items: list[Decision]
    total: int = Field(ge=0)


class CriterionScore(StrictSchema):
    criterion: str
    value: float | None
    normalized: float | None = Field(description="0..1 after direction-aware min-max scaling; null when the value "
                                                 "is unknown or the criterion is not used.")
    weight_share: float | None = Field(description="Share of this option's score weight (re-weighted over its "
                                                    "known used criteria); null when not counted.")
    contribution: float | None = Field(description="Points of the 0..100 score this criterion contributed.")


class OptionResult(StrictSchema):
    option: str
    rank: int | None = Field(description="1 is best; tied options share a rank; null when it cannot be scored.")
    score: float | None = Field(description="0..100, rounded to 2 decimals; null when no used value is known.")
    coverage: float = Field(ge=0, le=1, description="Weight of the used criteria this option has values for, "
                                                    "divided by the weight of all used criteria.")
    tied: bool
    unknown_criteria: list[str]
    breakdown: list[CriterionScore]


ExclusionReason = Literal["no_values", "one_value", "same_for_all", "zero_weight"]


class ExcludedCriterion(StrictSchema):
    criterion: str
    reason: ExclusionReason
    message: str


class SensitivityItem(StrictSchema):
    criterion: str
    weight: float
    lower_threshold: float | None = Field(description="Below this weight the winner changes (0 means 'set to 0').")
    lower_new_winner: list[str] | None = None
    upper_threshold: float | None = Field(description="Above this weight the winner changes (0 means 'any weight "
                                                      "above 0').")
    upper_new_winner: list[str] | None = None
    smallest_change: float | None = Field(description="Signed weight change to the nearest threshold.")
    statement: str


class DecisionResult(StrictSchema):
    decision_id: UUID
    question: str
    method: str = ("Each used criterion is min-max scaled across the options that have a value (direction-aware). "
                   "A criterion that cannot separate the options (no value, a value for only one option, or the "
                   "same value for all) is not used for anyone. An option's score is the weighted mean of its "
                   "known scaled values; unknown values are left out and its remaining weights re-scaled, and its "
                   "coverage says how much of the used weight it has values for. Scores are relative to these "
                   "options only.")
    ranking: list[OptionResult]
    winners: list[str]
    tie: bool
    low_confidence_winner: bool = Field(description="The top option has values for under half of the used weight, "
                                                    "or for less than the runner-up: not a clear winner.")
    excluded_criteria: list[ExcludedCriterion]
    sensitivity: list[SensitivityItem]
    explanation: str


# ------------------------------------------------------------------ trips


class _TripMoney(StrictSchema):
    @field_validator("budget", "daily_food", "daily_local_transport", "daily_other", check_fields=False)
    @classmethod
    def _fits_currency(cls, value, info: ValidationInfo):
        currency = info.data.get("currency")
        return fit_money(value, currency, info.field_name) if isinstance(currency, str) and value is not None \
            else value


class _TripInput(_TripMoney):
    @field_validator("currency", mode="before", check_fields=False)
    @classmethod
    def _currency(cls, value):
        return validate_currency(value) if isinstance(value, str) else value

    @field_validator("home_timezone", mode="before", check_fields=False)
    @classmethod
    def _zone(cls, value):
        return validate_timezone(value) if isinstance(value, str) else value


def _check_dates(start: date, end: date) -> None:
    if end < start:
        raise ValueError("end_date must be on or after start_date.")
    if (end - start).days + 1 > MAX_TRIP_DAYS:
        raise ValueError(f"A trip can span at most {MAX_TRIP_DAYS} days.")


class TripCreateRequest(_TripInput):
    name: Title
    travellers: StrictInt = Field(default=1, ge=1, le=20)
    currency: str = Field(description="ISO 4217 code; every segment cost must use it (no conversion).")
    budget: Money | None = None
    start_date: date
    end_date: date
    home_timezone: str = Field(description="IANA time zone name, e.g. 'Asia/Kolkata'.")
    daily_food: Money | None = Field(default=None, description="Estimated cost per day for the whole party.")
    daily_local_transport: Money | None = Field(default=None, description="Per day, whole party.")
    daily_other: Money | None = Field(default=None, description="Per day, whole party.")
    min_connection_minutes_flight: ConnectionMinutes = 60
    min_connection_minutes_other: ConnectionMinutes = 15
    notes: Notes | None = None

    @model_validator(mode="after")
    def _dates(self) -> "TripCreateRequest":
        _check_dates(self.start_date, self.end_date)
        return self


class TripUpdateRequest(_TripInput):
    """Fields left out are unchanged; ``null`` clears an optional field. Amounts are checked against the
    trip's (possibly new) currency when the change is applied."""

    name: Title | None = None
    travellers: StrictInt | None = Field(default=None, ge=1, le=20)
    currency: str | None = None
    budget: Money | None = None
    start_date: date | None = None
    end_date: date | None = None
    home_timezone: str | None = None
    daily_food: Money | None = None
    daily_local_transport: Money | None = None
    daily_other: Money | None = None
    min_connection_minutes_flight: ConnectionMinutes | None = None
    min_connection_minutes_other: ConnectionMinutes | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "TripUpdateRequest":
        _reject_nulls(self, ("name", "travellers", "currency", "start_date", "end_date", "home_timezone",
                             "min_connection_minutes_flight", "min_connection_minutes_other"))
        return self


class Trip(_TripMoney):
    """Stored trip. Zone and currency are checked for shape only (they were fully checked on input)."""

    id: UUID
    owner_id: UUID
    name: Title
    travellers: StrictInt = Field(ge=1, le=20)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    budget: Money | None = None
    start_date: date
    end_date: date
    home_timezone: str
    daily_food: Money | None = None
    daily_local_transport: Money | None = None
    daily_other: Money | None = None
    min_connection_minutes_flight: ConnectionMinutes = 60
    min_connection_minutes_other: ConnectionMinutes = 15
    notes: Notes | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("home_timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return _zone_shape(value)

    @model_validator(mode="after")
    def _dates(self) -> "Trip":
        _check_dates(self.start_date, self.end_date)
        return self


class TripPage(StrictSchema):
    items: list[Trip]
    total: int = Field(ge=0)


# ------------------------------------------------------------------ segments


def _naive(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        raise ValueError(f"{name} is a local date-time: give it without an offset and put the zone in the "
                         "matching *_timezone field.")
    return value


class _SegmentInput(StrictSchema):
    @field_validator("start_timezone", "end_timezone", mode="before", check_fields=False)
    @classmethod
    def _zone(cls, value):
        return validate_timezone(value) if isinstance(value, str) else value

    @field_validator("cost_currency", mode="before", check_fields=False)
    @classmethod
    def _currency(cls, value):
        return validate_currency(value) if isinstance(value, str) else value

    @field_validator("start_local", "end_local", check_fields=False)
    @classmethod
    def _local(cls, value, info: ValidationInfo):
        return _naive(value, info.field_name)


class SegmentCreateRequest(_SegmentInput):
    kind: SegmentKind
    title: Title
    from_place: Place | None = None
    to_place: Place | None = None
    start_local: datetime = Field(description="Local date-time without offset, e.g. '2026-11-02T23:55'.")
    start_timezone: str = Field(description="IANA zone of the start, e.g. 'Asia/Kolkata'.")
    end_local: datetime
    end_timezone: str | None = Field(default=None, description="IANA zone of the end; defaults to start_timezone.")
    reference: Reference | None = Field(default=None, repr=False,
                                        description="Booking reference you typed; kept private, never audited.")
    cost_currency: str | None = Field(default=None, description="Must equal the trip currency (defaults to it).")
    cost_amount: Money | None = Field(default=None, description="At most the currency's decimal places.")
    notes: Notes | None = None


class SegmentUpdateRequest(_SegmentInput):
    """Fields left out are unchanged; ``null`` clears an optional field. When ``start_timezone`` changes and the
    end used the same zone as the old start, the end zone follows unless ``end_timezone`` is given."""

    kind: SegmentKind | None = None
    title: Title | None = None
    from_place: Place | None = None
    to_place: Place | None = None
    start_local: datetime | None = None
    start_timezone: str | None = None
    end_local: datetime | None = None
    end_timezone: str | None = None
    reference: Reference | None = Field(default=None, repr=False)
    cost_currency: str | None = None
    cost_amount: Money | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "SegmentUpdateRequest":
        _reject_nulls(self, ("kind", "title", "start_local", "start_timezone", "end_local", "end_timezone"))
        return self


class Segment(StrictSchema):
    """Stored segment. Zones are checked for shape only; the UTC instants were derived on input."""

    id: UUID
    owner_id: UUID
    trip_id: UUID
    kind: SegmentKind
    title: Title
    from_place: Place | None = None
    to_place: Place | None = None
    start_local: datetime
    start_timezone: str
    end_local: datetime
    end_timezone: str
    start_utc: datetime
    end_utc: datetime
    reference: Reference | None = Field(default=None, repr=False)
    cost_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    cost_amount: Money | None = None
    notes: Notes | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("start_timezone", "end_timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return _zone_shape(value)

    @field_validator("cost_amount")
    @classmethod
    def _fits_currency(cls, value, info: ValidationInfo):
        currency = info.data.get("cost_currency")
        return fit_money(value, currency, "cost_amount") if value is not None and isinstance(currency, str) else value

    @model_validator(mode="after")
    def _consistent(self) -> "Segment":
        if self.end_utc <= self.start_utc:
            raise ValueError("The segment must end after it starts (compared in absolute time).")
        if (self.cost_amount is None) != (self.cost_currency is None):
            raise ValueError("cost_amount and cost_currency go together.")
        return self


ReminderFollow = Literal["none", "unchanged", "updated", "removed", "orphaned", "unavailable"]


class SegmentUpdateResult(Segment):
    """The updated segment, plus what happened to its departure reminder and any automatic adjustment."""

    reminder_status: ReminderFollow = Field(
        default="none", description="none (no reminder), unchanged, updated, removed, orphaned (the reminder could "
                                    "not be deleted; the link is kept and the next sync retries) or unavailable "
                                    "(the assistant failed; the segment change is saved).")
    notices: list[str] = Field(default_factory=list)


class SegmentPage(StrictSchema):
    items: list[Segment]
    total: int = Field(ge=0)


# ------------------------------------------------------------------ summary


class TimelineEntry(StrictSchema):
    segment_id: UUID
    kind: SegmentKind
    title: str
    from_place: str | None
    to_place: str | None
    start_local: datetime = Field(description="Start in the segment's own zone (with offset).")
    start_timezone: str
    end_local: datetime
    end_timezone: str
    start_home: datetime = Field(description="Start in the trip's home zone (with offset).")
    end_home: datetime
    duration_minutes: int
    cost_amount: Decimal | None
    has_reference: bool


class SegmentConflict(StrictSchema):
    first_id: UUID
    first_title: str
    second_id: UUID
    second_title: str
    overlap_minutes: int = Field(ge=1)


class OvernightGap(StrictSchema):
    night_of: date
    in_transit: bool = Field(description="A transport segment is under way at local midnight (home zone) "
                                         "ending this night.")
    message: str


class ConnectionWarning(StrictSchema):
    kind: Literal["short_connection", "place_mismatch"]
    from_segment_id: UUID
    to_segment_id: UUID
    minutes: int | None = None
    minimum_minutes: int | None = None
    message: str


class OutsideTripDates(StrictSchema):
    segment_id: UUID
    title: str
    message: str


class CostEstimate(StrictSchema):
    label: Literal["estimate"] = "estimate"
    currency: str
    minor_units: int
    by_kind: dict[str, Decimal]
    segments_total: Decimal
    segments_without_cost: int
    days: int
    daily_food: Decimal | None
    daily_local_transport: Decimal | None
    daily_other: Decimal | None
    daily_per_day: Decimal
    daily_total: Decimal
    total: Decimal
    travellers: int
    per_traveller: Decimal
    budget: Decimal | None
    remaining: Decimal | None
    over_by: Decimal | None
    notes: list[str]


class TripSummary(StrictSchema):
    trip_id: UUID
    name: str
    home_timezone: str
    start_date: date
    end_date: date
    timeline: list[TimelineEntry]
    conflicts: list[SegmentConflict]
    gaps: list[OvernightGap]
    connection_warnings: list[ConnectionWarning]
    outside_trip_dates: list[OutsideTripDates]
    time_notes: list[str]
    cost: CostEstimate


# ------------------------------------------------------------------ reminders


class ReminderRequest(StrictSchema):
    flight_lead_minutes: LeadMinutes = 180
    other_lead_minutes: LeadMinutes = 60


class ReminderLink(StrictSchema):
    owner_id: UUID
    trip_id: UUID
    segment_id: UUID
    reminder_id: UUID
    lead_minutes: int = Field(ge=15, le=1440)
    remind_at: datetime = Field(description="The reminder time JARVIS last set.")
    departure_utc: datetime = Field(description="The segment start the reminder was last set for.")
    created_at: datetime
    updated_at: datetime


ReminderAction = Literal["created", "recreated", "updated", "unchanged", "removed", "skipped_departed", "orphaned",
                         "failed"]


class ReminderOutcome(StrictSchema):
    segment_id: UUID
    reminder_id: UUID | None
    action: ReminderAction
    remind_at: datetime | None = None
    detail: str | None = None


class ReminderResult(StrictSchema):
    trip_id: UUID
    items: list[ReminderOutcome]
    note: str = ("One assistant reminder per transport segment (flight, train, bus, car, ferry) that has not "
                 "departed. Running this again does not duplicate reminders. A reminder time you changed in the "
                 "assistant (snooze) and its status are kept unless the departure itself moves. Editing a "
                 "segment's departure moves its reminder; deleting it, or changing it to a non-transport kind or "
                 "to a time already past, removes the reminder. 'orphaned' means a reminder could not be deleted "
                 "(the next run retries); 'failed' means the assistant could not be reached for that segment.")


# ------------------------------------------------------------------ model-assisted


class SuggestRequest(StrictSchema):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class SuggestResponse(StrictSchema):
    text: str
    model: str
    demo: bool
    truncated: bool = False
    notes: list[str]
