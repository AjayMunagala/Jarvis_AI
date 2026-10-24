"""Monthly cash-flow forecasts with out-of-sample backtesting and uncertainty (v6.6).

Strict safety invariant: analysis only. This module reads stored transactions and
returns numbers; it contains ZERO broker, exchange, order or trade APIs and never
moves money. Forecasts are evaluated out of sample and never presented as
guaranteed (master reference, phase 12 gate).

Design:
* History is the calendar-month (UTC) series of complete months, ending with the
  month before ``as_of``. The in-progress month is excluded from history because a
  partial month would look like a sudden drop; horizon 1 is the current month.
* Methods are deliberately simple and deterministic: naive, trailing means (3, 6),
  seasonal naive (12) and simple exponential smoothing with alpha chosen from a
  fixed grid using only the training window of each fold.
* Rolling-origin backtest: at origin ``t`` a method sees ``y[:t]`` only and is
  scored on ``y[t + h - 1]`` for ``h = 1..horizon``. All candidate methods are
  scored on the same origins.
* Prediction intervals are empirical quantiles of out-of-sample errors per
  horizon, with a sqrt(h)-scaled fallback when a horizon has too few errors (the
  cumulative balance band falls back with an h-scaling instead). The reported
  coverage is itself out of sample: the interval used at origin ``t`` is built
  only from errors whose actual month was observed before ``t``.
* A band is never narrower than a documented minimum (MIN_BAND_FRACTION of the
  mean absolute monthly level, at least MIN_BAND_FLOOR), so a perfectly regular
  history does not yield a zero-width "80%" band; coverage is then reported as
  not meaningful rather than as 1.0.
* A forecast is only returned ("ok") when the shared backtest has at least
  MIN_COMMON_ORIGINS origins and MIN_POOLED_ERRORS out-of-sample errors, so every
  horizon point carries a band. Otherwise the status is "insufficient_history".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Callable, Sequence

from jarvis.finance.models import (
    BalancePoint,
    BalanceScenario,
    FinanceForecast,
    FinancialAccount,
    FinancialTransaction,
    ForecastDataSummary,
    ForecastPoint,
    MethodBacktest,
    MonthlyFlow,
    SeriesForecast,
)

DISCLAIMER = (
    "Analysis only - not financial advice. These figures are statistical estimates from your "
    "recorded transactions; past patterns may not continue and nothing here is guaranteed. "
    "JARVIS does not execute any transactions or trades."
)

INTERVAL_LEVEL = 0.8
LOWER_Q = 0.1
UPPER_Q = 0.9
MIN_HORIZON_ERRORS = 5  # per-horizon empirical quantiles need at least this many errors
MIN_POOLED_ERRORS = 8  # out-of-sample errors needed before any band (and any forecast) is reported
MIN_COMMON_ORIGINS = 3  # an optional method may not shrink the shared backtest below this
MAX_HISTORY_MONTHS = 120
MIN_TRAIN_FLOOR = 3
MIN_BAND_FRACTION = 0.05  # minimum half-width: 5% of the mean absolute monthly level ...
MIN_BAND_FLOOR = 1.0  # ... and never below one currency unit (scaled by sqrt(h), or h when cumulative)
SES_ALPHAS = (0.2, 0.5, 0.8)
TIE_TOLERANCE = 1e-9

Month = tuple[int, int]


# ============================================================= Series building


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _month_of(dt: datetime) -> Month:
    u = _utc(dt)
    return (u.year, u.month)


def _shift(month: Month, delta: int) -> Month:
    index = month[0] * 12 + (month[1] - 1) + delta
    return (index // 12, index % 12 + 1)


def _month_index(month: Month) -> int:
    return month[0] * 12 + (month[1] - 1)


def month_label(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


@dataclass(frozen=True)
class MonthlySeries:
    months: list[Month]
    income: list[float]
    spending: list[float]  # outflows as positive magnitudes
    net: list[float]
    transactions_considered: int
    excluded_current_month: int
    excluded_future_dated: int
    excluded_older_than_window: int
    dropped_partial_first_month: bool
    current_month_net_to_date: float
    excluded_invalid: int = 0
    future_dated_net: float = 0.0


def build_monthly_series(
    transactions: Sequence[FinancialTransaction],
    as_of: datetime,
    *,
    truncated: bool = False,
    max_months: int = MAX_HISTORY_MONTHS,
) -> MonthlySeries:
    """Complete calendar months (UTC) up to the month before ``as_of``; empty months are zero.

    When the transaction read was truncated (newest first), the earliest month present
    may be incomplete, so it is dropped rather than reported as a low month.
    """
    as_of = _utc(as_of)
    current = _month_of(as_of)
    last_complete = _shift(current, -1)

    past: list[FinancialTransaction] = []
    excluded_current = 0
    excluded_future = 0
    excluded_invalid = 0
    current_net = 0.0
    future_net = 0.0
    for tx in transactions:
        if not math.isfinite(tx.amount):
            excluded_invalid += 1  # older rows written before v6.6 validation; never let them corrupt output
            continue
        tx_date = _utc(tx.transaction_date)
        if tx_date > as_of:
            # Already applied to today's balance by record_transaction; kept out of history.
            excluded_future += 1
            future_net += tx.amount
            continue
        if _month_of(tx_date) == current:
            excluded_current += 1
            current_net += tx.amount
            continue
        past.append(tx)

    dropped_partial = False
    if truncated and past:
        earliest = min(_month_of(t.transaction_date) for t in past)
        past = [t for t in past if _month_of(t.transaction_date) != earliest]
        dropped_partial = True

    if not past:
        return MonthlySeries([], [], [], [], 0, excluded_current, excluded_future, 0, dropped_partial,
                             round(current_net, 2), excluded_invalid, round(future_net, 2))

    first = min(_month_of(t.transaction_date) for t in past)
    window_start = _shift(last_complete, -(max_months - 1))
    excluded_older = 0
    if _month_index(first) < _month_index(window_start):
        first = window_start
    count = _month_index(last_complete) - _month_index(first) + 1
    months = [_shift(first, i) for i in range(count)]
    income = [0.0] * count
    spending = [0.0] * count
    used = 0
    for tx in past:
        position = _month_index(_month_of(tx.transaction_date)) - _month_index(first)
        if position < 0:
            excluded_older += 1
            continue
        used += 1
        if tx.amount >= 0:
            income[position] += tx.amount
        else:
            spending[position] += -tx.amount
    income = [round(v, 2) for v in income]
    spending = [round(v, 2) for v in spending]
    net = [round(i - s, 2) for i, s in zip(income, spending)]
    return MonthlySeries(months, income, spending, net, used, excluded_current, excluded_future,
                         excluded_older, dropped_partial, round(current_net, 2), excluded_invalid,
                         round(future_net, 2))


# ============================================================= Forecast methods


@dataclass(frozen=True)
class Method:
    name: str
    min_history: int
    simplicity: int  # lower is simpler; breaks MAE ties
    fn: Callable[[Sequence[float], int], tuple[list[float], dict[str, float]]]


def naive(history: Sequence[float], horizon: int) -> tuple[list[float], dict[str, float]]:
    return [float(history[-1])] * horizon, {}


def trailing_mean(window: int) -> Callable[[Sequence[float], int], tuple[list[float], dict[str, float]]]:
    def forecast(history: Sequence[float], horizon: int) -> tuple[list[float], dict[str, float]]:
        recent = history[-window:]
        return [sum(recent) / len(recent)] * horizon, {"window": float(window)}

    return forecast


def seasonal_naive(history: Sequence[float], horizon: int) -> tuple[list[float], dict[str, float]]:
    """Same month last year: forecast for step h is ``history[n + h - 1 - 12]`` (horizon <= 12)."""
    n = len(history)
    return [float(history[n + h - 1 - 12]) for h in range(1, horizon + 1)], {"season": 12.0}


def _ses_fit(history: Sequence[float], alpha: float) -> tuple[float, float]:
    """Returns (in-sample one-step absolute error sum, final level). Uses only ``history``."""
    level = float(history[0])
    error = 0.0
    for value in history[1:]:
        error += abs(value - level)
        level = alpha * value + (1 - alpha) * level
    return error, level


def exponential_smoothing(history: Sequence[float], horizon: int) -> tuple[list[float], dict[str, float]]:
    """Simple exponential smoothing; alpha picked from a fixed grid on the training data only.

    Each in-sample one-step prediction uses only earlier points, so choosing alpha by
    their error never looks past the end of ``history``. Ties keep the smallest alpha.
    """
    best_alpha, best_error, best_level = SES_ALPHAS[0], math.inf, float(history[-1])
    for alpha in SES_ALPHAS:
        error, level = _ses_fit(history, alpha)
        if error < best_error - TIE_TOLERANCE:
            best_alpha, best_error, best_level = alpha, error, level
    return [best_level] * horizon, {"alpha": best_alpha}


METHODS: tuple[Method, ...] = (
    Method("naive", 1, 0, naive),
    Method("trailing_mean_3", 3, 1, trailing_mean(3)),
    Method("trailing_mean_6", 6, 2, trailing_mean(6)),
    Method("seasonal_naive", 12, 3, seasonal_naive),
    Method("exponential_smoothing", 3, 4, exponential_smoothing),
)
_BY_NAME = {m.name: m for m in METHODS}


def fold_count(n: int, start: int, horizon: int) -> int:
    """Out-of-sample (origin, horizon) pairs available with origins ``start..n-1``."""
    return sum(min(horizon, n - origin) for origin in range(start, n))


def _backtest_is_adequate(n: int, start: int, horizon: int) -> bool:
    return n - start >= MIN_COMMON_ORIGINS and fold_count(n, start, horizon) >= MIN_POOLED_ERRORS


def select_candidates(n: int, min_train: int, horizon: int = 1) -> tuple[list[Method], int]:
    """Methods scored on a shared set of origins, and the first origin (training length).

    Methods whose history need fits in ``min_train`` always take part. Longer-memory
    methods join only if the shared backtest still has MIN_COMMON_ORIGINS origins and
    MIN_POOLED_ERRORS errors. Seasonal naive needs 12 months of training, so in practice
    it needs ``months_needed_for("seasonal_naive", ...)`` months (16 for horizons >= 3,
    more for shorter horizons), not 13.
    """
    chosen = [m for m in METHODS if m.min_history <= min_train]
    start = min_train
    for method in METHODS:
        if method in chosen:
            continue
        candidate_start = max(start, method.min_history)
        if _backtest_is_adequate(n, candidate_start, horizon):
            chosen.append(method)
            start = candidate_start
    chosen.sort(key=lambda m: m.simplicity)
    return chosen, start


def months_needed(min_train: int, horizon: int) -> int:
    """Smallest history length that yields an adequate shared backtest."""
    n = min_train + 1
    while not _backtest_is_adequate(n, min_train, horizon):
        n += 1
    return n


def months_needed_for(method_name: str, min_train: int, horizon: int) -> int:
    """Smallest history length at which ``method_name`` takes part in the backtest."""
    n = months_needed(min_train, horizon)
    while method_name not in {m.name for m in select_candidates(n, min_train, horizon)[0]}:
        n += 1
    return n


# ============================================================= Backtest


@dataclass(frozen=True)
class Fold:
    origin: int  # training is y[:origin]
    horizon: int
    forecast: float
    actual: float

    @property
    def target(self) -> int:
        return self.origin + self.horizon - 1

    @property
    def error(self) -> float:
        return self.actual - self.forecast


@dataclass
class BacktestResult:
    method: str
    folds: list[Fold] = field(default_factory=list)
    params_by_origin: dict[int, dict[str, float]] = field(default_factory=dict)


def rolling_origin_backtest(
    series: Sequence[float], method: Method, start: int, horizon: int
) -> BacktestResult:
    """Origins ``t = start..n-1``; the method is given ``series[:t]`` and nothing else."""
    result = BacktestResult(method.name)
    n = len(series)
    for origin in range(start, n):
        training = list(series[:origin])  # copy: the method cannot reach later values
        forecasts, params = method.fn(training, horizon)
        result.params_by_origin[origin] = params
        for h in range(1, horizon + 1):
            target = origin + h - 1
            if target >= n:
                break
            result.folds.append(Fold(origin, h, forecasts[h - 1], float(series[target])))
    return result


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


Offsets = list[tuple[float, float, str] | None]


def minimum_half_width(values: Sequence[float]) -> float:
    """Documented floor for a band's half-width at horizon 1 (see MIN_BAND_FRACTION)."""
    level = sum(abs(v) for v in values) / len(values) if values else 0.0
    return max(MIN_BAND_FRACTION * level, MIN_BAND_FLOOR)


