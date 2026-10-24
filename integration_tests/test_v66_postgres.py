"""v6.6 live PostgreSQL validation: a forecast is computed from ~18 months of stored transactions."""

from datetime import datetime, timezone
from uuid import uuid4


def test_forecast_from_stored_transactions(isolated_postgres):
    from jarvis.finance import forecasting as fc
    from jarvis.finance.models import AccountCreateRequest, TransactionCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore

    owner, other = uuid4(), uuid4()
    service = FinanceService(PostgresFinanceStore(isolated_postgres.database_url))
    account = service.create_account(owner, AccountCreateRequest(
        name="Live checking", account_type="checking", institution="Test", balance=5000.0))
    current = fc._month_of(datetime.now(timezone.utc))
    months = [fc._shift(current, -i) for i in range(18, 0, -1)]
    for index, (year, month) in enumerate(months):
        for amount, day in ((3000.0, 1), (-2200.0 - 50 * (index % 4), 15)):
            service.record_transaction(owner, TransactionCreateRequest(
                account_id=account.id, amount=amount, description="live salary" if amount > 0 else "live rent",
                transaction_date=datetime(year, month, day, 12, tzinfo=timezone.utc)))

    result = service.get_forecast(owner, horizon=3, min_train=6)
    assert result.status == "ok" and result.data.truncated is False
    assert result.data.months_available == 18 and result.data.transactions_considered == 36
    assert result.data.first_month == fc.month_label(months[0])
    assert len(result.net.points) == 3 and result.net.evaluation_origins >= 3
    assert any(m.method == "naive" for m in result.net.methods)
    assert result.balance_scenario.liquid_accounts == 1
    assert result.disclaimer == fc.DISCLAIMER

    restarted = FinanceService(PostgresFinanceStore(isolated_postgres.database_url))
    assert restarted.get_forecast(owner).data.months_available == 18
    isolated = restarted.get_forecast(other)
    assert isolated.status == "insufficient_history" and isolated.data.months_available == 0


def test_transactions_with_equal_dates_page_without_gaps_or_repeats(isolated_postgres):
    from jarvis.finance.models import AccountCreateRequest, TransactionCreateRequest
    from jarvis.finance.service import FinanceService
    from jarvis.finance.stores import PostgresFinanceStore

    owner = uuid4()
    store = PostgresFinanceStore(isolated_postgres.database_url)
    service = FinanceService(store)
    account = service.create_account(owner, AccountCreateRequest(
        name="Ties", account_type="checking", institution="Test", balance=0.0))
    same_time = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    for _ in range(7):
        service.record_transaction(owner, TransactionCreateRequest(
            account_id=account.id, amount=-1.0, description="same instant", transaction_date=same_time))
    seen = []
    for offset in range(0, 7, 2):
        page, _ = store.list_transactions(owner, limit=2, offset=offset)
        seen.extend(t.id for t in page)
    assert len(seen) == len(set(seen)) == 7
