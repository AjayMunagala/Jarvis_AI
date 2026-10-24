"""Forecast snapshots and out-of-sample accuracy tracking (v7.2, analysis only).

Strict safety invariant: analysis only. Nothing here moves money or talks to a broker,
exchange or trading API; it stores what a forecast said and later compares it with
what actually happened (master reference, phase 12: forecasts are timestamped,
evaluated out of sample, uncertainty-aware and never presented as guaranteed).

Design:
* A snapshot is the live forecast (``build_forecast``) frozen at ``created_at``: per
  series the chosen method and per target month the point, band and band basis, plus
  the model version and a fingerprint of the history it used. Snapshots are immutable.
* A target is evaluable once its month is complete: strictly before the current UTC
  month. Actuals come from the owner's CURRENT transactions with the same series rules
  as the forecast (``build_monthly_series``), so transactions added, removed or imported
  after the snapshot change the actuals; that is reported per snapshot as
  ``data_changed_since_snapshot`` (history fingerprint differs).
* The naive baseline for a target is the value of the last complete month before the
  snapshot's ``as_of`` month, also read from current data, evaluated on the same targets.
* Overlapping snapshots score the same month several times (once per horizon); the
  counts are target evaluations, not independent observations.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
DRIFT_WINDOW = 6  # last evaluated targets per series looked at by the drift check
DRIFT_MIN_COVERAGE = 0.5
COVERAGE_TOLERANCE = 1e-9


class ForecastSnapshotExists(Exception):
    """A snapshot was already taken today (UTC) and force was not set."""

    def __init__(self, existing_id: UUID) -> None:
        super().__init__("A forecast snapshot was already taken today (UTC).")
        self.existing_id = existing_id


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


@dataclass(frozen=True)
class HistoryFingerprint:
    count: int
    latest: datetime | None
    net_cents: int

    @property
    def net_total(self) -> float:
        return self.net_cents / 100


class HistoryIndex:
    """Transactions sorted by date so the fingerprint before any cutoff is a binary search."""

    def __init__(self, transactions: Sequence[FinancialTransaction]) -> None:
        finite = sorted((_utc(t.transaction_date), _cents(t.amount)) for t in transactions if math.isfinite(t.amount))
        self._dates = [d for d, _ in finite]
        self._prefix = [0]
        for _, cents in finite:
            self._prefix.append(self._prefix[-1] + cents)

    def before(self, cutoff: datetime) -> HistoryFingerprint:
        position = bisect_left(self._dates, cutoff)
        return HistoryFingerprint(position, self._dates[position - 1] if position else None, self._prefix[position])


def history_cutoff(as_of: datetime) -> datetime:
    """History of a forecast made at ``as_of``: transactions dated before the start of the as_of month."""
    month = _month_of(as_of)
    return datetime(month[0], month[1], 1, tzinfo=timezone.utc)


def history_fingerprint(transactions: Sequence[FinancialTransaction], as_of: datetime) -> HistoryFingerprint:
    return HistoryIndex(transactions).before(history_cutoff(as_of))


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
    if forecast.status == "ok":
        for name in SERIES:
            series = getattr(forecast, name)
            if series is None:
                continue
            for point in series.points:
                targets.append(ForecastSnapshotTarget(
                    series=name, target_month=point.month, horizon=point.horizon, method=series.chosen_method,
                    point=point.point, lower=point.lower, upper=point.upper, interval_basis=point.interval_basis,
                ))
    return ForecastSnapshot(
        id=snapshot_id, owner_id=owner_id, created_at=_utc(created_at), as_of=forecast.as_of,
        horizon=forecast.horizon, min_train=forecast.min_train, status=forecast.status,
        model_version=FORECAST_MODEL_VERSION, months_available=forecast.data.months_available,
        transactions_considered=forecast.data.transactions_considered,
        history_transaction_count=fingerprint.count, history_latest_transaction_date=fingerprint.latest,
        history_net_total=fingerprint.net_total, truncated=forecast.data.truncated,
        accounts_truncated=forecast.data.accounts_truncated, target_count=len(targets), targets=targets,
        disclaimer=DISCLAIMER,
    )


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
    def sort_key(self) -> tuple[str, datetime, datetime, int]:
        return (self.target.target_month, self.snapshot.as_of, self.snapshot.created_at, self.target.horizon)


def _group(
    series: str, evaluations: Sequence[Evaluation], *, horizon: int | None = None, method: str | None = None
) -> ForecastAccuracyGroup:
    n = len(evaluations)
    mae = sum(abs(e.error) for e in evaluations) / n
    bias = sum(e.error for e in evaluations) / n
    naive_mae = sum(abs(e.naive_error) for e in evaluations) / n
    naive_bias = sum(e.naive_error for e in evaluations) / n
    mape: float | None = None
    mape_note: str | None = None
    if any(e.actual == 0 for e in evaluations):
        mape_note = "MAPE is undefined because at least one actual value is zero."
    else:
        mape = round(100 * sum(abs(e.error) / abs(e.actual) for e in evaluations) / n, 2)
    banded = [e for e in evaluations if e.has_band]
    coverage = round(sum(1 for e in banded if e.covered) / len(banded), 3) if banded else None
    beats = mae < naive_mae - 1e-9
    if beats:
        comparison = (f"The stored forecasts beat the naive baseline (last complete month before the snapshot) "
                      f"on these {n} target(s): MAE {mae:,.2f} vs {naive_mae:,.2f}.")
    else:
        comparison = (f"The stored forecasts did not beat the naive baseline (last complete month before the "
                      f"snapshot) on these {n} target(s): MAE {mae:,.2f} vs {naive_mae:,.2f}.")
    return ForecastAccuracyGroup(
        series=series, horizon=horizon, method=method, evaluated=n, mae=round(mae, 2), mean_error=round(bias, 2),
        mape=mape, mape_note=mape_note, interval_level=INTERVAL_LEVEL, interval_coverage=coverage,
        coverage_targets=len(banded), naive_mae=round(naive_mae, 2), naive_mean_error=round(naive_bias, 2),
        beats_naive=beats, comparison=comparison,
    )


def drift_warnings(evaluations: Sequence[Evaluation]) -> list[str]:
    """Plain-language warnings from the last DRIFT_WINDOW evaluated targets of each series."""
    warnings: list[str] = []
    for name in SERIES:
        recent = sorted((e for e in evaluations if e.target.series == name), key=lambda e: e.sort_key)[-DRIFT_WINDOW:]
        if len(recent) < DRIFT_WINDOW:
            continue
        banded = [e for e in recent if e.has_band]
        if banded:
            inside = sum(1 for e in banded if e.covered)
            if inside / len(banded) < DRIFT_MIN_COVERAGE:
                warnings.append(
                    f"{name}: only {inside} of the last {len(banded)} evaluated months fell inside the "
                    f"{INTERVAL_LEVEL:.0%} band. The bands look too narrow for recent data, or the pattern has "
                    "changed; treat current forecasts for this series with extra caution.")
        if all(e.error > 0 for e in recent):
            warnings.append(
                f"{name}: actuals were above the forecast in each of the last {DRIFT_WINDOW} evaluated months "
                "(consistent under-forecasting). The pattern may have shifted.")
        elif all(e.error < 0 for e in recent):
            warnings.append(
                f"{name}: actuals were below the forecast in each of the last {DRIFT_WINDOW} evaluated months "
                "(consistent over-forecasting). The pattern may have shifted.")
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
        "transactions added, removed or imported after a snapshot change them; such snapshots are flagged "
        "data_changed_since_snapshot.",
        "The naive baseline is the last complete month before each snapshot's as_of month (from current data), "
        "scored on the same targets.",
        "Overlapping snapshots score the same month once per horizon, so the counts are evaluations, not "
        "independent observations.",
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

    evaluations: list[Evaluation] = []
    per_snapshot: dict[UUID, dict[str, int]] = {}
    headers: dict[UUID, ForecastSnapshotSummary] = {}
    for snapshot, target in completed:
        headers[snapshot.id] = snapshot
        counts = per_snapshot.setdefault(snapshot.id, {"evaluated": 0, "missing": 0})
        actual = values[target.series].get(parse_month(target.target_month))
        naive = values[target.series].get(_shift(_month_of(snapshot.as_of), -1))
        if actual is None or naive is None:
            counts["missing"] += 1
            continue
        counts["evaluated"] += 1
        evaluations.append(Evaluation(snapshot, target, actual, naive))

    snapshot_reports: list[ForecastSnapshotEvaluation] = []
    for snapshot_id, snapshot in sorted(headers.items(), key=lambda item: (item[1].created_at, item[0])):
        changed: bool | None = None
        if index is not None and not snapshot.truncated:
            now_print = index.before(history_cutoff(snapshot.as_of))
            stored_latest = snapshot.history_latest_transaction_date
            changed = (now_print.count != snapshot.history_transaction_count
                       or now_print.net_cents != _cents(snapshot.history_net_total)
                       or (now_print.latest is None) != (stored_latest is None)
                       or (stored_latest is not None and now_print.latest != _utc(stored_latest)))
        counts = per_snapshot[snapshot_id]
        snapshot_reports.append(ForecastSnapshotEvaluation(
            snapshot_id=snapshot_id, created_at=snapshot.created_at, as_of=snapshot.as_of,
            model_version=snapshot.model_version, targets_evaluated=counts["evaluated"],
            targets_pending=rows.pending_by_snapshot.get(snapshot_id, 0), targets_without_actuals=counts["missing"],
            data_changed_since_snapshot=changed,
        ))
    if any(r.data_changed_since_snapshot for r in snapshot_reports):
        notes.append("At least one snapshot's history changed after it was taken (data_changed_since_snapshot); "
                     "its errors partly reflect data changes, not only forecast skill.")

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
        if not mine:
            continue
        overall.append(_group(name, mine))
        for h in sorted({e.target.horizon for e in mine}):
            by_horizon.append(_group(name, [e for e in mine if e.target.horizon == h], horizon=h))
        for method in sorted({e.target.method for e in mine}):
            by_method.append(_group(name, [e for e in mine if e.target.method == method], method=method))
    if {s.model_version for s in headers.values()} != {FORECAST_MODEL_VERSION}:
        notes.append("Some snapshots were made with a different model version; see each snapshot's model_version.")
    return ForecastAccuracyReport(
        status="ok", snapshots_considered=len(headers), targets_evaluated=len(evaluations),
        targets_without_actuals=missing, overall=overall, by_horizon=by_horizon, by_method=by_method,
        snapshots=snapshot_reports, warnings=drift_warnings(evaluations), notes=notes, **common,
    )