def interval_offsets(
    errors_by_horizon: dict[int, list[float]],
    horizon: int,
    *,
    min_half_width: float = 0.0,
    cumulative: bool = False,
) -> Offsets:
    """(lower offset, upper offset, basis) per horizon, added to the point forecast.

    No band at all with fewer than MIN_POOLED_ERRORS errors in total. Otherwise
    per-horizon empirical 10th/90th percentiles when a horizon has MIN_HORIZON_ERRORS
    errors; else percentiles of all errors normalised by their horizon's scale and
    rescaled (sqrt(h) for monthly values; h for cumulative sums, a conservative choice
    when errors are correlated across months). Offsets never narrow as the horizon
    grows, and a band narrower than ``2 * min_half_width * scale(h)`` is widened
    symmetrically to that width (basis gains "+minimum_width").
    """
    scale_of = (lambda h: float(h)) if cumulative else math.sqrt
    fallback = "h_scaled" if cumulative else "sqrt_h_scaled"
    pooled = [e / scale_of(h) for h, errors in errors_by_horizon.items() for e in errors]
    out: Offsets = []
    previous: tuple[float, float, str] | None = None
    for h in range(1, horizon + 1):
        errors = errors_by_horizon.get(h, [])
        current: tuple[float, float, str] | None
        if len(pooled) < MIN_POOLED_ERRORS:
            current = None  # too few out-of-sample errors in total for any band
        elif len(errors) >= MIN_HORIZON_ERRORS:
            current = (_quantile(errors, LOWER_Q), _quantile(errors, UPPER_Q), "empirical")
        else:
            scale = scale_of(h)
            current = (_quantile(pooled, LOWER_Q) * scale, _quantile(pooled, UPPER_Q) * scale, fallback)
        if current is not None and previous is not None:
            current = (min(current[0], previous[0]), max(current[1], previous[1]), current[2])
        if current is not None and min_half_width > 0:
            half = min_half_width * scale_of(h)
            if current[1] - current[0] < 2 * half:
                centre = (current[0] + current[1]) / 2
                current = (centre - half, centre + half, current[2] + "+minimum_width")
        out.append(current)
        if current is not None:
            previous = current
    return out


