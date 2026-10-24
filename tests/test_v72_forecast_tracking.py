"""v7.2 forecast snapshots (timestamped, immutable) and out-of-sample accuracy tracking (analysis only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from jarvis.core.config import Settings
from jarvis.finance import forecasting as fc
from jarvis.finance import service as finance_service_module
from jarvis.finance import tracking
from jarvis.finance.models import (
    FinancialAccount,
    FinancialTransaction,
    ForecastSnapshot,
    ForecastSnapshotCreateRequest,
    ForecastSnapshotTarget,
)
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.finance.tracking import ForecastSnapshotExists
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "finance-tracking-test-token-at-least-32-chars"
OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
CREATED = datetime(2020, 1, 1, tzinfo=timezone.utc)


def utc(y, m, d=10, h=12, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


def tx(amount, when, owner=OWNER):
    return FinancialTransaction(id=uuid4(), account_id=uuid4(), owner_id=owner, amount=amount, category="other",
                                description="t", transaction_date=when, created_at=CREATED)


def setup(values_by_month=None, owner=OWNER):
    store = MemoryFinanceStore()
    service = FinanceService(store)
    for (y, m), value in (values_by_month or {}).items():
        store.save_transaction(tx(value, utc(y, m), owner=owner))
    return service, store


def months(start, count):
    return [fc._shift(start, i) for i in range(count)]


def target(series, month, horizon, point, lower, upper, method="trailing_mean_3"):
    return ForecastSnapshotTarget(series=series, target_month=month, horizon=horizon, method=method, point=point,
                                  lower=lower, upper=upper, interval_basis="empirical")


def crafted(store, as_of, targets, owner=OWNER, created_at=None, truncated=False):
    """A stored snapshot with known targets and the fingerprint of the store's current history."""
    transactions, _ = store.list_transactions(owner, limit=100000)
    fingerprint = tracking.history_fingerprint(transactions, as_of)
    snapshot = ForecastSnapshot(
        id=uuid4(), owner_id=owner, created_at=created_at or as_of, as_of=as_of, horizon=3, min_train=6,
        status="ok", model_version=fc.FORECAST_MODEL_VERSION, months_available=12, transactions_considered=12,
        history_transaction_count=fingerprint.count, history_latest_transaction_date=fingerprint.latest,
        history_net_total=fingerprint.net_total, truncated=truncated, accounts_truncated=False,
        target_count=len(targets), targets=targets, disclaimer=fc.DISCLAIMER,
    )
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    store.save_forecast_snapshot(snapshot, day_start=start, day_end=start, force=True, cap=500)
    return snapshot


def history(count=14, end=(2026, 9), owner=OWNER):
    """``count`` months ending at ``end`` with mixed income and spending."""
    out = []
    for i, (y, m) in enumerate(months(fc._shift(end, -(count - 1)), count)):
        out.append(tx(2000.0 + 25 * (i % 4), utc(y, m, 3), owner=owner))
        out.append(tx(-1200.0 - 40 * (i % 3), utc(y, m, 18), owner=owner))
    return out


# ============================================================= Snapshots


def test_snapshot_stores_exactly_what_the_live_forecast_returns():
    service, store = setup()
    for t in history():
        store.save_transaction(t)
    as_of = utc(2026, 10, 15)
    live = service.get_forecast(OWNER, horizon=3, min_train=6, as_of=as_of)
    assert live.status == "ok" and live.generated_at.tzinfo is not None
    snap = service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(horizon=3, min_train=6),
                                            now=as_of + timedelta(hours=1), as_of=as_of)
    assert snap.created_at == as_of + timedelta(hours=1) and snap.as_of == live.as_of
    assert (snap.status, snap.horizon, snap.min_train) == ("ok", 3, 6)
    assert snap.model_version == fc.FORECAST_MODEL_VERSION == "v6.6-1"
    assert snap.transactions_considered == live.data.transactions_considered == 28
    assert snap.history_transaction_count == 28 and snap.history_latest_transaction_date == utc(2026, 9, 18)
    assert snap.truncated is False and snap.accounts_truncated is False
    assert snap.target_count == 9 and snap.disclaimer == fc.DISCLAIMER
    for name in ("net", "income", "spending"):
        series = getattr(live, name)
        stored = [t for t in snap.targets if t.series == name]
        assert [(t.target_month, t.horizon, t.point, t.lower, t.upper, t.interval_basis) for t in stored] == [
            (p.month, p.horizon, p.point, p.lower, p.upper, p.interval_basis) for p in series.points]
        assert {t.method for t in stored} == {series.chosen_method}
    assert service.get_forecast_snapshot(OWNER, snap.id) == snap


