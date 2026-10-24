"""Forecast snapshots and out-of-sample accuracy tracking (v7.2, analysis only).

Strict safety invariant: analysis only. Nothing here moves money or talks to a broker,
exchange or trading API; it stores what a forecast said and later compares it with
what actually happened (master reference, phase 12: forecasts are timestamped,
evaluated out of sample, uncertainty-aware and never presented as guaranteed).

Design:
* A snapshot is a recomputation of the live forecast (``build_forecast``, the same code and
  the same single bounded read as GET /finance/forecast) at save time, frozen with its
  ``created_at`` and ``generated_at`` timestamps and its notes: per series the chosen method,
  per target month the point, band and band basis, the naive value known at save time, the
  model version and a fingerprint of the history used. Snapshots are immutable.
* A target is evaluable once its month is complete: strictly before the current UTC month.
  Actuals come from the owner's CURRENT transactions with the same series rules as the
  forecast (``build_monthly_series``); transactions added, removed, moved or imported after
  the snapshot change them, which is reported per snapshot as ``data_changed_since_snapshot``.
* The naive baseline is the one the forecast could have used: the last complete month's value
  as seen at save time (stored per target), scored on the same targets.
* Repeats are collapsed. Every snapshot in one as_of month with the same settings, model
  version and history fingerprint is the same forecast, so daily snapshots would otherwise
  count one forecast ~30 times. Only the earliest such snapshot is scored; a changed history
  within the month gives a distinct forecast and is kept (and said so).
* The drift check looks at the last DRIFT_WINDOW distinct target months, each scored once at
  its shortest stored horizon.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import math
from typing import Sequence
from uuid import UUID

from jarvis.finance.forecasting import (
    DISCLAIMER,
    FORECAST_MODEL_VERSION,
    INTERVAL_LEVEL,
    Month,
    _month_of,
    _shift,
    _utc,
    build_monthly_series,
    month_label,
)
from jarvis.finance.models import (
    FinanceForecast,
    FinancialTransaction,
    ForecastAccuracyGroup,
    ForecastAccuracyReport,
    ForecastSnapshot,
    ForecastSnapshotEvaluation,
    ForecastSnapshotSummary,
    ForecastSnapshotTarget,
)

SERIES = ("net", "income", "spending")
DRIFT_WINDOW = 6  # distinct target months per series looked at by the drift check
DRIFT_MIN_COVERAGE = 0.5
COVERAGE_TOLERANCE = 1e-9
NET_MAPE_NOTE = ("Not reported for net: net cash flow can be near zero, which makes percentage errors explode; "
                 "use MAE and bias.")


class ForecastSnapshotExists(Exception):
    """A snapshot was already taken today (UTC) without force, or the daily maximum was reached."""

    def __init__(self, existing_id: UUID, limit_reached: bool = False) -> None:
        super().__init__("A forecast snapshot was already taken today (UTC).")
        self.existing_id = existing_id
        self.limit_reached = limit_reached


# ============================================================= Months


def month_start(month: Month) -> date:
    return date(month[0], month[1], 1)


def parse_month(label: str) -> Month:
    year, month = label.split("-")
    return (int(year), int(month))


def month_completes_at(month: Month) -> datetime:
    """The UTC instant a month is complete (the first moment of the next month)."""
    following = _shift(month, 1)
    return datetime(following[0], following[1], 1, tzinfo=timezone.utc)


# ============================================================= History fingerprint


def _cents(amount: float) -> int:
    return int(round(amount * 100))


EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class HistoryFingerprint:
    count: int
    latest: datetime | None
    net_cents: int
    digest: str = EMPTY_DIGEST  # sha256 over sorted "YYYY-MM:cents:count" lines, one per month with transactions

    @property
    def net_total(self) -> float:
        return self.net_cents / 100


class HistoryIndex:
    """Transactions sorted by date, with per-month aggregates, so the fingerprint before any month start is a
    binary search (the digest is precomputed for every month prefix)."""

    def __init__(self, transactions: Sequence[FinancialTransaction]) -> None:
        finite = sorted((_utc(t.transaction_date), _cents(t.amount)) for t in transactions if math.isfinite(t.amount))
        self._dates = [d for d, _ in finite]
        self._prefix = [0]
        monthly: dict[Month, list[int]] = {}
        for moment, cents in finite:
            self._prefix.append(self._prefix[-1] + cents)
            totals = monthly.setdefault((moment.year, moment.month), [0, 0])
            totals[0] += cents
            totals[1] += 1
        self._month_starts: list[datetime] = []
        self._month_digests: list[str] = []
        hasher = hashlib.sha256()
        for month in sorted(monthly):
            cents, count = monthly[month]
            hasher.update(f"{month_label(month)}:{cents}:{count}\n".encode("ascii"))
            self._month_starts.append(datetime(month[0], month[1], 1, tzinfo=timezone.utc))
            self._month_digests.append(hasher.copy().hexdigest())

    def before(self, cutoff: datetime) -> HistoryFingerprint:
        """Fingerprint of transactions dated before ``cutoff``, which must be the start of a UTC month."""
        position = bisect_left(self._dates, cutoff)
        months = bisect_left(self._month_starts, cutoff)
        return HistoryFingerprint(position, self._dates[position - 1] if position else None, self._prefix[position],
                                  self._month_digests[months - 1] if months else EMPTY_DIGEST)


def history_cutoff(as_of: datetime) -> datetime:
    """History of a forecast made at ``as_of``: transactions dated before the start of the as_of month."""
    month = _month_of(as_of)
    return datetime(month[0], month[1], 1, tzinfo=timezone.utc)


def history_fingerprint(transactions: Sequence[FinancialTransaction], as_of: datetime) -> HistoryFingerprint:
    return HistoryIndex(transactions).before(history_cutoff(as_of))


def same_history(snapshot: ForecastSnapshotSummary, fingerprint: HistoryFingerprint) -> bool:
    stored = snapshot.history_latest_transaction_date
    return (fingerprint.count == snapshot.history_transaction_count
            and fingerprint.net_cents == _cents(snapshot.history_net_total)
            and fingerprint.digest == snapshot.history_digest
            and (fingerprint.latest is None) == (stored is None)
            and (stored is None or fingerprint.latest == _utc(stored)))


# ============================================================= Snapshot from a forecast


def snapshot_from_forecast(
    forecast: FinanceForecast,
    *,
    snapshot_id: UUID,
    owner_id: UUID,
    created_at: datetime,
    fingerprint: HistoryFingerprint,
) -> ForecastSnapshot:
    targets: list[ForecastSnapshotTarget] = []
    last = forecast.data.history[-1] if forecast.data.history else None
    if forecast.status == "ok":
        for name in SERIES:
            series = getattr(forecast, name)
            if series is None:
                continue
            naive = getattr(last, name) if last is not None else None
            for point in series.points:
                targets.append(ForecastSnapshotTarget(
                    series=name, target_month=point.month, horizon=point.horizon, method=series.chosen_method,
                    point=point.point, lower=point.lower, upper=point.upper, interval_basis=point.interval_basis,
                    naive_point=naive,
                ))
    return ForecastSnapshot(
        id=snapshot_id, owner_id=owner_id, created_at=_utc(created_at), generated_at=forecast.generated_at,
        as_of=forecast.as_of, horizon=forecast.horizon, min_train=forecast.min_train, status=forecast.status,
        model_version=FORECAST_MODEL_VERSION, months_available=forecast.data.months_available,
        transactions_considered=forecast.data.transactions_considered,
        history_transaction_count=fingerprint.count, history_latest_transaction_date=fingerprint.latest,
        history_net_total=fingerprint.net_total, history_digest=fingerprint.digest, truncated=forecast.data.truncated,
        accounts_truncated=forecast.data.accounts_truncated, target_count=len(targets), targets=targets,
        notes=list(forecast.notes), disclaimer=DISCLAIMER,
    )


# ============================================================= Cap eviction


def choose_evictions(
    rows: Sequence[tuple[UUID, datetime, datetime]], new_id: UUID, cap: int
) -> list[UUID]:
    """Snapshots to delete so at most ``cap`` remain; ``rows`` are (id, created_at, as_of) including the new one.

    The new snapshot always stays. Repeats within an as_of month go first (newest repeat first), so the earliest
    snapshot of a month (the one accuracy scores) is never removed while any repeat exists; only when every
    snapshot is alone in its month are the oldest removed.
    """
    excess = len(rows) - max(cap, 1)
    if excess <= 0:
        return []
    keepers: dict[Month, tuple[datetime, UUID]] = {}
    for snapshot_id, created_at, as_of in rows:
        month = _month_of(as_of)
        key = (_utc(created_at), snapshot_id)
        if month not in keepers or key < keepers[month]:
            keepers[month] = key
    keeper_ids = {snapshot_id for _, snapshot_id in keepers.values()}
    candidates = [(_utc(created_at), snapshot_id) for snapshot_id, created_at, _ in rows if snapshot_id != new_id]
    repeats = sorted((c for c in candidates if c[1] not in keeper_ids), reverse=True)
    victims = [snapshot_id for _, snapshot_id in repeats[:excess]]
    if len(victims) < excess:
        singles = sorted(c for c in candidates if c[1] in keeper_ids)
        victims += [snapshot_id for _, snapshot_id in singles[: excess - len(victims)]]
    return victims


# ============================================================= Accuracy


@dataclass
class ForecastTargetRows:
    """What the store returns for an evaluation: completed targets with their snapshot, and what is pending."""

    evaluable: list[tuple[ForecastSnapshotSummary, ForecastSnapshotTarget]] = field(default_factory=list)
    pending_by_snapshot: dict[UUID, int] = field(default_factory=dict)
    earliest_pending: date | None = None


@dataclass(frozen=True)
class Evaluation:
    snapshot: ForecastSnapshotSummary
    target: ForecastSnapshotTarget
    actual: float
    naive: float

    @property
    def error(self) -> float:
        return self.actual - self.target.point

    @property
    def naive_error(self) -> float:
        return self.actual - self.naive

    @property
    def has_band(self) -> bool:
        return self.target.lower is not None and self.target.upper is not None

    @property
    def covered(self) -> bool:
        assert self.target.lower is not None and self.target.upper is not None
        return (self.target.lower - COVERAGE_TOLERANCE <= self.actual
                <= self.target.upper + COVERAGE_TOLERANCE)

    @property
    def order(self) -> tuple[datetime, UUID]:
        return (_utc(self.snapshot.created_at), self.snapshot.id)

    @property
    def forecast_key(self) -> tuple:
        """Identical forecasts share this key: same target, as_of month, settings, model version and history."""
        s = self.snapshot
        latest = _utc(s.history_latest_transaction_date) if s.history_latest_transaction_date else None
        return (self.target.series, self.target.target_month, self.target.horizon, _month_of(s.as_of), s.horizon,
                s.min_train, s.model_version, s.history_digest, s.history_transaction_count, latest)


def collapse_repeats(evaluations: Sequence[Evaluation]) -> list[Evaluation]:
    """One evaluation per distinct forecast: the earliest snapshot of each forecast_key."""
    kept: dict[tuple, Evaluation] = {}
    for evaluation in evaluations:
        key = evaluation.forecast_key
        if key not in kept or evaluation.order < kept[key].order:
            kept[key] = evaluation
    return sorted(kept.values(), key=lambda e: (e.target.series, e.target.target_month, e.target.horizon, e.order))


def _group(
    series: str, evaluations: Sequence[Evaluation], raw: int, *, horizon: int | None = None,
    method: str | None = None,
) -> ForecastAccuracyGroup:
    n = len(evaluations)
    mae = sum(abs(e.error) for e in evaluations) / n
    bias = sum(e.error for e in evaluations) / n
    naive_mae = sum(abs(e.naive_error) for e in evaluations) / n
    naive_bias = sum(e.naive_error for e in evaluations) / n
    mape: float | None = None
    mape_note: str | None = None
    if series == "net":
        mape_note = NET_MAPE_NOTE
    elif any(e.actual == 0 for e in evaluations):
        mape_note = "MAPE is undefined because at least one actual value is zero."
    else:
        mape = round(100 * sum(abs(e.error) / abs(e.actual) for e in evaluations) / n, 2)
    banded = [e for e in evaluations if e.has_band]
    coverage = round(sum(1 for e in banded if e.covered) / len(banded), 3) if banded else None
    beats = mae < naive_mae - 1e-9
    verdict = "beat" if beats else "did not beat"
    comparison = (f"The stored forecasts {verdict} the naive baseline (last complete month known at save time) on "
                  f"these {n} distinct forecast(s): MAE {mae:,.2f} vs {naive_mae:,.2f}.")
    return ForecastAccuracyGroup(
        series=series, horizon=horizon, method=method, evaluated=n, raw_evaluations=raw, mae=round(mae, 2),
        mean_error=round(bias, 2), mape=mape, mape_note=mape_note, interval_level=INTERVAL_LEVEL,
        interval_coverage=coverage, coverage_targets=len(banded), naive_mae=round(naive_mae, 2),
        naive_mean_error=round(naive_bias, 2), beats_naive=beats, comparison=comparison,
    )


def drift_window(evaluations: Sequence[Evaluation], series: str) -> list[Evaluation]:
    """The last DRIFT_WINDOW distinct target months of a series, each once: at its shortest stored horizon, from
    the earliest snapshot that made it."""
    by_month: dict[str, Evaluation] = {}
    for e in evaluations:
        if e.target.series != series:
            continue
        current = by_month.get(e.target.target_month)
        if current is None or (e.target.horizon, e.order) < (current.target.horizon, current.order):
            by_month[e.target.target_month] = e
    return [by_month[month] for month in sorted(by_month)][-DRIFT_WINDOW:]


def drift_warnings(evaluations: Sequence[Evaluation]) -> list[str]:
    """Plain-language warnings from the last DRIFT_WINDOW distinct target months of each series."""
    warnings: list[str] = []
    counted = "each scored once, at its shortest stored horizon"
    for name in SERIES:
        recent = drift_window(evaluations, name)
        if len(recent) < DRIFT_WINDOW:
            continue
        banded = [e for e in recent if e.has_band]
        if banded:
            inside = sum(1 for e in banded if e.covered)
            if inside / len(banded) < DRIFT_MIN_COVERAGE:
                warnings.append(
                    f"{name}: only {inside} of the last {len(banded)} distinct target months ({counted}) fell "
                    f"inside the {INTERVAL_LEVEL:.0%} band. The bands look too narrow for recent data, or the "
                    "pattern has changed; treat current forecasts for this series with extra caution.")
        if all(e.error > 0 for e in recent):
            warnings.append(
                f"{name}: actuals were above the forecast in each of the last {DRIFT_WINDOW} distinct target "
                f"months ({counted}): consistent under-forecasting. The pattern may have shifted.")
        elif all(e.error < 0 for e in recent):
            warnings.append(
                f"{name}: actuals were below the forecast in each of the last {DRIFT_WINDOW} distinct target "
                f"months ({counted}): consistent over-forecasting. The pattern may have shifted.")
    return warnings


def evaluate_accuracy(
    rows: ForecastTargetRows,
    transactions: Sequence[FinancialTransaction],
    *,
    truncated: bool,
    now: datetime,
) -> ForecastAccuracyReport:
    now = _utc(now)
    current = _month_of(now)
    notes = [
        "Only target months that are complete (strictly before the current UTC month) are evaluated.",
        "Actuals are computed from your current transactions with the same rules as the forecast, so "
        "transactions added, removed, moved or imported after a snapshot change them; such snapshots are flagged "
        "data_changed_since_snapshot.",
        "The naive baseline is the last complete month's value as known when each snapshot was saved, scored on "
        "the same targets.",
        "Snapshots in the same as_of month with the same settings, model version and history are the same "
        "forecast; only the earliest is scored, so counts are distinct forecasts (raw_evaluations counts all).",
        "Different horizons of one target month are distinct forecasts and are each scored; the drift check uses "
        "each target month once, at its shortest stored horizon.",
        "MAPE is reported for income and spending only; net can be near zero, so use its MAE and bias.",
    ]
    if truncated:
        notes.append("Only the most recent transactions were read; the earliest (possibly incomplete) month was "
                     "dropped, its targets are reported without actuals, and data changes cannot be checked.")

    pending_total = sum(rows.pending_by_snapshot.values())
    first_pending = rows.earliest_pending
    first_pending_label = month_label((first_pending.year, first_pending.month)) if first_pending else None
    completes_at = month_completes_at((first_pending.year, first_pending.month)) if first_pending else None
    common = dict(generated_at=now, current_month=month_label(current), disclaimer=DISCLAIMER,
                  model_version=FORECAST_MODEL_VERSION, interval_level=INTERVAL_LEVEL,
                  first_pending_month=first_pending_label, first_target_completes_at=completes_at,
                  data_truncated=truncated, targets_pending=pending_total)

    completed = [(s, t) for s, t in rows.evaluable
                 if s.status == "ok" and parse_month(t.target_month) < current]
    if not completed:
        if first_pending is None:
            notes.append("No stored forecast has any target months yet. Create a snapshot with "
                         "POST /finance/forecast/snapshots; accuracy can be measured once its months are complete.")
        else:
            notes.append(f"The first stored target month ({first_pending_label}) completes at "
                         f"{completes_at.isoformat()}.")
        return ForecastAccuracyReport(status="nothing_to_evaluate_yet", snapshots_considered=0, targets_evaluated=0,
                                      targets_without_actuals=0, notes=notes, **common)

    series = build_monthly_series(transactions, now, truncated=truncated)
    values = {name: dict(zip(series.months, getattr(series, name))) for name in SERIES}
    index = None if truncated else HistoryIndex(transactions)

    raw: list[Evaluation] = []
    per_snapshot: dict[UUID, dict[str, int]] = {}
    headers: dict[UUID, ForecastSnapshotSummary] = {}
    naive_from_current = 0
    for snapshot, target in completed:
        headers[snapshot.id] = snapshot
        counts = per_snapshot.setdefault(snapshot.id, {"evaluated": 0, "missing": 0, "counted": 0})
        actual = values[target.series].get(parse_month(target.target_month))
        naive = target.naive_point
        if naive is None:  # not expected for 'ok' snapshots; fall back to current data and say so
            naive = values[target.series].get(_shift(_month_of(snapshot.as_of), -1))
            if naive is not None:
                naive_from_current += 1
        if actual is None or naive is None:
            counts["missing"] += 1
            continue
        counts["evaluated"] += 1
        raw.append(Evaluation(snapshot, target, actual, naive))
    evaluations = collapse_repeats(raw)
    for e in evaluations:
        per_snapshot[e.snapshot.id]["counted"] += 1
    if naive_from_current:
        notes.append(f"{naive_from_current} target(s) had no stored naive value; their naive baseline was read "
                     "from current data.")

    snapshot_reports: list[ForecastSnapshotEvaluation] = []
    for snapshot_id, snapshot in sorted(headers.items(), key=lambda item: (_utc(item[1].created_at), item[0])):
        changed: bool | None = None
        if index is not None and not snapshot.truncated:
            changed = not same_history(snapshot, index.before(history_cutoff(snapshot.as_of)))
        counts = per_snapshot[snapshot_id]
        snapshot_reports.append(ForecastSnapshotEvaluation(
            snapshot_id=snapshot_id, created_at=snapshot.created_at, as_of=snapshot.as_of,
            model_version=snapshot.model_version, targets_evaluated=counts["evaluated"],
            targets_counted=counts["counted"], targets_pending=rows.pending_by_snapshot.get(snapshot_id, 0),
            targets_without_actuals=counts["missing"], data_changed_since_snapshot=changed,
        ))
    if any(r.data_changed_since_snapshot for r in snapshot_reports):
        notes.append("At least one snapshot's history changed after it was taken (data_changed_since_snapshot); "
                     "its errors partly reflect data changes, not only forecast skill.")
    collapsed = len(raw) - len(evaluations)
    if collapsed:
        notes.append(f"{collapsed} repeated evaluation(s) of identical forecasts were collapsed into the earliest "
                     "snapshot of their as_of month.")
    variants: dict[tuple, set[tuple]] = {}
    for e in evaluations:
        key = e.forecast_key
        variants.setdefault(key[:7], set()).add(key[7:])
    changed_within = len({key[3] for key, kinds in variants.items() if len(kinds) > 1})
    if changed_within:
        notes.append(f"In {changed_within} as_of month(s) the history changed between snapshots, so more than one "
                     "distinct forecast of the same month was kept.")

    missing = sum(c["missing"] for c in per_snapshot.values())
    if not evaluations:
        notes.append("Completed target months exist, but no actual monthly totals are available for them in the "
                     "current data (for example after transactions were deleted).")
        return ForecastAccuracyReport(status="no_actuals_available", snapshots_considered=len(headers),
                                      targets_evaluated=0, targets_without_actuals=missing,
                                      snapshots=snapshot_reports, notes=notes, **common)

    overall: list[ForecastAccuracyGroup] = []
    by_horizon: list[ForecastAccuracyGroup] = []
    by_method: list[ForecastAccuracyGroup] = []
    for name in SERIES:
        mine = [e for e in evaluations if e.target.series == name]
        mine_raw = [e for e in raw if e.target.series == name]
        if not mine:
            continue
        overall.append(_group(name, mine, len(mine_raw)))
        for h in sorted({e.target.horizon for e in mine}):
            by_horizon.append(_group(name, [e for e in mine if e.target.horizon == h],
                                     sum(1 for e in mine_raw if e.target.horizon == h), horizon=h))
        for method in sorted({e.target.method for e in mine}):
            by_method.append(_group(name, [e for e in mine if e.target.method == method],
                                    sum(1 for e in mine_raw if e.target.method == method), method=method))
    if {s.model_version for s in headers.values()} != {FORECAST_MODEL_VERSION}:
        notes.append("Some snapshots were made with a different model version; see each snapshot's model_version.")
    return ForecastAccuracyReport(
        status="ok", snapshots_considered=len(headers), targets_evaluated=len(evaluations),
        raw_evaluations=len(raw), duplicate_evaluations_collapsed=collapsed, targets_without_actuals=missing,
        overall=overall, by_horizon=by_horizon, by_method=by_method, snapshots=snapshot_reports,
        warnings=drift_warnings(evaluations), notes=notes, **common,
    )