def _errors_by_horizon(folds: Sequence[Fold]) -> dict[int, list[float]]:
    grouped: dict[int, list[float]] = {}
    for fold in folds:
        grouped.setdefault(fold.horizon, []).append(fold.error)
    return grouped


COVERAGE_NOT_MEANINGFUL = (
    "Not meaningful: every out-of-sample error was zero, so the band is the documented minimum width "
    "rather than a measured spread."
)


def out_of_sample_coverage(
    folds: Sequence[Fold], horizon: int, values: Sequence[float] | None = None
) -> tuple[float | None, int]:
    """Coverage of intervals built at each origin only from errors observed before it.

    The minimum band width at origin ``t`` uses only ``values[:t]``.
    """
    covered = 0
    scored = 0
    by_origin: dict[int, list[Fold]] = {}
    for fold in folds:
        by_origin.setdefault(fold.origin, []).append(fold)
    for origin, origin_folds in sorted(by_origin.items()):
        known = [f for f in folds if f.target < origin]
        floor = minimum_half_width(values[:origin]) if values is not None else 0.0
        offsets = interval_offsets(_errors_by_horizon(known), horizon, min_half_width=floor)
        for fold in origin_folds:
            bounds = offsets[fold.horizon - 1]
            if bounds is None:
                continue
            scored += 1
            if fold.forecast + bounds[0] - 1e-9 <= fold.actual <= fold.forecast + bounds[1] + 1e-9:
                covered += 1
    return (covered / scored if scored else None), scored