def test_insufficient_history_is_stored_without_targets():
    service, _ = setup()
    snap = service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2026, 10, 1))
    assert snap.status == "insufficient_history" and snap.targets == [] and snap.target_count == 0
    assert snap.history_transaction_count == 0 and snap.history_latest_transaction_date is None


def test_one_snapshot_per_utc_day_unless_forced():
    service, _ = setup()
    req = ForecastSnapshotCreateRequest()
    first = service.create_forecast_snapshot(OWNER, req, now=utc(2026, 10, 1, 0, 5))
    with pytest.raises(ForecastSnapshotExists) as caught:
        service.create_forecast_snapshot(OWNER, req, now=utc(2026, 10, 1, 23, 50))
    assert caught.value.existing_id == first.id
    forced = service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(force=True),
                                              now=utc(2026, 10, 1, 23, 55))
    next_day = service.create_forecast_snapshot(OWNER, req, now=utc(2026, 10, 2, 0, 1))
    other = service.create_forecast_snapshot(OTHER, req, now=utc(2026, 10, 1, 12))  # per owner
    page = service.list_forecast_snapshots(OWNER)
    assert [s.id for s in page.items] == [next_day.id, forced.id, first.id] and page.total == 3
    assert page.disclaimer == fc.DISCLAIMER
    assert [s.id for s in service.list_forecast_snapshots(OTHER).items] == [other.id]


def test_future_as_of_is_refused():
    service, _ = setup()
    with pytest.raises(ValueError):
        service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2026, 10, 1),
                                         as_of=utc(2026, 10, 2))


def test_cap_deletes_the_oldest_first(monkeypatch):
    monkeypatch.setattr(finance_service_module, "FORECAST_SNAPSHOT_CAP", 3)
    service, _ = setup()
    made = [service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2026, 10, day))
            for day in range(1, 6)]
    other = service.create_forecast_snapshot(OTHER, ForecastSnapshotCreateRequest(), now=utc(2026, 9, 1))
    page = service.list_forecast_snapshots(OWNER)
    assert page.total == 3 and [s.id for s in page.items] == [made[4].id, made[3].id, made[2].id]
    assert service.get_forecast_snapshot(OWNER, made[0].id) is None
    assert service.get_forecast_snapshot(OTHER, other.id) is not None  # other owners are untouched


def test_snapshots_are_immutable_and_owner_scoped():
    service, store = setup()
    for t in history():
        store.save_transaction(t)
    snap = service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2026, 10, 15))
    with pytest.raises(ValidationError):
        snap.targets[0].point = 1.0
    with pytest.raises(ValidationError):
        snap.horizon = 5
    assert not any(name.startswith("update_forecast") for name in dir(store))
    store.save_transaction(tx(99999.0, utc(2026, 9, 20)))  # later data never rewrites a stored forecast
    assert service.get_forecast_snapshot(OWNER, snap.id) == snap
    assert service.get_forecast_snapshot(OTHER, snap.id) is None
    assert service.delete_forecast_snapshot(OTHER, snap.id) is False
    assert service.delete_forecast_snapshot(OWNER, snap.id) is True
    assert service.delete_forecast_snapshot(OWNER, snap.id) is False
    assert service.list_forecast_snapshots(OWNER).total == 0


# ============================================================= Accuracy (hand-computed)

ACTUALS = {(2026, 1): 80.0, (2026, 2): 95.0, (2026, 3): 105.0, (2026, 4): 130.0, (2026, 5): 100.0, (2026, 6): 50.0}
NOW = utc(2026, 6, 15)


def hand_computed():
    service, store = setup(ACTUALS)
    s1 = crafted(store, utc(2026, 3, 10), [
        target("net", "2026-03", 1, 100.0, 90.0, 110.0),
        target("net", "2026-04", 2, 100.0, 80.0, 120.0),
        target("net", "2026-05", 3, 100.0, 70.0, 130.0, method="naive"),
        target("spending", "2026-03", 1, 0.0, 0.0, 5.0),
    ])
    s2 = crafted(store, utc(2026, 5, 5), [
        target("net", "2026-05", 1, 90.0, 85.0, 95.0),
        target("net", "2026-06", 2, 90.0, 80.0, 100.0),  # current month: not complete
        target("net", "2026-07", 3, 90.0, 75.0, 105.0),
    ])
    return service, store, s1, s2


