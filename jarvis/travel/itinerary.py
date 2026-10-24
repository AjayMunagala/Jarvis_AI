"""Itinerary checks and cost estimates for v7.4 trips (deterministic, no network, no conversion).

Times: each segment end is a local date-time plus an IANA zone. ``resolve_local_time`` turns that into an
absolute (UTC) instant with ``zoneinfo``: a local time that does not exist (skipped when clocks go forward)
is refused; a local time that happens twice (when clocks go back) uses the first occurrence and is reported.

Checks in the summary:
* conflicts: two segments that overlap in absolute time, except a stay overlapping a non-stay (you travel or
  do things while a room is booked); two stays that overlap are a conflict.
* overnight gaps (multi-day trips): a night ``d`` (from ``start_date`` to the day before ``end_date``) is
  covered by a stay whose local start date is on or before ``d`` and local end date after ``d``. An uncovered
  night is reported, marked ``in_transit`` when a transport segment is under way (in absolute time) at the
  midnight that ends night ``d`` in the trip's home zone, so date-line crossings are handled.
* connections: non-stay segments in start order; the next one starting less than the minimum after the
  previous one ends (the flight minimum when either is a flight, else the other minimum) is a warning.
  Between consecutive transport segments, a start place that differs from the previous end place
  (case-insensitive text) is also a warning.
* segments whose local dates fall outside the trip dates are listed as a warning; they still count in the
  timeline and the cost estimate.

Money is reported at the trip currency's minor units; per traveller is rounded half-up to them.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.travel.models import (
    TRANSPORT_KINDS, ConnectionWarning, CostEstimate, OutsideTripDates, OvernightGap, Segment, SegmentConflict,
    TimelineEntry, Trip, TripSummary, minor_units, quantum,
)

MAX_LISTED = 200


def resolve_local_time(local: datetime, zone: str) -> tuple[datetime, bool]:
    """Return (UTC instant, ambiguous). Raises ``ValueError`` for a local time that does not exist."""
    tz = ZoneInfo(zone)
    first = local.replace(tzinfo=tz, fold=0)
    instant = first.astimezone(timezone.utc)
    if instant.astimezone(tz).replace(tzinfo=None) != local:
        raise ValueError(f"{local.isoformat(sep=' ', timespec='minutes')} does not exist in {zone} "
                         "(the clocks skip that time); use a time after the change.")
    ambiguous = first.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset()
    return instant, ambiguous


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _place(text: str | None) -> str | None:
    return " ".join(text.casefold().split()) if text else None


def _start_place(segment: Segment) -> str | None:
    return segment.from_place or segment.to_place


def _end_place(segment: Segment) -> str | None:
    return segment.to_place or segment.from_place


def _covers_night(segment: Segment, night: date) -> bool:
    return segment.start_local.date() <= night < segment.end_local.date()


def find_conflicts(segments: list[Segment]) -> list[SegmentConflict]:
    ordered = sorted(segments, key=lambda s: (s.start_utc, s.end_utc, str(s.id)))
    conflicts = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second.start_utc >= first.end_utc:
                break
            if (first.kind == "stay") != (second.kind == "stay"):
                continue
            overlap = min(first.end_utc, second.end_utc) - second.start_utc
            if overlap > timedelta(0):
                conflicts.append(SegmentConflict(first_id=first.id, first_title=first.title, second_id=second.id,
                                                 second_title=second.title,
                                                 overlap_minutes=max(1, _minutes(overlap))))
    return conflicts


def find_gaps(trip: Trip, segments: list[Segment], home: ZoneInfo) -> list[OvernightGap]:
    stays = [s for s in segments if s.kind == "stay"]
    moving = [s for s in segments if s.kind in TRANSPORT_KINDS]
    gaps = []
    night = trip.start_date
    while night < trip.end_date:
        if not any(_covers_night(stay, night) for stay in stays):
            midnight = datetime.combine(night + timedelta(days=1), datetime.min.time(), tzinfo=home)
            midnight = midnight.astimezone(timezone.utc)
            in_transit = any(item.start_utc <= midnight < item.end_utc for item in moving)
            gaps.append(OvernightGap(
                night_of=night, in_transit=in_transit,
                message=(f"No stay covers the night of {night.isoformat()}"
                         + (" (you are travelling overnight)." if in_transit else "."))))
        night += timedelta(days=1)
    return gaps


def find_connection_warnings(trip: Trip, segments: list[Segment]) -> list[ConnectionWarning]:
    chain = sorted((s for s in segments if s.kind != "stay"), key=lambda s: (s.start_utc, s.end_utc, str(s.id)))
    warnings = []
    for previous, following in zip(chain, chain[1:]):
        gap = _minutes(following.start_utc - previous.end_utc)
        minimum = (trip.min_connection_minutes_flight if "flight" in (previous.kind, following.kind)
                   else trip.min_connection_minutes_other)
        if 0 <= gap < minimum:
            warnings.append(ConnectionWarning(
                kind="short_connection", from_segment_id=previous.id, to_segment_id=following.id, minutes=gap,
                minimum_minutes=minimum,
                message=f"Only {gap} min between '{previous.title}' and '{following.title}' (minimum {minimum} min)."))
    transport = [s for s in chain if s.kind in TRANSPORT_KINDS]
    for previous, following in zip(transport, transport[1:]):
        end, start = _place(_end_place(previous)), _place(_start_place(following))
        if end and start and end != start:
            warnings.append(ConnectionWarning(
                kind="place_mismatch", from_segment_id=previous.id, to_segment_id=following.id,
                message=(f"'{previous.title}' ends at '{_end_place(previous)}' but '{following.title}' starts at "
                         f"'{_start_place(following)}'.")))
    return warnings


def find_outside(trip: Trip, segments: list[Segment]) -> list[OutsideTripDates]:
    items = []
    for segment in sorted(segments, key=lambda s: (s.start_utc, str(s.id))):
        if segment.start_local.date() < trip.start_date or segment.end_local.date() > trip.end_date:
            items.append(OutsideTripDates(
                segment_id=segment.id, title=segment.title,
                message=(f"'{segment.title}' ({segment.start_local.date().isoformat()} to "
                         f"{segment.end_local.date().isoformat()}, local) is outside the trip dates "
                         f"{trip.start_date.isoformat()} to {trip.end_date.isoformat()}; it is still counted.")))
    return items


def estimate_costs(trip: Trip, segments: list[Segment]) -> CostEstimate:
    step = quantum(trip.currency)

    def money(value: Decimal) -> Decimal:
        return value.quantize(step, rounding=ROUND_HALF_UP)

    by_kind: dict[str, Decimal] = {}
    without = 0
    for segment in segments:
        if segment.cost_amount is None:
            without += 1
            continue
        by_kind[segment.kind] = by_kind.get(segment.kind, Decimal(0)) + segment.cost_amount
    segments_total = sum(by_kind.values(), Decimal(0))
    days = (trip.end_date - trip.start_date).days + 1
    per_day = sum((value for value in (trip.daily_food, trip.daily_local_transport, trip.daily_other)
                   if value is not None), Decimal(0))
    daily_total = per_day * days
    total = segments_total + daily_total
    remaining = over_by = None
    if trip.budget is not None:
        if total <= trip.budget:
            remaining = trip.budget - total
        else:
            over_by = total - trip.budget
    notes = ["Estimate only: every price is owner-entered and unverified; nothing was booked or looked up.",
             f"All amounts are in {trip.currency}; there is no currency conversion.",
             f"Daily costs are for the whole party per day, times {days} trip day(s) (start and end dates included).",
             f"per_traveller is the total divided by the number of travellers, rounded half-up to "
             f"{minor_units(trip.currency)} decimal place(s) ({trip.currency} minor units)."]
    if without:
        notes.append(f"{without} segment(s) have no cost entered and add nothing to the total.")
    if find_outside(trip, segments):
        notes.append("Segments outside the trip dates are included in these totals.")

    def optional(value: Decimal | None) -> Decimal | None:
        return money(value) if value is not None else None

    return CostEstimate(currency=trip.currency, minor_units=minor_units(trip.currency),
                        by_kind={kind: money(value) for kind, value in by_kind.items()},
                        segments_total=money(segments_total), segments_without_cost=without, days=days,
                        daily_food=optional(trip.daily_food), daily_local_transport=optional(trip.daily_local_transport),
                        daily_other=optional(trip.daily_other), daily_per_day=money(per_day),
                        daily_total=money(daily_total), total=money(total), travellers=trip.travellers,
                        per_traveller=money(total / trip.travellers), budget=optional(trip.budget),
                        remaining=optional(remaining), over_by=optional(over_by), notes=notes)


def build_summary(trip: Trip, segments: list[Segment]) -> TripSummary:
    home = ZoneInfo(trip.home_timezone)
    ordered = sorted(segments, key=lambda s: (s.start_utc, s.end_utc, str(s.id)))
    timeline = []
    time_notes = []
    for segment in ordered:
        for local, zone, which in ((segment.start_local, segment.start_timezone, "starts"),
                                   (segment.end_local, segment.end_timezone, "ends")):
            try:
                _, ambiguous = resolve_local_time(local, zone)
            except (ValueError, ZoneInfoNotFoundError, OSError):
                ambiguous = False  # stored before a time zone database change; the stored instant is used
            if ambiguous:
                time_notes.append(f"'{segment.title}' {which} at {local.isoformat(sep=' ', timespec='minutes')}, "
                                  f"which happens twice in {zone} (clocks go back); the first occurrence is used.")
        timeline.append(TimelineEntry(
            segment_id=segment.id, kind=segment.kind, title=segment.title, from_place=segment.from_place,
            to_place=segment.to_place, start_local=segment.start_utc.astimezone(ZoneInfo(segment.start_timezone)),
            start_timezone=segment.start_timezone,
            end_local=segment.end_utc.astimezone(ZoneInfo(segment.end_timezone)), end_timezone=segment.end_timezone,
            start_home=segment.start_utc.astimezone(home), end_home=segment.end_utc.astimezone(home),
            duration_minutes=_minutes(segment.end_utc - segment.start_utc), cost_amount=segment.cost_amount,
            has_reference=segment.reference is not None))
    return TripSummary(trip_id=trip.id, name=trip.name, home_timezone=trip.home_timezone,
                       start_date=trip.start_date, end_date=trip.end_date, timeline=timeline,
                       conflicts=find_conflicts(segments)[:MAX_LISTED], gaps=find_gaps(trip, segments, home),
                       connection_warnings=find_connection_warnings(trip, segments)[:MAX_LISTED],
                       outside_trip_dates=find_outside(trip, segments), time_notes=time_notes,
                       cost=estimate_costs(trip, segments))
