"""v6.6 finance forecasts: out-of-sample backtesting and uncertainty (analysis only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from jarvis.core.config import Settings
from jarvis.finance import forecasting as fc
from jarvis.finance import service as finance_service_module
from jarvis.finance.models import FinancialAccount, FinancialTransaction
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "finance-forecast-test-token-at-least-32-chars"
TEST_OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def tx(amount, when, owner=TEST_OWNER, account=None):
    return FinancialTransaction(
        id=uuid4(), account_id=account or uuid4(), owner_id=owner, amount=amount, category="other",
        description="t", transaction_date=when, created_at=NOW,
    )


def account(kind, balance, owner=TEST_OWNER):
    return FinancialAccount(id=uuid4(), owner_id=owner, name=kind, account_type=kind, institution="Bank",
                            balance=balance, created_at=NOW, updated_at=NOW)


def months_back(count, end=(2026, 9)):
    """``count`` (year, month) pairs ending at ``end``, oldest first."""
    return [fc._shift(end, -i) for i in range(count - 1, -1, -1)]


def net_transactions(values, end=(2026, 9), owner=TEST_OWNER):
    return [tx(v, datetime(y, m, 10, tzinfo=timezone.utc), owner=owner)
            for (y, m), v in zip(months_back(len(values), end), values)]


# ============================================================= Series building


def test_series_counts_empty_months_as_zero_and_uses_utc_month_boundaries():
    eastern = timezone(timedelta(hours=-5))
    txs = [
        tx(100.0, datetime(2026, 1, 5, tzinfo=timezone.utc)),
        tx(-40.0, datetime(2026, 1, 31, 23, 30, tzinfo=eastern)),  # 2026-02-01 04:30 UTC
        tx(-10.0, datetime(2026, 4, 1, 0, 0)),  # naive datetime is treated as UTC
        tx(-5.0, datetime(2026, 5, 2, tzinfo=timezone.utc)),  # current month: excluded from history
        tx(999.0, datetime(2026, 6, 1, tzinfo=timezone.utc)),  # future-dated: excluded
    ]
    series = fc.build_monthly_series(txs, datetime(2026, 5, 10, tzinfo=timezone.utc))
    assert [fc.month_label(m) for m in series.months] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert series.income == [100.0, 0.0, 0.0, 0.0]
    assert series.spending == [0.0, 40.0, 0.0, 10.0]  # outflows are positive magnitudes
    assert series.net == [100.0, -40.0, 0.0, -10.0]
    assert (series.excluded_current_month, series.excluded_future_dated) == (1, 1)
    assert series.current_month_net_to_date == -5.0
    assert series.transactions_considered == 3


def test_series_with_income_and_spending_in_the_same_month():
    txs = [tx(500.0, datetime(2026, 8, 1, tzinfo=timezone.utc)), tx(-200.0, datetime(2026, 8, 31, tzinfo=timezone.utc))]
    series = fc.build_monthly_series(txs, AS_OF)
    assert (series.income, series.spending, series.net) == ([500.0, 0.0], [200.0, 0.0], [300.0, 0.0])


def test_truncated_read_drops_the_possibly_partial_earliest_month():
    txs = net_transactions([1.0, 2.0, 3.0, 4.0])
    full = fc.build_monthly_series(txs, AS_OF)
    cut = fc.build_monthly_series(txs, AS_OF, truncated=True)
    assert len(full.months) == 4 and len(cut.months) == 3 and cut.dropped_partial_first_month
    assert cut.net == [2.0, 3.0, 4.0]


def test_history_window_is_bounded():
    txs = net_transactions([1.0, 2.0]) + [tx(5.0, datetime(1990, 1, 1, tzinfo=timezone.utc))]
    series = fc.build_monthly_series(txs, AS_OF)
    assert len(series.months) == fc.MAX_HISTORY_MONTHS
    assert series.excluded_older_than_window == 1


# ============================================================= Methods


def test_methods_on_hand_computed_series():
    assert fc.naive([1.0, 2.0, 3.0], 2)[0] == [3.0, 3.0]
    assert fc.trailing_mean(3)([1.0, 2.0, 3.0, 4.0, 5.0], 2)[0] == [4.0, 4.0]
    assert fc.trailing_mean(6)([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 1)[0] == [3.5]
    assert fc.seasonal_naive([float(i) for i in range(13)], 3)[0] == [1.0, 2.0, 3.0]
    # SES: alpha 0.8 has the smallest in-sample one-step error on [0, 10, 10, 10]:
    # levels 0 -> 8 -> 9.6 -> 9.92, errors 10 + 2 + 0.4.
    values, params = fc.exponential_smoothing([0.0, 10.0, 10.0, 10.0], 2)
    assert params == {"alpha": 0.8} and values == pytest.approx([9.92, 9.92])
    # Equal in-sample error for every alpha keeps the smallest (smoothest) alpha.
    values, params = fc.exponential_smoothing([10.0, 20.0], 1)
    assert params == {"alpha": 0.2} and values == pytest.approx([12.0])


def test_candidates_share_origins_and_seasonal_needs_its_real_history():
    names = lambda ms: [m.name for m in ms]  # noqa: E731
    methods, start = fc.select_candidates(10, 6, 3)
    assert start == 6 and "seasonal_naive" not in names(methods) and "trailing_mean_6" in names(methods)
    # trailing_mean_6 would leave 6 one-step errors (< MIN_POOLED_ERRORS) at 12 months; it joins at 14.
    assert "trailing_mean_6" not in names(fc.select_candidates(12, 3, 1)[0])
    assert "trailing_mean_6" in names(fc.select_candidates(14, 3, 1)[0])
    # Seasonal naive trains on 12 months, so it needs more than 13 months to be evaluated honestly.
    assert fc.months_needed_for("seasonal_naive", 6, 3) == 16
    assert fc.months_needed_for("seasonal_naive", 3, 3) == 16
    assert fc.months_needed_for("seasonal_naive", 6, 1) == 20
    assert "seasonal_naive" not in names(fc.select_candidates(15, 6, 3)[0])
    methods, start = fc.select_candidates(16, 6, 3)
    assert start == 12 and "seasonal_naive" in names(methods)


def test_seasonal_requirement_is_reported_in_notes():
    short = fc.build_forecast([], net_transactions([100.0 + i % 3 for i in range(14)]), as_of=AS_OF)
    assert short.status == "ok"
    assert any("Seasonal naive" in note and "16" in note for note in short.notes)
    assert "seasonal_naive" not in {m.method for m in short.net.methods}
    full = fc.build_forecast([], net_transactions([100.0 + i % 3 for i in range(16)]), as_of=AS_OF)
    assert "seasonal_naive" in {m.method for m in full.net.methods}
    assert not any("Seasonal naive" in note for note in full.notes)


# ============================================================= Backtest


def test_backtest_never_sees_future_values():
    base = [0.0] * 6 + [1000.0] + [0.0] * 5
    for method in fc.METHODS:
        if method.min_history > 6:
            continue
        result = fc.rolling_origin_backtest(base, method, 6, 3)
        first = [f for f in result.folds if f.origin == 6]
        # The spike at index 6 is the first target; a leaking method would have moved towards it.
        assert first[0].actual == 1000.0 and first[0].forecast == 0.0, method.name
        # Changing every value from an index onwards never changes forecasts made at earlier origins.
        for cut in range(6, len(base)):
            altered = base[:cut] + [v + 12345.0 for v in base[cut:]]
            other = fc.rolling_origin_backtest(altered, method, 6, 3)
            before = [(f.origin, f.horizon, f.forecast) for f in result.folds if f.origin <= cut]
            after = [(f.origin, f.horizon, f.forecast) for f in other.folds if f.origin <= cut]
            assert before == after, (method.name, cut)
            assert {o: p for o, p in result.params_by_origin.items() if o <= cut} == \
                {o: p for o, p in other.params_by_origin.items() if o <= cut}


def test_backtest_fold_counts_and_targets():
    result = fc.rolling_origin_backtest([float(i) for i in range(10)], fc.METHODS[0], 6, 3)
    # Origins 6..9 with horizons truncated at the end of the data: 3 + 3 + 2 + 1.
    assert len(result.folds) == 9
    assert all(f.target < 10 and f.forecast == f.origin - 1 for f in result.folds)


def test_selection_prefers_lower_mae_and_reports_naive_comparison():
    alternating = [10.0 if i % 2 == 0 else 0.0 for i in range(16)]
    outcome = fc.forecast_series("net", alternating, months_back(16), 1, 6)
    assert outcome.forecast.chosen_method == "trailing_mean_6"
    assert outcome.forecast.beats_naive is True
    assert outcome.forecast.naive_baseline_mae == 10.0 and outcome.forecast.chosen_mae == 5.0
    assert "beat the naive baseline" in outcome.forecast.comparison


def test_naive_wins_ties_and_is_reported_plainly():
    outcome = fc.forecast_series("net", [50.0] * 12, months_back(12), 2, 6)
    assert outcome.forecast.chosen_method == "naive"
    assert outcome.forecast.beats_naive is False
    assert "No method beat the naive baseline" in outcome.forecast.comparison


def test_tie_breaking_goes_to_the_simpler_method():
    def result(name, error):
        r = fc.BacktestResult(name)
        r.folds.append(fc.Fold(5, 1, 0.0, error))
        return r

    assert fc.choose_method([result("exponential_smoothing", 3.0), result("trailing_mean_3", 3.0)]).method == \
        "trailing_mean_3"
    assert fc.choose_method([result("trailing_mean_3", 3.0), result("naive", 3.0)]).method == "naive"
    assert fc.choose_method([result("naive", 3.0), result("trailing_mean_6", 2.9)]).method == "trailing_mean_6"


def test_mape_is_null_with_a_reason_when_an_actual_is_zero():
    with_zero = [10.0, 20.0, 0.0, 30.0, 0.0, 15.0, 0.0, 25.0, 10.0, 0.0, 20.0, 5.0]
    outcome = fc.forecast_series("net", with_zero, months_back(12), 1, 3)
    naive_score = next(m for m in outcome.forecast.methods if m.method == "naive")
    assert naive_score.mape is None and "zero" in naive_score.mape_note
    positive = [10.0, 20.0, 10.0, 30.0, 20.0, 15.0, 10.0, 25.0, 10.0, 5.0, 20.0, 5.0]
    positive_outcome = fc.forecast_series("net", positive, months_back(12), 1, 3)
    assert all(m.mape is not None and m.mape_note is None for m in positive_outcome.forecast.methods)


def test_rmse_and_mae_on_known_errors():
    r = fc.BacktestResult("naive", [fc.Fold(3, 1, 0.0, 3.0), fc.Fold(4, 1, 0.0, -4.0)])
    s = fc.score(r, 1)
    assert (s.folds, s.mae, s.rmse) == (2, 3.5, round((12.5) ** 0.5, 2))


# ============================================================= Uncertainty


def test_intervals_are_empirical_then_scaled_and_never_narrow():
    errors = {1: [-2.0, -1.0, 0.0, 1.0, 2.0], 2: [-5.0, 5.0], 3: [-1.0]}
    offsets = fc.interval_offsets(errors, 3)
    assert offsets[0] == (pytest.approx(-1.6), pytest.approx(1.6), "empirical")
    assert offsets[1][2] == "sqrt_h_scaled" and offsets[2][2] == "sqrt_h_scaled"
    widths = [o[1] - o[0] for o in offsets]
    assert widths == sorted(widths)
    # Fewer than MIN_POOLED_ERRORS (8) errors: no band at all.
    assert fc.MIN_POOLED_ERRORS == 8
    assert fc.interval_offsets({1: [1.0] * 7}, 2) == [None, None]


def test_zero_errors_get_the_documented_minimum_band():
    offsets = fc.interval_offsets({1: [0.0] * 8}, 2, min_half_width=10.0)
    assert offsets[0] == (-10.0, 10.0, "empirical+minimum_width")
    assert offsets[1][2] == "sqrt_h_scaled+minimum_width"
    assert offsets[1][1] - offsets[1][0] == pytest.approx(20.0 * 2 ** 0.5)
    cumulative = fc.interval_offsets({1: [0.0] * 8}, 3, min_half_width=10.0, cumulative=True)
    assert cumulative[2][2] == "h_scaled+minimum_width"
    assert cumulative[2][1] - cumulative[2][0] == pytest.approx(60.0)  # h-scaled, not sqrt(h)
    assert fc.minimum_half_width([3000.0] * 4) == 150.0 and fc.minimum_half_width([0.0] * 4) == 1.0


def test_constant_series_has_a_non_zero_band_and_no_fake_coverage():
    txs = []
    for (y, m) in months_back(24):
        txs.append(tx(3000.0, datetime(y, m, 1, tzinfo=timezone.utc)))
    result = fc.build_forecast([account("checking", 100.0)], txs, horizon=3, as_of=AS_OF)
    for series in (result.income, result.net):
        assert all(p.upper - p.lower > 0 for p in series.points)
        assert all(p.interval_basis.endswith("+minimum_width") for p in series.points)
        assert series.empirical_coverage is None and series.coverage_note == fc.COVERAGE_NOT_MEANINGFUL
        assert series.interval_note and "minimum" in series.interval_note
    assert result.income.points[0].lower == 2850.0 and result.income.points[0].upper == 3150.0
    # Spending is all zero: the band is the 1.00 floor, clamped at zero.
    assert all(p.lower == 0.0 and p.upper > 0 for p in result.spending.points)
    assert all(m.interval_coverage is None for m in result.spending.methods)
    assert all(p.upper - p.lower > 0 for p in result.balance_scenario.points)


def test_out_of_sample_coverage_is_close_to_nominal_on_noisy_series():
    rng = random.Random(7)
    values = [1000.0 + rng.gauss(0, 100) for _ in range(72)]
    outcome = fc.forecast_series("net", values, months_back(72), 1, 12)
    assert outcome.forecast.interval_level == 0.8
    assert outcome.forecast.coverage_folds >= 40
    assert 0.65 <= outcome.forecast.empirical_coverage <= 0.95


def test_coverage_only_uses_errors_observed_before_each_origin():
    folds = [fc.Fold(o, 1, 0.0, 1.0) for o in range(3, 13)] + [fc.Fold(13, 1, 0.0, 100.0)]
    coverage, scored = fc.out_of_sample_coverage(folds, 1)
    # Origins 3-10 have fewer than 8 earlier errors; 11 and 12 are covered; the shock at 13 is not.
    assert scored == 3 and coverage == pytest.approx(2 / 3)


# ============================================================= End to end


def test_insufficient_history_returns_no_forecast():
    result = fc.build_forecast([], net_transactions([100.0] * 6), horizon=3, min_train=6, as_of=AS_OF)
    assert result.status == "insufficient_history"
    assert (result.data.months_available, result.data.months_needed) == (6, 10)
    assert result.net is None and result.balance_scenario is None
    assert result.disclaimer == fc.DISCLAIMER
    assert any("10 needed" in note for note in result.notes)
    empty = fc.build_forecast([], [], as_of=AS_OF)
    assert empty.status == "insufficient_history" and empty.data.months_available == 0
    with pytest.raises(ValueError):
        fc.build_forecast([], net_transactions([1.0, 2.0]), min_train=1, as_of=AS_OF)


@pytest.mark.parametrize("extra", [1, 2, 3])
def test_a_handful_of_folds_is_never_reported_as_ok(extra):
    # min_train + 1 and + 2 months used to return "ok" with one or two folds and no band.
    rng = random.Random(extra)
    values = [round(500 + rng.gauss(0, 50), 2) for _ in range(6 + extra)]
    result = fc.build_forecast([account("checking", 100.0)], net_transactions(values), horizon=3, min_train=6,
                               as_of=AS_OF)
    assert result.status == "insufficient_history" and result.net is None
    one_step = fc.build_forecast([], net_transactions(values + [500.0]), horizon=1, min_train=6, as_of=AS_OF)
    assert one_step.status == "insufficient_history"


@pytest.mark.parametrize(("months", "horizon"), [(10, 3), (14, 1), (10, 12)])
def test_ok_forecasts_always_carry_a_band_for_every_point(months, horizon):
    rng = random.Random(months + horizon)
    values = [round(500 + rng.gauss(0, 50), 2) for _ in range(months)]
    result = fc.build_forecast([account("checking", 100.0)], net_transactions(values), horizon=horizon,
                               min_train=6, as_of=AS_OF)
    assert result.status == "ok" and result.data.months_needed == months
    assert result.net.evaluation_origins >= fc.MIN_COMMON_ORIGINS
    for series in (result.net, result.income, result.spending):
        assert all(p.lower is not None and p.upper is not None for p in series.points)
        assert sum(m.folds for m in series.methods if m.method == "naive") >= fc.MIN_POOLED_ERRORS
    assert all(p.lower is not None for p in result.balance_scenario.points)


def test_balance_path_and_first_month_below_zero():
    accounts = [account("checking", 1000.0), account("savings", 500.0), account("credit", -300.0)]
    txs = net_transactions([-400.0] * 12) + [tx(-100.0, datetime(2026, 10, 3, tzinfo=timezone.utc))]
    result = fc.build_forecast(accounts, txs, horizon=5, min_train=6, as_of=AS_OF)
    assert result.status == "ok" and result.net.chosen_method == "naive"
    scenario = result.balance_scenario
    assert scenario.label == "scenario" and "not a prediction" in scenario.note
    assert "not measured" in scenario.note
    assert (scenario.starting_liquid_balance, scenario.liquid_accounts) == (1500.0, 2)
    assert scenario.current_month_net_to_date == -100.0
    # 1500 + 100 already spent this month - 400 per month.
    assert [p.point for p in scenario.points] == [1200.0, 800.0, 400.0, 0.0, -400.0]
    assert [p.month for p in scenario.points][0] == "2026-10"
    # Every backtest error is zero, so the band is the documented minimum: 5% of 400 = 20, scaled by h.
    assert [p.point - p.lower for p in scenario.points] == pytest.approx([20.0, 40.0, 60.0, 80.0, 100.0])
    assert all(p.lower < p.point < p.upper for p in scenario.points)
    # The lower bound crosses zero a month before the point does.
    assert scenario.first_month_lower_bound_below_zero == "2027-01"


def test_future_dated_transactions_do_not_move_the_balance_scenario():
    history = net_transactions([-400.0] * 12)
    rent = tx(-400.0, datetime(2026, 11, 1, tzinfo=timezone.utc))  # dated after as_of
    # record_transaction applies the rent to the balance immediately: 1900 before, 1500 after.
    before = fc.build_forecast([account("checking", 1900.0)], history, horizon=3, as_of=AS_OF)
    after = fc.build_forecast([account("checking", 1500.0)], history + [rent], horizon=3, as_of=AS_OF)
    assert after.data.excluded_future_dated == 1 and after.balance_scenario.future_dated_net == -400.0
    assert [(p.point, p.lower, p.upper) for p in after.balance_scenario.points] == \
        [(p.point, p.lower, p.upper) for p in before.balance_scenario.points]


def test_non_finite_stored_amounts_are_skipped_and_counted():
    bad = tx(0.0, datetime(2026, 3, 5, tzinfo=timezone.utc)).model_copy(update={"amount": float("nan")})
    inf = tx(0.0, datetime(2026, 4, 5, tzinfo=timezone.utc)).model_copy(update={"amount": float("inf")})
    result = fc.build_forecast([account("checking", 10.0)], net_transactions([100.0] * 12) + [bad, inf],
                               as_of=AS_OF)
    assert result.status == "ok" and result.data.excluded_invalid == 2
    assert any("non-finite" in note for note in result.notes)
    import json
    assert "NaN" not in json.dumps(result.model_dump(mode="json"))
    assert [h.net for h in result.data.history] == [100.0] * 12


def test_lower_bound_crossing_precedes_the_point_crossing():
    rng = random.Random(3)
    values = [-300.0 + rng.gauss(0, 150) for _ in range(24)]
    txs = net_transactions([round(v, 2) for v in values])
    result = fc.build_forecast([account("checking", 1200.0)], txs, horizon=6, min_train=6, as_of=AS_OF)
    scenario = result.balance_scenario
    assert all(p.lower is not None and p.lower <= p.point <= p.upper for p in scenario.points)
    lower_cross = next(p.month for p in scenario.points if p.lower < 0)
    assert scenario.first_month_lower_bound_below_zero == lower_cross
    point_cross = next((p.month for p in scenario.points if p.point < 0), "9999-99")
    assert lower_cross <= point_cross


def test_income_and_spending_bounds_are_not_negative():
    rng = random.Random(5)
    txs = []
    for (y, m) in months_back(15):
        txs.append(tx(round(abs(rng.gauss(50, 60)), 2) + 0.01, datetime(y, m, 3, tzinfo=timezone.utc)))
        txs.append(tx(-round(abs(rng.gauss(40, 60)), 2) - 0.01, datetime(y, m, 4, tzinfo=timezone.utc)))
    result = fc.build_forecast([], txs, horizon=4, as_of=AS_OF)
    for series in (result.income, result.spending):
        assert all(p.lower is None or p.lower >= 0 for p in series.points)


def test_response_models_are_frozen():
    result = fc.build_forecast([], net_transactions([1.0] * 8), as_of=AS_OF)
    with pytest.raises(ValidationError):
        result.status = "ok"


def test_service_reports_truncation_when_the_read_limit_is_hit(monkeypatch):
    monkeypatch.setattr(finance_service_module, "FORECAST_MAX_TRANSACTIONS", 10)
    store = MemoryFinanceStore()
    for t in net_transactions([float(i) for i in range(1, 13)]):
        store.save_transaction(t)
    service = FinanceService(store)
    result = service.get_forecast(TEST_OWNER, horizon=1, min_train=3, as_of=AS_OF)
    assert result.data.truncated is True
    # The newest 10 months were read; the earliest of them may be partial and is dropped.
    assert result.data.months_available == 9 and result.data.first_month == "2026-01"
    assert any("most recent" in note for note in result.notes)
    monkeypatch.setattr(finance_service_module, "FORECAST_MAX_TRANSACTIONS", 12)
    assert service.get_forecast(TEST_OWNER, as_of=AS_OF).data.truncated is False


def test_service_flags_a_truncated_accounts_read(monkeypatch):
    monkeypatch.setattr(finance_service_module, "FORECAST_MAX_ACCOUNTS", 1)
    store = MemoryFinanceStore()
    store.save_account(account("checking", 10.0))
    store.save_account(account("savings", 20.0))
    result = FinanceService(store).get_forecast(TEST_OWNER, as_of=AS_OF)
    assert result.data.accounts_truncated is True
    assert any("accounts" in note for note in result.notes)


def test_memory_store_ordering_has_a_unique_tiebreaker():
    store = MemoryFinanceStore()
    same_time = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for _ in range(7):
        store.save_transaction(tx(-1.0, same_time))
    seen = []
    for offset in range(0, 7, 2):
        page, _ = store.list_transactions(TEST_OWNER, limit=2, offset=offset)
        seen.extend(t.id for t in page)
    assert len(seen) == len(set(seen)) == 7
    assert seen == sorted(seen, reverse=True)


def test_notes_flag_optimism_and_double_counting_risks():
    result = fc.build_forecast([], net_transactions([100.0 + i % 4 for i in range(12)]), as_of=AS_OF)
    text = " ".join(result.notes)
    assert "slightly optimistic" in text and "refunds count as income" in text and "credit-card" in text


def test_forecast_writes_nothing_and_records_no_audit():
    class Audit:
        events = []

        def record(self, **kwargs):
            self.events.append(kwargs)

    store = MemoryFinanceStore()
    for t in net_transactions([100.0] * 9):
        store.save_transaction(t)
    audit = Audit()
    FinanceService(store, audit).get_forecast(TEST_OWNER, as_of=AS_OF)
    assert audit.events == [] and store.get_latest_snapshot(TEST_OWNER) is None


def test_forecasting_module_has_no_execution_surface():
    """Pure computation: only the standard library and the finance models are imported."""
    import ast

    tree = ast.parse(open(fc.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module if node.module.startswith("jarvis") else node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "math", "typing", "jarvis.finance.models"}


# ============================================================= API


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(TEST_OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


@pytest.fixture
def client():
    settings = Settings(_env_file=None, environment="test", log_level="ERROR",
                        local_auth_token=SecretStr(TEST_TOKEN), owner_id=TEST_OWNER)
    app = create_app(settings=settings)
    with TestClient(app, headers=AUTH) as c:
        c.app_ref = app
        yield c


def test_api_requires_authentication(client):
    assert TestClient(client.app_ref).get("/finance/forecast").status_code == 401


def test_api_requires_finance_read(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.FINANCE_READ})
    assert client.get("/finance/forecast").status_code == 403
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.FINANCE_WRITE})
    assert client.get("/finance/forecast").status_code == 200


@pytest.mark.parametrize("query", ["horizon=0", "horizon=13", "min_train=2", "min_train=25", "horizon=x"])
def test_api_bounds(client, query):
    assert client.get(f"/finance/forecast?{query}").status_code == 422


def test_api_forecast_is_owner_scoped_and_always_carries_the_disclaimer(client):
    store = client.app_ref.state.finance_service.store
    last_complete = fc._shift(fc._month_of(datetime.now(timezone.utc)), -1)
    for t in net_transactions([100.0 + i for i in range(12)], end=last_complete, owner=OTHER_OWNER):
        store.save_transaction(t)
    res = client.get("/finance/forecast")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "insufficient_history" and body["data"]["months_available"] == 0
    assert body["disclaimer"] == fc.DISCLAIMER

    acc = client.post("/finance/accounts", json={"name": "Main", "account_type": "checking",
                                                 "institution": "Bank", "balance": 2000.0}).json()
    current = fc._month_of(datetime.now(timezone.utc))
    for i, (y, m) in enumerate(months_back(10, end=fc._shift(current, -1))):
        assert client.post("/finance/transactions", json={
            "account_id": acc["id"], "amount": -150.0 - 10 * (i % 3), "description": "groceries",
            "transaction_date": datetime(y, m, 12, tzinfo=timezone.utc).isoformat()}).status_code == 201
    body = client.get("/finance/forecast?horizon=4&min_train=4").json()
    assert body["status"] == "ok" and body["horizon"] == 4 and len(body["net"]["points"]) == 4
    assert all(p["lower"] is not None for p in body["net"]["points"])
    assert body["disclaimer"] == fc.DISCLAIMER
    assert body["data"]["months_available"] == 10
    assert body["balance_scenario"]["label"] == "scenario"
    assert {m["method"] for m in body["net"]["methods"]} >= {"naive", "trailing_mean_3"}


def test_api_rejects_non_finite_amounts(client):
    acc = client.post("/finance/accounts", json={"name": "Main", "account_type": "checking",
                                                 "institution": "Bank", "balance": 10.0}).json()
    for literal in ("NaN", "Infinity", "-Infinity"):
        res = client.post("/finance/transactions", headers={"Content-Type": "application/json"},
                          content=f'{{"account_id": "{acc["id"]}", "amount": {literal}, "description": "bad"}}')
        assert res.status_code == 422, literal
    res = client.post("/finance/accounts", headers={"Content-Type": "application/json"},
                      content='{"name": "X", "account_type": "checking", "institution": "B", "balance": NaN}')
    assert res.status_code == 422
    assert client.get(f"/finance/accounts/{acc['id']}").json()["balance"] == 10.0