def group(groups, series, **key):
    found = [g for g in groups if g.series == series and all(getattr(g, k) == v for k, v in key.items())]
    assert len(found) == 1
    return found[0]


def test_accuracy_matches_hand_computed_mae_bias_coverage_and_naive():
    service, _, s1, s2 = hand_computed()
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert report.status == "ok" and report.current_month == "2026-06"
    assert report.disclaimer == fc.DISCLAIMER and report.interval_level == 0.8
    assert (report.targets_evaluated, report.targets_pending, report.snapshots_considered) == (5, 2, 2)
    assert report.first_pending_month == "2026-06"
    assert report.first_target_completes_at == datetime(2026, 7, 1, tzinfo=timezone.utc)

    net = group(report.overall, "net")
    # errors (actual - forecast): +5, +30, 0 (s1, naive 95) and +10 (s2, naive 130)
    assert net.evaluated == 4 and net.mae == 11.25 and net.mean_error == 11.25
    assert net.interval_coverage == 0.5 and net.coverage_targets == 4
    assert net.mape == round(100 * (5 / 105 + 30 / 130 + 0 + 10 / 100) / 4, 2) == 9.46
    assert net.naive_mae == 20.0 and net.naive_mean_error == 5.0 and net.beats_naive is True

    h1, h2, h3 = (group(report.by_horizon, "net", horizon=h) for h in (1, 2, 3))
    assert (h1.evaluated, h1.mae, h1.mean_error, h1.interval_coverage) == (2, 7.5, 7.5, 0.5)
    assert (h1.naive_mae, h1.naive_mean_error) == (20.0, -10.0)
    assert (h2.evaluated, h2.mae, h2.interval_coverage, h2.naive_mae) == (1, 30.0, 0.0, 35.0)
    assert (h3.evaluated, h3.mae, h3.interval_coverage, h3.naive_mae) == (1, 0.0, 1.0, 5.0)
    assert h3.mape == 0.0

    trailing = group(report.by_method, "net", method="trailing_mean_3")
    naive_method = group(report.by_method, "net", method="naive")
    assert (trailing.evaluated, trailing.mae) == (3, 15.0) and (naive_method.evaluated, naive_method.mae) == (1, 0.0)

    spending = group(report.overall, "spending")
    assert spending.evaluated == 1 and spending.mape is None and "zero" in spending.mape_note
    assert spending.interval_coverage == 1.0

    by_id = {s.snapshot_id: s for s in report.snapshots}
    assert (by_id[s1.id].targets_evaluated, by_id[s1.id].targets_pending) == (4, 0)
    assert (by_id[s2.id].targets_evaluated, by_id[s2.id].targets_pending) == (1, 2)
    assert all(s.data_changed_since_snapshot is False for s in report.snapshots)
    assert report.warnings == []


def test_current_and_future_months_are_never_evaluated():
    service, _, _, s2 = hand_computed()
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert group(report.by_horizon, "net", horizon=2).evaluated == 1  # s2's 2026-06 (current month) is excluded
    assert {s.snapshot_id: s.targets_evaluated for s in report.snapshots}[s2.id] == 1
    later = service.get_forecast_accuracy(OWNER, now=utc(2026, 7, 2))  # June is now complete
    assert later.targets_evaluated == 6 and later.targets_pending == 1
    assert {s.snapshot_id: s.targets_evaluated for s in later.snapshots}[s2.id] == 2


def test_data_changed_since_snapshot_is_flagged_per_snapshot():
    service, store, s1, s2 = hand_computed()
    store.save_transaction(tx(7.0, utc(2026, 4, 20)))  # imported later for April: after s1's history, in s2's
    flags = {s.snapshot_id: s.data_changed_since_snapshot for s in service.get_forecast_accuracy(OWNER, now=NOW).snapshots}
    assert flags == {s1.id: False, s2.id: True}
    store.save_transaction(tx(-3.0, utc(2026, 2, 1)))  # an earlier month: both histories changed
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert all(s.data_changed_since_snapshot is True for s in report.snapshots)
    assert any("data_changed_since_snapshot" in note for note in report.notes)
    assert group(report.overall, "net").evaluated == 4  # actuals now come from the changed data