def score(result: BacktestResult, horizon: int, values: Sequence[float] | None = None) -> MethodBacktest:
    errors = [f.error for f in result.folds]
    count = len(errors)
    mae = sum(abs(e) for e in errors) / count if count else 0.0
    rmse = math.sqrt(sum(e * e for e in errors) / count) if count else 0.0
    mape: float | None = None
    mape_note: str | None = None
    if not count:
        mape_note = "No out-of-sample folds."
    elif any(f.actual == 0 for f in result.folds):
        mape_note = "MAPE is undefined because at least one actual value is zero."
    else:
        mape = round(100 * sum(abs(f.error) / abs(f.actual) for f in result.folds) / count, 2)
    coverage, coverage_folds = out_of_sample_coverage(result.folds, horizon, values)
    coverage_note: str | None = None
    if count and all(e == 0 for e in errors):
        coverage, coverage_note = None, COVERAGE_NOT_MEANINGFUL
    elif coverage is None:
        coverage_note = "Too few earlier errors to build an interval at any origin."
    return MethodBacktest(
        method=result.method,
        folds=count,
        mae=round(mae, 2),
        rmse=round(rmse, 2),
        mape=mape,
        mape_note=mape_note,
        interval_coverage=None if coverage is None else round(coverage, 3),
        coverage_folds=coverage_folds,
        coverage_note=coverage_note,
    )


