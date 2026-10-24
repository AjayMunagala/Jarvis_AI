"""v7.2 live PostgreSQL validation: forecast snapshots, accuracy against later months, cascade, immutability."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest


def _utc(y, m, d=10, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _months(start, count):
    out, (y, m) = [], start
    for _ in range(count):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def test_snapshot_accuracy_cascade_and_persistence_in_postgresql(isolated_postgres):
    import psycopg

    from jarvis.finance import forecasting as fc
    from jarvis.finance.models import AccountCreateRequest, ForecastSnapshotCreateRequest, TransactionCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore
    from jarvis.finance.tracking import ForecastSnapshotExists

    owner, other = uuid4(), uuid4()
    url = isolated_postgres.database_url
    service = FinanceService(PostgresFinanceStore(url))
    account = service.create_account(owner, AccountCreateRequest(
        name="Live forecast", account_type="checking", institution="Test", balance=5000.0))
    # 18 months of past-dated income and spending: 2024-01 .. 2025-06.
    for i, (y, m) in enumerate(_months((2024, 1), 18)):
        service.record_transaction(owner, TransactionCreateRequest(
            account_id=account.id, amount=3000.0 + 50 * (i % 4), description="Salary",
            transaction_date=_utc(y, m, 2)))
        service.record_transaction(owner, TransactionCreateRequest(
            account_id=account.id, amount=-1800.0 - 35 * (i % 3), description="Rent and groceries",
            transaction_date=_utc(y, m, 20)))

    as_of = _utc(2025, 1, 15)  # history 2024-01 .. 2024-12; targets 2025-01 .. 2025-03
    live = service.get_forecast(owner, horizon=3, min_train=6, as_of=as_of)
    snap = service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(horizon=3, min_train=6),
                                            now=_utc(2025, 1, 15, 13), as_of=as_of)
    assert live.status == snap.status == "ok" and snap.target_count == 9
    assert [(t.target_month, t.point, t.lower, t.upper, t.interval_basis) for t in snap.targets if t.series == "net"] \
        == [(p.month, p.point, p.lower, p.upper, p.interval_basis) for p in live.net.points]
    assert snap.history_transaction_count == 24 and snap.history_latest_transaction_date == _utc(2024, 12, 20)
    assert {t.naive_point for t in snap.targets if t.series == "net"} == {3150.0 - 1870.0}  # 2024-12 at save time
    assert snap.notes == live.notes and snap.generated_at is not None
    with pytest.raises(ForecastSnapshotExists):
        service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(), now=_utc(2025, 1, 15, 20),
                                         as_of=as_of)

    restarted = FinanceService(PostgresFinanceStore(url))
    assert restarted.get_forecast_snapshot(owner, snap.id) == snap
    assert restarted.get_forecast_snapshot(other, snap.id) is None
    assert restarted.list_forecast_snapshots(owner).total == 1 and restarted.list_forecast_snapshots(other).total == 0

    early = restarted.get_forecast_accuracy(owner, now=_utc(2025, 1, 20))
    assert early.status == "nothing_to_evaluate_yet" and early.targets_pending == 9
    assert early.first_target_completes_at == datetime(2025, 2, 1, tzinfo=timezone.utc)

    report = restarted.get_forecast_accuracy(owner, now=_utc(2025, 5, 10))
    assert report.status == "ok" and report.targets_evaluated == 9 and report.targets_pending == 0
    assert report.disclaimer == fc.DISCLAIMER
    net = next(g for g in report.overall if g.series == "net")
    actual = {(2025, 1): 3000.0 + 50 * 0 - (1800.0 + 35 * 0), (2025, 2): 3050.0 - 1835.0, (2025, 3): 3100.0 - 1870.0}
    errors = [actual[fc_month] - p.point for fc_month, p in zip(sorted(actual), live.net.points)]
    assert net.evaluated == 3 and net.mae == round(sum(abs(e) for e in errors) / 3, 2)
    assert net.mean_error == round(sum(errors) / 3, 2)
    naive = 3150.0 - 1870.0  # 2024-12 (i = 11)
    assert net.naive_mae == round(sum(abs(actual[k] - naive) for k in actual) / 3, 2)
    assert [s.data_changed_since_snapshot for s in report.snapshots] == [False]

    later = restarted.record_transaction(owner, TransactionCreateRequest(
        account_id=account.id, amount=-40.0, description="Late import", transaction_date=_utc(2024, 11, 5)))
    assert [s.data_changed_since_snapshot for s in
            restarted.get_forecast_accuracy(owner, now=_utc(2025, 5, 10)).snapshots] == [True]
    restarted.delete_transaction(owner, later.id)
    assert [s.data_changed_since_snapshot for s in
            restarted.get_forecast_accuracy(owner, now=_utc(2025, 5, 10)).snapshots] == [False]
    # Move one transaction from 2024-03 to 2024-04: count, net total and latest date are unchanged.
    march = next(t for t in restarted.list_transactions(owner, limit=200).items
                 if t.transaction_date == _utc(2024, 3, 20))
    restarted.delete_transaction(owner, march.id)
    moved = restarted.record_transaction(owner, TransactionCreateRequest(
        account_id=account.id, amount=march.amount, description="Moved", transaction_date=_utc(2024, 4, 21)))
    assert [s.data_changed_since_snapshot for s in
            restarted.get_forecast_accuracy(owner, now=_utc(2025, 5, 10)).snapshots] == [True]
    restarted.delete_transaction(owner, moved.id)
    restarted.record_transaction(owner, TransactionCreateRequest(
        account_id=account.id, amount=march.amount, description=march.description,
        transaction_date=march.transaction_date))

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error):
                cur.execute("UPDATE jarvis_app.forecast_snapshot_targets SET point = 0 WHERE snapshot_id = %s",
                            (snap.id,))
            conn.rollback()
            with pytest.raises(psycopg.Error):
                cur.execute("UPDATE jarvis_app.forecast_snapshots SET horizon = 1 WHERE id = %s", (snap.id,))
            conn.rollback()

    assert restarted.delete_forecast_snapshot(other, snap.id) is False
    assert restarted.delete_forecast_snapshot(owner, snap.id) is True
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jarvis_app.forecast_snapshot_targets WHERE snapshot_id = %s",
                        (snap.id,))
            assert cur.fetchone()[0] == 0  # targets cascade with the snapshot
    assert restarted.get_forecast_accuracy(owner, now=_utc(2025, 5, 10)).status == "nothing_to_evaluate_yet"


def test_cap_one_per_day_and_constraints_in_postgresql(isolated_postgres, monkeypatch):
    import psycopg

    from jarvis.finance import service as service_module
    from jarvis.finance.models import ForecastSnapshotCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore
    from jarvis.finance.tracking import ForecastSnapshotExists

    monkeypatch.setattr(service_module, "FORECAST_SNAPSHOT_CAP", 2)
    owner, other = uuid4(), uuid4()
    url = isolated_postgres.database_url
    service = FinanceService(PostgresFinanceStore(url))
    kept_other = service.create_forecast_snapshot(other, ForecastSnapshotCreateRequest(), now=_utc(2025, 3, 1))
    made = [service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(), now=_utc(2025, 3, day))
            for day in (1, 2, 3)]  # all in the March as_of month
    forced = service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(force=True),
                                              now=_utc(2025, 3, 3, 18))
    assert made[0].status == "insufficient_history" and made[0].targets == []
    page = service.list_forecast_snapshots(owner)
    # Repeats within the as_of month go first (newest first); the month's first snapshot and the new one stay.
    assert page.total == 2 and [s.id for s in page.items] == [forced.id, made[0].id]
    assert service.get_forecast_snapshot(other, kept_other.id) is not None
    april = service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(), now=_utc(2025, 4, 1))
    may = service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(), now=_utc(2025, 5, 1))
    # forced (a March repeat) went when April arrived; with one snapshot per month the oldest (March) goes next.
    assert [s.id for s in service.list_forecast_snapshots(owner).items] == [may.id, april.id]

    monkeypatch.setattr(service_module, "FORECAST_SNAPSHOT_CAP", 500)
    for minute in range(4):
        service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(force=True),
                                         now=_utc(2025, 5, 1, 13 + minute))
    with pytest.raises(ForecastSnapshotExists) as caught:
        service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(force=True), now=_utc(2025, 5, 1, 20))
    assert caught.value.limit_reached is True and caught.value.existing_id == may.id
    with pytest.raises(ForecastSnapshotExists) as caught:
        service.create_forecast_snapshot(owner, ForecastSnapshotCreateRequest(), now=_utc(2025, 5, 1, 21))
    assert caught.value.limit_reached is False
    assert service.list_forecast_snapshots(owner).total == 6

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):  # targets require an 'ok' snapshot
                cur.execute(
                    """INSERT INTO jarvis_app.forecast_snapshots (id, owner_id, created_at, generated_at, as_of, horizon,
                           min_train, status, model_version, months_available, transactions_considered,
                           history_transaction_count, history_net_total, history_digest, truncated,
                           accounts_truncated, target_count)
                       VALUES (%s, %s, now(), now(), now(), 3, 6, 'insufficient_history', 'v', 0, 0, 0, 0, %s, false,
                               false, 3)""",
                    (uuid4(), owner, "a" * 64))
            conn.rollback()
            with pytest.raises(psycopg.errors.ForeignKeyViolation):  # a target must match its snapshot's owner
                cur.execute(
                    """INSERT INTO jarvis_app.forecast_snapshot_targets (snapshot_id, owner_id, series, target_month,
                           horizon, method, point, interval_basis) VALUES (%s, %s, 'net', '2025-04-01', 1, 'naive',
                           1.0, 'empirical')""",
                    (forced.id, other))
            conn.rollback()