def test_truncated_snapshots_cannot_tell_whether_data_changed():
    service, store = setup(ACTUALS)
    snap = crafted(store, utc(2026, 3, 10), [target("net", "2026-03", 1, 100.0, 90.0, 110.0)], truncated=True)
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert report.snapshots[0].snapshot_id == snap.id and report.snapshots[0].data_changed_since_snapshot is None


def test_nothing_to_evaluate_yet_reports_when_the_first_month_completes():
    service, store = setup({ym: 100.0 + i for i, ym in enumerate(months((2025, 8), 14))})
    snap = service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2026, 10, 15))
    assert snap.status == "ok"
    report = service.get_forecast_accuracy(OWNER, now=utc(2026, 10, 20))
    assert report.status == "nothing_to_evaluate_yet" and report.targets_evaluated == 0
    assert report.targets_pending == 9 and report.first_pending_month == "2026-10"
    assert report.first_target_completes_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert report.disclaimer == fc.DISCLAIMER and report.overall == []
    first = service.get_forecast_accuracy(OWNER, now=utc(2026, 11, 1))
    assert first.status == "ok" and first.targets_evaluated == 3  # October, horizon 1, for each series


def test_no_snapshots_and_insufficient_history_snapshots_have_nothing_to_evaluate():
    service, store = setup()
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert report.status == "nothing_to_evaluate_yet" and report.first_target_completes_at is None
    service.create_forecast_snapshot(OWNER, ForecastSnapshotCreateRequest(), now=utc(2025, 1, 1))
    report = service.get_forecast_accuracy(OWNER, now=NOW)
    assert report.status == "nothing_to_evaluate_yet" and report.snapshots_considered == 0
    assert report.first_pending_month is None


def test_completed_targets_without_actuals():
    service, store = setup()
    crafted(store, utc(2026, 3, 10), [target("net", "2026-03", 1, 100.0, 90.0, 110.0)])
    report = service.get_forecast_accuracy(OWNER, now=NOW)  # every transaction was deleted
    assert report.status == "no_actuals_available" and report.targets_without_actuals == 1


def test_accuracy_is_owner_scoped():
    service, store, _, _ = hand_computed()
    for (y, m), value in ACTUALS.items():
        store.save_transaction(tx(value * 10, utc(y, m), owner=OTHER))
    assert service.get_forecast_accuracy(OTHER, now=NOW).status == "nothing_to_evaluate_yet"
    assert group(service.get_forecast_accuracy(OWNER, now=NOW).overall, "net").mae == 11.25


# ============================================================= Drift


def drift_setup(points_and_bands):
    """Actual net 100 every month; one h1 net target per month 2025-10.. with the given point and band."""
    service, store = setup({ym: 100.0 for ym in months((2025, 8), 12)})
    for (y, m), (point, lower, upper) in zip(months((2025, 10), len(points_and_bands)), points_and_bands):
        crafted(store, utc(y, m, 2), [target("net", fc.month_label((y, m)), 1, point, lower, upper)])
    return service.get_forecast_accuracy(OWNER, now=utc(2026, 7, 15))


def test_drift_warns_on_consistent_bias_with_good_coverage():
    report = drift_setup([(95.0, 80.0, 120.0)] * 6)  # actual above forecast every time, always inside the band
    assert len(report.warnings) == 1 and "above the forecast" in report.warnings[0]
    report = drift_setup([(105.0, 80.0, 120.0)] * 6)
    assert len(report.warnings) == 1 and "below the forecast" in report.warnings[0]


def test_drift_warns_on_low_coverage():
    bands = [(90.0, 85.0, 95.0), (110.0, 105.0, 115.0)] * 3  # alternate signs, always outside
    report = drift_setup(bands)
    assert len(report.warnings) == 1 and "0 of the last 6" in report.warnings[0] and "80% band" in report.warnings[0]


def test_no_drift_warning_when_recent_targets_look_fine_or_are_too_few():
    assert drift_setup([(95.0, 80.0, 120.0), (105.0, 80.0, 120.0)] * 3).warnings == []
    assert drift_setup([(90.0, 85.0, 95.0)] * 5).warnings == []  # fewer than 6 evaluated targets
    # Only the last 6 count: early misses followed by 6 good months give no warning.
    assert drift_setup([(10.0, 5.0, 15.0)] * 3 + [(95.0, 80.0, 120.0), (105.0, 80.0, 120.0)] * 3).warnings == []