def _raw_mae(result: BacktestResult) -> float:
    return sum(abs(f.error) for f in result.folds) / len(result.folds) if result.folds else math.inf


def choose_method(results: Sequence[BacktestResult]) -> BacktestResult:
    """Lowest out-of-sample MAE; ties (within TIE_TOLERANCE) go to the simpler method."""
    best: BacktestResult | None = None
    for result in sorted(results, key=lambda r: _BY_NAME[r.method].simplicity):
        if best is None or _raw_mae(result) < _raw_mae(best) - TIE_TOLERANCE:
            best = result
    if best is None:
        raise ValueError("no backtest results to choose from")
    return best


# ============================================================= Series forecast


def _cumulative_errors(folds: Sequence[Fold], horizon: int) -> dict[int, list[float]]:
    by_origin: dict[int, dict[int, float]] = {}
    for fold in folds:
        by_origin.setdefault(fold.origin, {})[fold.horizon] = fold.error
    out: dict[int, list[float]] = {}
    for errors in by_origin.values():
        running = 0.0
        for h in range(1, horizon + 1):
            if h not in errors:
                break
            running += errors[h]
            out.setdefault(h, []).append(running)
    return out


@dataclass(frozen=True)
class SeriesOutcome:
    forecast: SeriesForecast
    points: list[float]
    cumulative_offsets: Offsets


