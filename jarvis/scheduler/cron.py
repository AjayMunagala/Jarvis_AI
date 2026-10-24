"""Standard five-field cron expressions and strict schedule validation (v4.5).

Before v4.5 every cron expression ran hourly, and an unparsable interval or
"once" value silently fell back to five minutes or one minute. Now a schedule
is validated when a job is created or changed (an invalid one is refused),
and the next run time is computed from what the expression actually says.

Cron fields: minute (0-59), hour (0-23), day of month (1-31), month (1-12),
day of week (0-6, Sunday = 0; 7 is also accepted as Sunday). Each field
accepts ``*``, numbers, ranges ``a-b``, lists ``a,b`` and steps ``*/n`` or
``a-b/n``. Times are evaluated in UTC. As in standard cron, when both day of
month and day of week are restricted, a day matching either one is due.
"""

from datetime import datetime, timedelta, timezone

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
_MAX_DAYS_AHEAD = 366 * 5  # covers every valid expression, including 29 February


class ScheduleError(ValueError):
    """The schedule expression is invalid; the message says why."""


def _parse_field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"Empty value in the cron {name} field.")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"Invalid step '{step_text}' in the cron {name} field.")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            if not (first.isdigit() and last.isdigit()):
                raise ScheduleError(f"Invalid range '{base}' in the cron {name} field.")
            start, end = int(first), int(last)
        elif base.isdigit():
            start = end = int(base)
            if step_text:
                end = high
        else:
            raise ScheduleError(f"Invalid value '{base}' in the cron {name} field.")
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise ScheduleError(f"Value '{base}' is outside {low}-{high} in the cron {name} field.")
        values.update(range(start, end + 1, step))
    return values


class CronExpression:
    def __init__(self, expression: str):
        parts = expression.split()
        if len(parts) != 5:
            raise ScheduleError("A cron expression needs five fields: minute hour day-of-month month day-of-week.")
        parsed = [_parse_field(part, name, low, high) for part, (name, low, high) in zip(parts, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, weekdays = parsed
        self.weekdays = {0 if day == 7 else day for day in weekdays}
        # As in Vixie cron, a field that starts with "*" (for example "*/2") does not restrict the day.
        self.day_restricted = not parts[2].startswith("*")
        self.weekday_restricted = not parts[4].startswith("*")

    def _day_matches(self, moment: datetime) -> bool:
        cron_weekday = (moment.weekday() + 1) % 7  # Python: Monday = 0; cron: Sunday = 0
        day_ok, weekday_ok = moment.day in self.days, cron_weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, moment: datetime) -> datetime:
        """The first matching minute strictly after ``moment`` (UTC)."""
        candidate = moment.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=_MAX_DAYS_AHEAD)
        while candidate < limit:
            if candidate.month not in self.months or not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue
            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
                continue
            return candidate
        raise ScheduleError("The cron expression never matches a real date.")


def validate_schedule(schedule_type: str, schedule_expr: str, *, now: datetime | None = None) -> datetime:
    """Return the first run time for a valid schedule, or raise ScheduleError."""
    now = now or datetime.now(timezone.utc)
    expr = schedule_expr.strip()
    if schedule_type == "interval":
        try:
            seconds = float(expr)
        except ValueError as exc:
            raise ScheduleError("An interval schedule must be a number of seconds.") from exc
        if not 60 <= seconds <= 366 * 86_400:
            raise ScheduleError("An interval must be between 60 seconds and 366 days.")
        return now + timedelta(seconds=seconds)
    if schedule_type == "once":
        try:
            seconds = float(expr)
        except ValueError:
            try:
                moment = datetime.fromisoformat(expr)
            except ValueError as exc:
                raise ScheduleError("A one-time schedule must be seconds from now or an ISO 8601 date-time.") from exc
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        if not 0 <= seconds <= 366 * 86_400:  # also rejects nan and inf
            raise ScheduleError("A one-time delay must be between 0 seconds and 366 days.")
        return now + timedelta(seconds=seconds)
    if schedule_type == "cron":
        return CronExpression(expr).next_after(now)
    raise ScheduleError(f"Unknown schedule type '{schedule_type}'.")