# ============================================================= API


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


@pytest.fixture
def client():
    settings = Settings(_env_file=None, environment="test", log_level="ERROR",
                        local_auth_token=SecretStr(TEST_TOKEN), owner_id=OWNER)
    app = create_app(settings=settings)
    with TestClient(app, headers=AUTH) as c:
        c.app_ref = app
        yield c


def test_api_requires_authentication(client):
    anonymous = TestClient(client.app_ref)
    assert anonymous.post("/finance/forecast/snapshots", json={}).status_code == 401
    assert anonymous.get("/finance/forecast/snapshots").status_code == 401
    assert anonymous.get(f"/finance/forecast/snapshots/{uuid4()}").status_code == 401
    assert anonymous.delete(f"/finance/forecast/snapshots/{uuid4()}").status_code == 401
    assert anonymous.get("/finance/forecast/accuracy").status_code == 401


def test_api_capabilities(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.FINANCE_WRITE})
    assert client.post("/finance/forecast/snapshots", json={}).status_code == 403
    assert client.delete(f"/finance/forecast/snapshots/{uuid4()}").status_code == 403
    assert client.get("/finance/forecast/snapshots").status_code == 200
    assert client.get("/finance/forecast/accuracy").status_code == 200
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.FINANCE_READ})
    assert client.get("/finance/forecast/snapshots").status_code == 403
    assert client.get(f"/finance/forecast/snapshots/{uuid4()}").status_code == 403
    assert client.get("/finance/forecast/accuracy").status_code == 403
    assert client.post("/finance/forecast/snapshots", json={}).status_code == 201


@pytest.mark.parametrize("body", [{"horizon": 0}, {"horizon": 13}, {"min_train": 2}, {"min_train": 25},
                                  {"horizon": "x"}, {"as_of": "2020-01-01T00:00:00Z"}, {"force": "maybe"}])
def test_api_body_bounds(client, body):
    assert client.post("/finance/forecast/snapshots", json=body).status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_api_list_bounds(client, query):
    assert client.get(f"/finance/forecast/snapshots?{query}").status_code == 422


def test_api_snapshot_lifecycle_and_disclaimer(client):
    live = client.get("/finance/forecast").json()
    assert live["generated_at"] and live["disclaimer"] == fc.DISCLAIMER
    created = client.post("/finance/forecast/snapshots", json={"horizon": 2, "min_train": 4})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "insufficient_history" and body["targets"] == [] and body["created_at"]
    assert body["disclaimer"] == fc.DISCLAIMER and body["model_version"] == fc.FORECAST_MODEL_VERSION
    again = client.post("/finance/forecast/snapshots", json={})
    assert again.status_code == 409 and body["id"] in again.json()["detail"]
    assert client.post("/finance/forecast/snapshots", json={"force": True}).status_code == 201
    page = client.get("/finance/forecast/snapshots?limit=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["disclaimer"] == fc.DISCLAIMER
    assert "targets" not in page["items"][0]
    assert client.get(f"/finance/forecast/snapshots/{body['id']}").json()["id"] == body["id"]
    accuracy = client.get("/finance/forecast/accuracy").json()
    assert accuracy["status"] == "nothing_to_evaluate_yet" and accuracy["disclaimer"] == fc.DISCLAIMER
    assert client.delete(f"/finance/forecast/snapshots/{body['id']}").status_code == 204
    assert client.get(f"/finance/forecast/snapshots/{body['id']}").status_code == 404
    assert client.delete(f"/finance/forecast/snapshots/{body['id']}").status_code == 404
    assert client.get("/finance/forecast/snapshots/not-a-uuid").status_code == 422


def test_api_other_owner_snapshots_are_invisible(client):
    service = client.app_ref.state.finance_service
    foreign = service.create_forecast_snapshot(OTHER, ForecastSnapshotCreateRequest())
    assert client.get(f"/finance/forecast/snapshots/{foreign.id}").status_code == 404
    assert client.delete(f"/finance/forecast/snapshots/{foreign.id}").status_code == 404
    assert client.get("/finance/forecast/snapshots").json()["total"] == 0


def test_tracking_module_has_no_trading_or_network_imports():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(tracking))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module if node.module.startswith("jarvis") else node.module.split(".")[0])
    assert imported <= {"__future__", "bisect", "dataclasses", "datetime", "math", "typing", "uuid",
                        "jarvis.finance.forecasting", "jarvis.finance.models"}