def forecast_series(
    name: str,
    values: Sequence[float],
    months: Sequence[Month],
    horizon: int,
    min_train: int,
    *,
    non_negative: bool = False,
) -> SeriesOutcome:
    candidates, start = select_candidates(len(values), min_train, horizon)
    if not _backtest_is_adequate(len(values), start, horizon):
        raise ValueError("history too short for an adequate out-of-sample backtest")
    results = [rolling_origin_backtest(values, method, start, horizon) for method in candidates]
    scored = {r.method: score(r, horizon, values) for r in results}
    chosen = choose_method(results)
    naive_result = next(r for r in results if r.method == "naive")
    chosen_mae, naive_mae = _raw_mae(chosen), _raw_mae(naive_result)

    if chosen.method == "naive":
        comparison = ("No method beat the naive baseline (repeat last month) out of sample; "
                      "the forecast is simply last month's value.")
        beats = False
    else:
        improvement = 100 * (naive_mae - chosen_mae) / naive_mae if naive_mae > 0 else 0.0
        beats = chosen_mae < naive_mae - TIE_TOLERANCE
        comparison = (f"{chosen.method} beat the naive baseline out of sample "
                      f"(MAE {chosen_mae:,.2f} vs {naive_mae:,.2f}, {improvement:.1f}% lower).")

    method = _BY_NAME[chosen.method]
    point_values, params = method.fn(list(values), horizon)
    floor = minimum_half_width(values)
    offsets = interval_offsets(_errors_by_horizon(chosen.folds), horizon, min_half_width=floor)
    last = months[-1]
    points: list[ForecastPoint] = []
    for h in range(1, horizon + 1):
        point = point_values[h - 1]
        bounds = offsets[h - 1]
        lower = upper = None
        if bounds is not None:
            lower, upper = point + bounds[0], point + bounds[1]
            if non_negative:
                lower, upper = max(0.0, lower), max(0.0, upper)
            lower, upper = round(lower, 2), round(upper, 2)
        points.append(ForecastPoint(
            month=month_label(_shift(last, h)), horizon=h, point=round(point, 2), lower=lower, upper=upper,
            interval_basis=bounds[2] if bounds is not None else "unavailable_too_few_errors",
        ))

    chosen_score = scored[chosen.method]
    interval_note: str | None = None
    if any(p.interval_basis.endswith("+minimum_width") for p in points):
        interval_note = (
            f"At least one band was widened to the documented minimum (half-width {floor:,.2f} at horizon 1 = "
            f"max({MIN_BAND_FRACTION:.0%} of the mean absolute monthly level, {MIN_BAND_FLOOR:,.2f}), "
            "scaled by sqrt(h)) because the observed errors were smaller than that."
        )
    forecast = SeriesForecast(
        series=name,
        chosen_method=chosen.method,
        chosen_parameters=params,
        naive_baseline_mae=round(naive_mae, 2),
        chosen_mae=round(chosen_mae, 2),
        beats_naive=beats,
        comparison=comparison,
        evaluation_origins=len(values) - start,
        evaluation_start_month=month_label(months[start]),
        methods=[scored[r.method] for r in results],
        interval_level=INTERVAL_LEVEL,
        empirical_coverage=chosen_score.interval_coverage,
        coverage_folds=chosen_score.coverage_folds,
        coverage_note=chosen_score.coverage_note,
        interval_note=interval_note,
        points=points,
    )
    cumulative = interval_offsets(_cumulative_errors(chosen.folds, horizon), horizon, min_half_width=floor,
                                  cumulative=True)
    return SeriesOutcome(forecast, point_values, cumulative)


# ============================================================= Balance scenario


LIQUID_TYPES = ("checking", "savings")


def balance_scenario(
    accounts: Sequence[FinancialAccount],
    net: SeriesOutcome,
    current_month_net_to_date: float,
    future_dated_net: float = 0.0,
) -> BalanceScenario:
    liquid = [a for a in accounts if a.account_type in LIQUID_TYPES and math.isfinite(a.balance)]
    start = round(sum(a.balance for a in liquid), 2)
    points: list[BalancePoint] = []
    first_negative: str | None = None
    cumulative = 0.0
    for h, forecast_point in enumerate(net.forecast.points, start=1):
        cumulative += net.points[h - 1]
        # Flows already recorded this month, and future-dated flows, are already in today's balance
        # (record_transaction applies them immediately); the forecast flow covers them, so take them out.
        point = start + cumulative - current_month_net_to_date - future_dated_net
        bounds = net.cumulative_offsets[h - 1]
        lower = upper = None
        if bounds is not None:
            lower, upper = round(point + bounds[0], 2), round(point + bounds[1], 2)
        if first_negative is None and (lower if lower is not None else point) < 0:
            first_negative = forecast_point.month
        points.append(BalancePoint(month=forecast_point.month, horizon=h, point=round(point, 2),
                                   lower=lower, upper=upper))
    note = (
        "Scenario, not a prediction: today's checking and savings balances plus the forecast net cash flow "
        "of all recorded transactions, with a band from out-of-sample cumulative errors (h-scaled where a "
        "horizon has too few errors). The band's coverage is not measured. It assumes every recorded flow "
        "moves liquid cash and that past patterns continue."
    )
    return BalanceScenario(
        label="scenario",
        starting_liquid_balance=start,
        liquid_accounts=len(liquid),
        current_month_net_to_date=round(current_month_net_to_date, 2),
        future_dated_net=round(future_dated_net, 2),
        points=points,
        first_month_lower_bound_below_zero=first_negative,
        note=note,
    )


# ============================================================= Entry point


