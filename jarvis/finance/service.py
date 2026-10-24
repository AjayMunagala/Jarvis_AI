"""Financial service coordinating accounts, categorized transactions, and net worth intelligence (v3.7).

Strict safety invariant: Contains ZERO broker, exchange, or order execution APIs.
Purely analytical personal finance intelligence.
"""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.finance.analytics import (
    categorize_transaction,
    compute_cash_flow_and_runway,
    compute_net_worth,
)
from jarvis.finance.forecasting import build_forecast
from jarvis.finance.models import (
    AccountCreateRequest,
    FinanceForecast,
    FinancialAccount,
    FinancialAccountPage,
    FinancialAnalyticsReport,
    FinancialSnapshot,
    FinancialTransaction,
    FinancialTransactionPage,
    TransactionCreateRequest,
)
from jarvis.finance.stores import FinanceStore

logger = logging.getLogger(__name__)


# v6.6: bounded reads for on-demand forecasts (newest first; the rest is reported as truncated).
FORECAST_PAGE_SIZE = 200
FORECAST_MAX_TRANSACTIONS = 5000
FORECAST_MAX_ACCOUNTS = 1000


class FinanceService:
    def __init__(self, store: FinanceStore, audit_service: Any = None) -> None:
        self.store = store
        self.audit_service = audit_service

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="finance",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record finance audit event: %s", exc)

    # ========================================================= Accounts

    def create_account(self, owner_id: UUID, req: AccountCreateRequest) -> FinancialAccount:
        now = datetime.now(timezone.utc)
        account = FinancialAccount(
            id=uuid4(),
            owner_id=owner_id,
            name=req.name,
            account_type=req.account_type,
            institution=req.institution,
            balance=round(req.balance, 2),
            currency=req.currency,
            metadata=req.metadata,
            created_at=now,
            updated_at=now,
        )
        self.store.save_account(account)
        self._audit(
            owner_id,
            "finance.account.create",
            f"finance:account:{account.id}",
            {"name": account.name, "type": account.account_type, "balance": account.balance},
        )
        return account

    def get_account(self, owner_id: UUID, account_id: UUID) -> FinancialAccount | None:
        return self.store.get_account(owner_id, account_id)

    def list_accounts(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> FinancialAccountPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_accounts(owner_id, limit=limit, offset=offset)
        return FinancialAccountPage(items=items, total=total)

    def delete_account(self, owner_id: UUID, account_id: UUID) -> bool:
        deleted = self.store.delete_account(owner_id, account_id)
        if deleted:
            self._audit(
                owner_id,
                "finance.account.delete",
                f"finance:account:{account_id}",
                {"deleted": True},
            )
        return deleted

    # ========================================================= Transactions

    def record_transaction(
        self, owner_id: UUID, req: TransactionCreateRequest
    ) -> FinancialTransaction:
        account = self.store.get_account(owner_id, req.account_id)
        if not account:
            raise ValueError(f"Account {req.account_id} not found.")

        now = datetime.now(timezone.utc)
        category = req.category or categorize_transaction(req.description, req.amount)
        tx_date = req.transaction_date or now

        tx = FinancialTransaction(
            id=uuid4(),
            account_id=req.account_id,
            owner_id=owner_id,
            amount=round(req.amount, 2),
            category=category,
            description=req.description,
            transaction_date=tx_date,
            created_at=now,
        )
        # v4.4: the transaction and its balance change are stored together, or not at all.
        self.store.record_transaction(tx)

        self._audit(
            owner_id,
            "finance.transaction.record",
            f"finance:tx:{tx.id}",
            {"account_id": str(tx.account_id), "amount": tx.amount, "category": tx.category},
        )
        return tx

    def list_transactions(
        self,
        owner_id: UUID,
        account_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> FinancialTransactionPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_transactions(
            owner_id, account_id=account_id, limit=limit, offset=offset
        )
        return FinancialTransactionPage(items=items, total=total)

    def delete_transaction(self, owner_id: UUID, tx_id: UUID) -> bool:
        # v4.4: delete and reverse together; a second delete finds nothing and reverses nothing.
        tx = self.store.delete_transaction_reversing(owner_id, tx_id)
        deleted = tx is not None
        if deleted:
            self._audit(
                owner_id,
                "finance.transaction.delete",
                f"finance:tx:{tx_id}",
                {"deleted": True, "reversed_amount": tx.amount},
            )
        return deleted

    # ========================================================= Snapshots & Reports

    def generate_snapshot(self, owner_id: UUID) -> FinancialSnapshot:
        accounts, _ = self.store.list_accounts(owner_id, limit=200, offset=0)
        transactions, _ = self.store.list_transactions(owner_id, limit=500, offset=0)

        total_assets, total_liabilities, net_worth = compute_net_worth(accounts)
        cash_flow, breakdown, runway = compute_cash_flow_and_runway(accounts, transactions)

        now = datetime.now(timezone.utc)
        snapshot = FinancialSnapshot(
            id=uuid4(),
            owner_id=owner_id,
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            net_worth=net_worth,
            monthly_cash_flow=cash_flow,
            breakdown_by_category=breakdown,
            projected_runway_months=runway,
            as_of_date=now,
        )
        self.store.save_snapshot(snapshot)
        self._audit(
            owner_id,
            "finance.snapshot.generate",
            f"finance:snapshot:{snapshot.id}",
            {"net_worth": net_worth, "runway_months": runway},
        )
        return snapshot

    def get_latest_snapshot(self, owner_id: UUID) -> FinancialSnapshot | None:
        return self.store.get_latest_snapshot(owner_id)

    def get_analytics_report(self, owner_id: UUID) -> FinancialAnalyticsReport:
        snapshot = self.get_latest_snapshot(owner_id) or self.generate_snapshot(owner_id)
        accounts, total_accs = self.store.list_accounts(owner_id, limit=1, offset=0)
        transactions, total_txs = self.store.list_transactions(owner_id, limit=1, offset=0)

        insights: list[str] = []
        if snapshot.net_worth >= 0:
            insights.append(f"Net worth is positive at ${snapshot.net_worth:,.2f}.")
        else:
            insights.append(f"Net worth is negative at ${snapshot.net_worth:,.2f}.")

        if snapshot.projected_runway_months is not None:
            insights.append(
                f"Estimated runway is {snapshot.projected_runway_months} months based on liquid cash reserves."
            )
        else:
            insights.append("Cash flow is positive; runway is sustainable.")

        top_expenses = sorted(
            [item for item in snapshot.breakdown_by_category.items() if item[1] < 0],
            key=lambda item: item[1],
        )
        if top_expenses:
            top_cat, top_amt = top_expenses[0]
            insights.append(f"Primary expense driver is '{top_cat}' at ${abs(top_amt):,.2f}.")

        summary = (
            f"Financial Health Summary: Net Worth ${snapshot.net_worth:,.2f} | "
            f"Monthly Cash Flow ${snapshot.monthly_cash_flow:,.2f} across {total_accs} accounts."
        )

        return FinancialAnalyticsReport(
            snapshot=snapshot,
            accounts_count=total_accs,
            transactions_count=total_txs,
            summary=summary,
            insights=insights,
        )

    # ========================================================= Forecasts (v6.6, analysis only)

    def _read_bounded(self, reader: Any, owner_id: UUID, cap: int) -> tuple[list[Any], bool]:
        items: dict[UUID, Any] = {}
        offset = 0
        total = 0
        while offset < cap:
            page, total = reader(owner_id, limit=min(FORECAST_PAGE_SIZE, cap - offset), offset=offset)
            for item in page:
                items.setdefault(item.id, item)
            if len(page) < FORECAST_PAGE_SIZE or offset + len(page) >= total:
                break
            offset += len(page)
        return list(items.values()), total > cap

    def get_forecast(
        self,
        owner_id: UUID,
        horizon: int = 3,
        min_train: int = 6,
        as_of: datetime | None = None,
    ) -> FinanceForecast:
        """Computed on demand from the owner's stored data; nothing is written or audited (a read)."""
        transactions, truncated = self._read_bounded(
            self.store.list_transactions, owner_id, FORECAST_MAX_TRANSACTIONS
        )
        accounts, _ = self._read_bounded(self.store.list_accounts, owner_id, FORECAST_MAX_ACCOUNTS)
        return build_forecast(
            accounts,
            transactions,
            horizon=horizon,
            min_train=min_train,
            truncated=truncated,
            as_of=as_of,
        )
