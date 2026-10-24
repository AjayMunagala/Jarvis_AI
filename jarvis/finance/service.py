"""Financial service coordinating accounts, categorized transactions, and net worth intelligence (v3.7).

Strict safety invariant: Contains ZERO broker, exchange, or order execution APIs.
Purely analytical personal finance intelligence.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.finance.analytics import (
    categorize_transaction,
    compute_cash_flow_and_runway,
    compute_net_worth,
)
from jarvis.finance.forecasting import DISCLAIMER, _utc, build_forecast
from jarvis.finance.importing import (
    DELIMITERS,
    MAX_EXISTING_SCAN,
    PREVIEW_ROWS,
    ImportAccountNotFound,
    ImportPreviewCache,
    ImportPreviewNotFound,
    ParsedRow,
    PreviewEntry,
    assign_keys,
    check_day_month_order,
    clean_filename,
    date_window,
    decode_base64,
    existing_key_counts,
    flag_outliers,
    mark_duplicates,
    parse_row,
    read_table,
    resolve_columns,
    row_messages,
)
from jarvis.finance.models import (
    AccountCreateRequest,
    FinanceForecast,
    FinancialAccount,
    FinancialAccountPage,
    FinancialAnalyticsReport,
    FinancialImportBatch,
    FinancialImportBatchPage,
    FinancialSnapshot,
    FinancialTransaction,
    FinancialTransactionPage,
    ImportCommitRequest,
    ImportCommitResult,
    ImportCounts,
    ImportPreview,
    ImportPreviewRequest,
    ImportRowResult,
    ImportUndoResult,
    ForecastAccuracyReport,
    ForecastSnapshot,
    ForecastSnapshotCreateRequest,
    ForecastSnapshotPage,
    TransactionCreateRequest,
)
from jarvis.finance.stores import FinanceStore, ImportOutcome
from jarvis.finance.tracking import (
    ForecastSnapshotExists,
    HistoryIndex,
    evaluate_accuracy,
    history_cutoff,
    month_start,
    snapshot_from_forecast,
)

logger = logging.getLogger(__name__)


# v6.6: bounded single reads for on-demand forecasts (newest first; the rest is reported as truncated).
FORECAST_MAX_TRANSACTIONS = 5000
FORECAST_MAX_ACCOUNTS = 1000
# v7.2: stored forecast snapshots per owner; the oldest are deleted first beyond this.
FORECAST_SNAPSHOT_CAP = 500
# v7.2: snapshots per owner per UTC day: one without force, at most this many in total with force.
FORECAST_SNAPSHOTS_PER_DAY_MAX = 5


class FinanceService:
    def __init__(
        self, store: FinanceStore, audit_service: Any = None, import_previews: ImportPreviewCache | None = None
    ) -> None:
        self.store = store
        self.audit_service = audit_service
        # v7.1: CSV import previews are held in memory only (TTL and caps; see ImportPreviewCache).
        self.import_previews = import_previews or ImportPreviewCache()

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
        # One query of cap + 1 rows: no offset paging (which can skip or repeat rows under concurrent
        # writes) and no separate count; the extra row only signals truncation.
        items, _ = reader(owner_id, limit=cap + 1, offset=0)
        return list(items[:cap]), len(items) > cap

    def get_forecast(
        self,
        owner_id: UUID,
        horizon: int = 3,
        min_train: int = 6,
        as_of: datetime | None = None,
    ) -> FinanceForecast:
        """Computed on demand from the owner's stored data; nothing is written or audited (a read)."""
        return self._forecast_with_inputs(owner_id, horizon, min_train, as_of)[0]

    def _forecast_with_inputs(
        self, owner_id: UUID, horizon: int, min_train: int, as_of: datetime | None
    ) -> tuple[FinanceForecast, list[FinancialTransaction]]:
        transactions, truncated = self._read_bounded(
            self.store.list_transactions, owner_id, FORECAST_MAX_TRANSACTIONS
        )
        accounts, accounts_truncated = self._read_bounded(
            self.store.list_accounts, owner_id, FORECAST_MAX_ACCOUNTS
        )
        forecast = build_forecast(
            accounts,
            transactions,
            horizon=horizon,
            min_train=min_train,
            truncated=truncated,
            accounts_truncated=accounts_truncated,
            as_of=as_of,
        )
        return forecast, transactions

    # ========================================================= Forecast snapshots and accuracy (v7.2)
    # TODO(v7.x): a scheduler job type could call create_forecast_snapshot monthly; until then the owner calls
    # POST /finance/forecast/snapshots (the one-per-UTC-day guard keeps repeated calls harmless).

    def create_forecast_snapshot(
        self,
        owner_id: UUID,
        req: ForecastSnapshotCreateRequest,
        now: datetime | None = None,
        as_of: datetime | None = None,
    ) -> ForecastSnapshot:
        """Recompute the forecast at save time with the same code and read as GET /finance/forecast, and store
        it immutably with its timestamps (created_at, generated_at) and notes.

        Raises ForecastSnapshotExists when one was already taken this UTC day and ``req.force`` is false, or
        when FORECAST_SNAPSHOTS_PER_DAY_MAX were already taken this UTC day.
        """
        now = _utc(now or datetime.now(timezone.utc))
        as_of = _utc(as_of or now)
        if as_of > now:
            raise ValueError("as_of cannot be in the future")
        forecast, transactions = self._forecast_with_inputs(owner_id, req.horizon, req.min_train, as_of)
        fingerprint = HistoryIndex(transactions).before(history_cutoff(as_of))
        snapshot = snapshot_from_forecast(forecast, snapshot_id=uuid4(), owner_id=owner_id, created_at=now,
                                          fingerprint=fingerprint)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        outcome = self.store.save_forecast_snapshot(snapshot, day_start=day_start,
                                                    day_end=day_start + timedelta(days=1), force=req.force,
                                                    cap=FORECAST_SNAPSHOT_CAP,
                                                    day_max=FORECAST_SNAPSHOTS_PER_DAY_MAX)
        if outcome.snapshot is None:
            raise ForecastSnapshotExists(outcome.existing_id, limit_reached=outcome.limit_reached)
        self._audit(owner_id, "finance.forecast_snapshot.create", f"finance:forecast_snapshot:{snapshot.id}",
                    {"status": snapshot.status, "horizon": snapshot.horizon, "targets": snapshot.target_count,
                     "forced": req.force, "evicted": outcome.evicted})
        return outcome.snapshot

    def list_forecast_snapshots(self, owner_id: UUID, limit: int = 50, offset: int = 0) -> ForecastSnapshotPage:
        items, total = self.store.list_forecast_snapshots(owner_id, limit=max(1, min(limit, 200)),
                                                          offset=max(0, offset))
        return ForecastSnapshotPage(items=items, total=total, disclaimer=DISCLAIMER)

    def get_forecast_snapshot(self, owner_id: UUID, snapshot_id: UUID) -> ForecastSnapshot | None:
        return self.store.get_forecast_snapshot(owner_id, snapshot_id)

    def delete_forecast_snapshot(self, owner_id: UUID, snapshot_id: UUID) -> bool:
        deleted = self.store.delete_forecast_snapshot(owner_id, snapshot_id)
        if deleted:
            self._audit(owner_id, "finance.forecast_snapshot.delete", f"finance:forecast_snapshot:{snapshot_id}",
                        {"deleted": True})
        return deleted

    def get_forecast_accuracy(self, owner_id: UUID, now: datetime | None = None) -> ForecastAccuracyReport:
        """Score stored targets whose month is complete against actuals from current data (a read)."""
        now = _utc(now or datetime.now(timezone.utc))
        rows = self.store.forecast_targets_for_evaluation(owner_id, month_start((now.year, now.month)))
        transactions, truncated = self._read_bounded(
            self.store.list_transactions, owner_id, FORECAST_MAX_TRANSACTIONS
        )
        return evaluate_accuracy(rows, transactions, truncated=truncated, now=now)

    # ========================================================= Bank CSV imports (v7.1)

    def _mark_existing_duplicates(self, owner_id: UUID, account_id: UUID, rows: list[ParsedRow]) -> bool:
        """Flag rows already stored for the account; False when the stored history was too large to scan fully."""
        window = date_window(rows)
        if window is None:
            mark_duplicates(rows, {})
            return True
        stored = self.store.list_transaction_keys(owner_id, account_id, window[0], window[1], MAX_EXISTING_SCAN + 1)
        mark_duplicates(rows, existing_key_counts(account_id, stored[:MAX_EXISTING_SCAN]))
        return len(stored) <= MAX_EXISTING_SCAN

    def preview_import(
        self, owner_id: UUID, req: ImportPreviewRequest, now: datetime | None = None
    ) -> ImportPreview:
        """Parse and check a bank CSV file; only the in-memory preview is kept. Nothing is written or audited."""
        account = self.store.get_account(owner_id, req.account_id)
        if account is None:
            raise ImportAccountNotFound("The financial account was not found.")
        table = read_table(decode_base64(req.data_base64), req.mapping.delimiter)
        columns = resolve_columns(table.header, req.mapping)
        order_warnings = check_day_month_order(table, columns["date_column"], req.mapping.date_format)
        now = now or datetime.now(timezone.utc)
        rows = [parse_row(number, cells, columns, req.mapping, len(table.header), now.date(), line_number)
                for number, line_number, cells in table.rows]
        flag_outliers(rows)
        assign_keys(owner_id, account.id, rows)
        complete = self._mark_existing_duplicates(owner_id, account.id, rows)
        warnings = list(table.warnings) + order_warnings
        if not complete:
            warnings.append("The account has too many stored transactions in this date range to check them all "
                            "for duplicates; re-imports of the same rows are still prevented by fingerprints.")
        entry = PreviewEntry(
            id=uuid4(), owner_id=owner_id, account_id=account.id, filename=clean_filename(req.filename), rows=rows,
            created_at=now, expires_at=now + timedelta(seconds=self.import_previews.ttl_seconds),
            created_monotonic=self.import_previews.clock(),
            meta={"encoding": table.encoding, "delimiter": DELIMITERS[table.delimiter],
                  "columns": table.header, "blank_rows": table.blank_rows, "warnings": warnings,
                  "complete": complete},
        )
        self.import_previews.put(entry)
        return self._preview_response(entry)

    @staticmethod
    def _row_result(row: ParsedRow) -> ImportRowResult:
        return ImportRowResult(
            row_number=row.row_number, line_number=max(1, row.line_number), status=row.status,
            transaction_date=row.transaction_date,
            amount=float(row.amount) if row.amount is not None else None, description=row.description,
            category=row.category, messages=row_messages(row),
        )

    def _preview_response(self, entry: PreviewEntry) -> ImportPreview:
        rows = entry.rows
        valid = [row for row in rows if row.valid]
        dated = [row.transaction_date for row in rows if row.key is not None and row.transaction_date is not None]
        credits = sum((row.amount for row in valid if row.amount > 0), Decimal("0"))
        debits = sum((row.amount for row in valid if row.amount < 0), Decimal("0"))
        counts = ImportCounts(
            rows=len(rows), valid=len(valid),
            ok=sum(1 for row in rows if row.status == "ok"),
            warnings=sum(1 for row in rows if row.status == "warning"),
            errors=sum(1 for row in rows if row.status == "error"),
            duplicates_in_file=sum(1 for row in rows if row.duplicate == "file"),
            duplicates_existing=sum(1 for row in rows if row.duplicate == "existing"),
            blank_rows_skipped=entry.meta["blank_rows"],
        )
        return ImportPreview(
            id=entry.id, account_id=entry.account_id, filename=entry.filename, created_at=entry.created_at,
            expires_at=entry.expires_at, encoding=entry.meta["encoding"], delimiter=entry.meta["delimiter"],
            columns=entry.meta["columns"], counts=counts,
            rows=[self._row_result(row) for row in rows[:PREVIEW_ROWS]],
            issues=[self._row_result(row) for row in rows if row.status != "ok"][:PREVIEW_ROWS],
            total_valid_amount=float(credits + debits), total_valid_credits=float(credits),
            total_valid_debits=float(debits), date_from=min(dated) if dated else None,
            date_to=max(dated) if dated else None, duplicate_check_complete=entry.meta["complete"],
            file_warnings=entry.meta["warnings"],
        )

    def commit_import(
        self, owner_id: UUID, preview_id: UUID, req: ImportCommitRequest
    ) -> ImportCommitResult:
        """Insert the preview's valid rows and their balance change in one transaction. The preview is consumed;
        if storing fails nothing is written and the preview is put back so the owner can retry."""
        entry = self.import_previews.take(owner_id, preview_id)
        if entry is None:
            raise ImportPreviewNotFound("The import preview was not found or has expired.")
        try:
            if self.store.get_account(owner_id, entry.account_id) is None:
                raise ImportAccountNotFound("The financial account was not found.")
            self._mark_existing_duplicates(owner_id, entry.account_id, entry.rows)  # fresh at commit time
            now = datetime.now(timezone.utc)
            batch_id = uuid4()
            skipped = {"errors": 0, "warnings": 0, "file": 0, "existing": 0}
            to_store: list[tuple[FinancialTransaction, str]] = []
            for row in entry.rows:
                if row.errors or row.amount is None or row.fingerprint is None:
                    skipped["errors"] += 1
                elif row.duplicate == "existing":
                    skipped["existing"] += 1
                elif row.duplicate == "file" and req.skip_duplicates:
                    skipped["file"] += 1
                elif row.status == "warning" and not req.include_warnings:  # includes kept repeats
                    skipped["warnings"] += 1
                else:
                    amount = float(row.amount)
                    to_store.append((FinancialTransaction(
                        id=uuid4(), account_id=entry.account_id, owner_id=owner_id, amount=amount,
                        category=row.category or categorize_transaction(row.description, amount),
                        description=row.description, transaction_date=row.transaction_date, created_at=now,
                        import_batch_id=batch_id,
                    ), row.fingerprint))
            batch = FinancialImportBatch(
                id=batch_id, owner_id=owner_id, account_id=entry.account_id, filename=entry.filename,
                rows_in_file=len(entry.rows), rows_inserted=0, rows_skipped=len(entry.rows) - len(to_store),
                created_at=now,
            )
            try:
                outcome = self.store.commit_import(batch, to_store) if to_store else ImportOutcome(None, 0, 0, 0.0)
            except ValueError:
                if self.store.get_account(owner_id, entry.account_id) is None:
                    raise ImportAccountNotFound("The financial account was not found.") from None
                raise
        except Exception:
            self.import_previews.restore(entry)
            raise
        account = self.store.get_account(owner_id, entry.account_id)
        result = ImportCommitResult(
            batch_id=outcome.batch.id if outcome.batch is not None else None, account_id=entry.account_id,
            inserted=outcome.inserted, skipped_errors=skipped["errors"], skipped_warnings=skipped["warnings"],
            skipped_duplicates_in_file=skipped["file"],
            # Rows whose fingerprint was stored between preview and commit count as already stored.
            skipped_duplicates_existing=skipped["existing"] + outcome.conflicts,
            balance_change=round(outcome.balance_change, 2),
            account_balance=account.balance if account is not None else None,
        )
        # Counts only: never descriptions, amounts or the file name (all file-derived).
        self._audit(owner_id, "finance.import.commit",
                    f"finance:import:{result.batch_id or preview_id}",
                    {"account_id": str(entry.account_id), "rows_in_file": len(entry.rows),
                     "inserted": result.inserted, "skipped_errors": result.skipped_errors,
                     "skipped_warnings": result.skipped_warnings,
                     "skipped_duplicates_in_file": result.skipped_duplicates_in_file,
                     "skipped_duplicates_existing": result.skipped_duplicates_existing})
        return result

    def list_import_batches(self, owner_id: UUID, limit: int = 50, offset: int = 0) -> FinancialImportBatchPage:
        items, total = self.store.list_import_batches(owner_id, limit=max(1, min(limit, 200)),
                                                      offset=max(0, offset))
        return FinancialImportBatchPage(items=items, total=total)

    def undo_import(self, owner_id: UUID, batch_id: UUID) -> ImportUndoResult | None:
        """Remove exactly the batch's transactions and reverse their balance effect, atomically."""
        outcome = self.store.undo_import(owner_id, batch_id)
        if outcome is None:
            return None
        self._audit(owner_id, "finance.import.undo", f"finance:import:{batch_id}",
                    {"account_id": str(outcome.batch.account_id), "removed": outcome.removed})
        return ImportUndoResult(batch_id=batch_id, account_id=outcome.batch.account_id, removed=outcome.removed,
                                balance_change=round(outcome.balance_change, 2))