def build_forecast(
    accounts: Sequence[FinancialAccount],
    transactions: Sequence[FinancialTransaction],
    *,
    horizon: int = 3,
    min_train: int = 6,
    truncated: bool = False,
    accounts_truncated: bool = False,
    as_of: datetime | None = None,
) -> FinanceForecast:
    if not 1 <= horizon <= 12:
        raise ValueError("horizon must be between 1 and 12")
    if min_train < MIN_TRAIN_FLOOR:
        # Never extrapolate from one or two points.
        raise ValueError(f"min_train must be at least {MIN_TRAIN_FLOOR}")
    now = datetime.now(timezone.utc)
    as_of = _utc(as_of or now)
    series = build_monthly_series(transactions, as_of, truncated=truncated)
    n = len(series.months)
    data = ForecastDataSummary(
        months_available=n,
        months_needed=months_needed(min_train, horizon),
        first_month=month_label(series.months[0]) if n else None,
        last_month=month_label(series.months[-1]) if n else None,
        transactions_considered=series.transactions_considered,
        truncated=truncated,
        accounts_truncated=accounts_truncated,
        excluded_invalid=series.excluded_invalid,
        excluded_current_month=series.excluded_current_month,
        excluded_future_dated=series.excluded_future_dated,
        excluded_older_than_window=series.excluded_older_than_window,
        history=[MonthlyFlow(month=month_label(m), income=i, spending=s, net=v)
                 for m, i, s, v in zip(series.months, series.income, series.spending, series.net)],
    )
    notes = [
        "History uses complete calendar months in UTC; the current month is excluded from history and is "
        "horizon 1 of the forecast.",
        "Spending is reported as a positive amount; net = income - spending. Transfers between your own "
        "accounts are counted like any other transaction, refunds count as income, and a credit-card "
        "purchase plus the card payment can both count as spending.",
        "The chosen method's MAE, coverage and intervals come from the same backtest folds used to select "
        "it, so they may be slightly optimistic.",
    ]
    if series.excluded_invalid:
        notes.append(f"{series.excluded_invalid} stored transaction(s) with a non-finite amount were ignored.")
    if accounts_truncated:
        notes.append("Only the most recently created accounts were read; the starting liquid balance may be "
                     "incomplete.")
    if truncated:
        notes.append("Only the most recent transactions were read; the earliest (possibly incomplete) month "
                     "was dropped from history.")
    needed = months_needed(min_train, horizon)
    if n < needed:
        notes.append(
            f"Insufficient history: {n} complete month(s) available, {needed} needed so that at least "
            f"{MIN_COMMON_ORIGINS} out-of-sample origins and {MIN_POOLED_ERRORS} out-of-sample errors back "
            f"every forecast and its band (min_train={min_train}, horizon={horizon}). No forecast is produced."
        )
        return FinanceForecast(status="insufficient_history", generated_at=now, as_of=as_of, horizon=horizon,
                               min_train=min_train, disclaimer=DISCLAIMER, data=data, notes=notes)

    net = forecast_series("net", series.net, series.months, horizon, min_train)
    income = forecast_series("income", series.income, series.months, horizon, min_train, non_negative=True)
    spending = forecast_series("spending", series.spending, series.months, horizon, min_train, non_negative=True)
    if not net.forecast.beats_naive:
        notes.append("For net cash flow no method beat the naive baseline out of sample.")
    seasonal_needed = months_needed_for("seasonal_naive", min_train, horizon)
    if n < seasonal_needed:
        notes.append(f"Seasonal naive (same month last year) was not evaluated: with these settings it needs "
                     f"{seasonal_needed} complete months (12 to train plus an adequate backtest).")
    outcomes = (net, income, spending)
    if any(p.lower is None or p.upper is None for o in outcomes for p in o.forecast.points) or any(
            b is None for b in net.cumulative_offsets):
        raise AssertionError("adequate backtest must yield a band for every horizon")  # guarded above
    return FinanceForecast(
        status="ok", generated_at=now, as_of=as_of, horizon=horizon, min_train=min_train,
        disclaimer=DISCLAIMER, data=data, net=net.forecast, income=income.forecast, spending=spending.forecast,
        balance_scenario=balance_scenario(accounts, net, series.current_month_net_to_date,
                                          series.future_dated_net),
        notes=notes,
    )
