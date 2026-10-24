"""Due-date arithmetic for bills (v7.5).

Monthly, quarterly and yearly bills return to an *anchor* day of the month: the day of the first due date (or of
the last due date the owner set). When a month is shorter, the last day of that month is used, and the next
month goes back to the anchor: Jan 31 -> Feb 28 (Feb 29 in a leap year) -> Mar 31 -> Apr 30 -> May 31.
A yearly bill anchored on Feb 29 falls on Feb 28 in common years and Feb 29 in leap years.
Weekly is every 7 days, every_n_days every n days (1-3650), and a one-time bill has no next date.
A next date after the bill's end date means the bill is finished (``None``).
"""

from calendar import monthrange
from datetime import date, timedelta

from jarvis.lifeadmin.models import MAX_DATE

_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def add_months(day: date, months: int, anchor_day: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(anchor_day, monthrange(year, month)[1]))


def next_due(current: date, recurrence: str, *, interval_days: int | None, anchor_day: int,
             end_on: date | None = None) -> date | None:
    """The due date after ``current``, or ``None`` when there is none (one-time, past the end date or range)."""
    if recurrence == "one_time":
        return None
    if recurrence == "weekly":
        step = timedelta(days=7)
    elif recurrence == "every_n_days":
        if interval_days is None or interval_days < 1:
            raise ValueError("every_n_days needs interval_days of at least 1.")
        step = timedelta(days=interval_days)
    elif recurrence in _MONTHS:
        step = None
    else:
        raise ValueError("Unknown recurrence.")
    if step is not None:
        if current > MAX_DATE - step:
            return None
        following = current + step
    else:
        if current.year >= MAX_DATE.year:
            return None
        following = add_months(current, _MONTHS[recurrence], anchor_day)
    if following > MAX_DATE or (end_on is not None and following > end_on):
        return None
    return following


def occurrences(first: date | None, recurrence: str, *, interval_days: int | None, anchor_day: int,
                end_on: date | None, until: date, limit: int) -> list[date]:
    """Due dates from ``first`` up to and including ``until`` (at most ``limit``)."""
    found: list[date] = []
    current = first
    while current is not None and current <= until and len(found) < limit:
        found.append(current)
        current = next_due(current, recurrence, interval_days=interval_days, anchor_day=anchor_day, end_on=end_on)
    return found
