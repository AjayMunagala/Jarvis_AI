"""Domain models and schemas for personal finance and analytical intelligence (v3.7)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FinancialAccount(StrictSchema):
    id: UUID
    owner_id: UUID
    name: str = Field(min_length=1, max_length=200)
    account_type: str = Field(pattern=r"^(checking|savings|credit|investment|loan)$")
    institution: str = Field(min_length=1, max_length=200)
    balance: float
    currency: str = Field(default="USD", min_length=1, max_length=10)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AccountCreateRequest(StrictSchema):
    name: str = Field(min_length=1, max_length=200)
    account_type: str = Field(pattern=r"^(checking|savings|credit|investment|loan)$")
    institution: str = Field(min_length=1, max_length=200)
    balance: float = Field(default=0.0, allow_inf_nan=False)
    currency: str = Field(default="USD", min_length=1, max_length=10)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FinancialTransaction(StrictSchema):
    id: UUID
    account_id: UUID
    owner_id: UUID
    amount: float
    category: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    transaction_date: datetime
    created_at: datetime
    import_batch_id: UUID | None = None  # v7.1: set when the transaction came from a CSV import


class TransactionCreateRequest(StrictSchema):
    account_id: UUID
    amount: float = Field(allow_inf_nan=False)  # v6.6: NaN/inf would corrupt balances and forecasts
    category: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    transaction_date: datetime | None = None


class FinancialSnapshot(StrictSchema):
    id: UUID
    owner_id: UUID
    total_assets: float
    total_liabilities: float
    net_worth: float
    monthly_cash_flow: float
    breakdown_by_category: dict[str, float] = Field(default_factory=dict)
    projected_runway_months: float | None = None
    as_of_date: datetime


class FinancialAnalyticsReport(StrictSchema):
    snapshot: FinancialSnapshot
    accounts_count: int = Field(ge=0)
    transactions_count: int = Field(ge=0)
    summary: str
    insights: list[str] = Field(default_factory=list)


class FinancialAccountPage(StrictSchema):
    items: list[FinancialAccount]
    total: int = Field(ge=0)


class FinancialTransactionPage(StrictSchema):
    items: list[FinancialTransaction]
    total: int = Field(ge=0)


# ============================================================= v6.6 forecasts (analysis only)


class MonthlyFlow(StrictSchema):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    income: float
    spending: float
    net: float


class ForecastDataSummary(StrictSchema):
    months_available: int = Field(ge=0)
    months_needed: int = Field(ge=1)
    first_month: str | None = None
    last_month: str | None = None
    transactions_considered: int = Field(ge=0)
    truncated: bool
    accounts_truncated: bool = False
    excluded_invalid: int = Field(default=0, ge=0)
    excluded_current_month: int = Field(ge=0)
    excluded_future_dated: int = Field(ge=0)
    excluded_older_than_window: int = Field(ge=0)
    history: list[MonthlyFlow] = Field(default_factory=list)


class MethodBacktest(StrictSchema):
    method: str
    folds: int = Field(ge=0)
    mae: float
    rmse: float
    mape: float | None = None
    mape_note: str | None = None
    interval_coverage: float | None = None
    coverage_folds: int = Field(ge=0)
    coverage_note: str | None = None


class ForecastPoint(StrictSchema):
    month: str
    horizon: int = Field(ge=1)
    point: float
    lower: float | None = None
    upper: float | None = None
    interval_basis: str


class SeriesForecast(StrictSchema):
    series: str
    chosen_method: str
    chosen_parameters: dict[str, float] = Field(default_factory=dict)
    naive_baseline_mae: float
    chosen_mae: float
    beats_naive: bool
    comparison: str
    evaluation_origins: int = Field(ge=0)
    evaluation_start_month: str
    methods: list[MethodBacktest]
    interval_level: float
    empirical_coverage: float | None = None
    coverage_folds: int = Field(ge=0)
    coverage_note: str | None = None
    interval_note: str | None = None
    points: list[ForecastPoint]


class BalancePoint(StrictSchema):
    month: str
    horizon: int = Field(ge=1)
    point: float
    lower: float | None = None
    upper: float | None = None


class BalanceScenario(StrictSchema):
    label: str
    starting_liquid_balance: float
    liquid_accounts: int = Field(ge=0)
    current_month_net_to_date: float
    future_dated_net: float = 0.0
    points: list[BalancePoint]
    first_month_lower_bound_below_zero: str | None = None
    note: str


class FinanceForecast(StrictSchema):
    status: str = Field(pattern=r"^(ok|insufficient_history)$")
    generated_at: datetime
    as_of: datetime
    horizon: int = Field(ge=1, le=12)
    min_train: int = Field(ge=1)
    disclaimer: str
    data: ForecastDataSummary
    net: SeriesForecast | None = None
    income: SeriesForecast | None = None
    spending: SeriesForecast | None = None
    balance_scenario: BalanceScenario | None = None
    notes: list[str] = Field(default_factory=list)


# ============================================================= v7.1 bank CSV imports

ImportDateFormat = Literal["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "YYYY/MM/DD", "DD.MM.YYYY",
                           "DD/MM/YY", "DD-Mon-YYYY", "DD Mon YYYY", "DD-Mon-YY"]
ImportRowStatus = Literal["ok", "warning", "error", "duplicate"]


class ImportMapping(StrictSchema):
    """Which header columns hold what. Either one signed amount column, or a debit and a credit column."""

    date_column: str = Field(min_length=1, max_length=200)
    date_format: ImportDateFormat
    description_column: str = Field(min_length=1, max_length=200)
    amount_column: str | None = Field(default=None, min_length=1, max_length=200)
    debit_column: str | None = Field(default=None, min_length=1, max_length=200)
    credit_column: str | None = Field(default=None, min_length=1, max_length=200)
    category_column: str | None = Field(default=None, min_length=1, max_length=200)
    decimal_separator: Literal[".", ","] = "."
    delimiter: Literal[",", ";", "\t", "|"] | None = None  # None: detected from the file
    # Offset of times written without one (for example 330 for IST); times with an offset use their own.
    utc_offset_minutes: int = Field(default=0, ge=-720, le=840)

    @model_validator(mode="after")
    def _one_amount_layout(self) -> "ImportMapping":
        split = self.debit_column is not None or self.credit_column is not None
        if self.amount_column is not None and split:
            raise ValueError("Use either amount_column or debit_column and credit_column, not both.")
        if self.amount_column is None and (self.debit_column is None or self.credit_column is None):
            raise ValueError("Map amount_column, or both debit_column and credit_column.")
        names = [n.casefold() for n in (self.date_column, self.description_column, self.amount_column,
                                        self.debit_column, self.credit_column, self.category_column) if n]
        if len(names) != len(set(names)):
            raise ValueError("Each mapped column must be a different column.")
        return self


class ImportPreviewRequest(StrictSchema):
    account_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    # Longer than a 2 MB file can produce is refused as too large (413) by the service, before decoding.
    data_base64: str = Field(min_length=1)
    mapping: ImportMapping


class ImportRowResult(StrictSchema):
    row_number: int = Field(ge=1)  # data row (the header and blank rows are not counted)
    line_number: int = Field(ge=1)  # line in the file where the row starts
    status: ImportRowStatus
    transaction_date: datetime | None = None
    amount: float | None = None
    description: str | None = None
    category: str | None = None
    messages: list[str] = Field(default_factory=list)


class ImportCounts(StrictSchema):
    rows: int = Field(ge=0)
    valid: int = Field(ge=0)
    ok: int = Field(ge=0)
    warnings: int = Field(ge=0)
    errors: int = Field(ge=0)
    duplicates_in_file: int = Field(ge=0)
    duplicates_existing: int = Field(ge=0)
    blank_rows_skipped: int = Field(ge=0)


class ImportPreview(StrictSchema):
    id: UUID
    account_id: UUID
    filename: str
    created_at: datetime
    expires_at: datetime
    encoding: str
    delimiter: str
    columns: list[str]
    counts: ImportCounts
    rows: list[ImportRowResult]
    issues: list[ImportRowResult]
    total_valid_amount: float
    total_valid_credits: float
    total_valid_debits: float
    date_from: datetime | None = None
    date_to: datetime | None = None
    duplicate_check_complete: bool = True
    file_warnings: list[str] = Field(default_factory=list)


class ImportCommitRequest(StrictSchema):
    include_warnings: bool = True
    # Repeats within the file are kept by default (fingerprints still make re-imports add nothing);
    # skip_duplicates=true skips them. Rows already stored for the account are always skipped.
    skip_duplicates: bool = False


class ImportCommitResult(StrictSchema):
    batch_id: UUID | None = None
    account_id: UUID
    inserted: int = Field(ge=0)
    skipped_errors: int = Field(ge=0)
    skipped_warnings: int = Field(ge=0)
    skipped_duplicates_in_file: int = Field(ge=0)
    skipped_duplicates_existing: int = Field(ge=0)
    balance_change: float
    account_balance: float | None = None


class FinancialImportBatch(StrictSchema):
    id: UUID
    owner_id: UUID
    account_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    rows_in_file: int = Field(ge=0)
    rows_inserted: int = Field(ge=0)
    rows_skipped: int = Field(ge=0)
    created_at: datetime


class FinancialImportBatchPage(StrictSchema):
    items: list[FinancialImportBatch]
    total: int = Field(ge=0)


class ImportUndoResult(StrictSchema):
    batch_id: UUID
    account_id: UUID
    removed: int = Field(ge=0)
    balance_change: float


# ============================================================= v7.2 forecast snapshots and accuracy (analysis only)

ForecastSeriesName = Literal["net", "income", "spending"]


class ForecastSnapshotCreateRequest(StrictSchema):
    horizon: int = Field(default=3, ge=1, le=12)
    min_train: int = Field(default=6, ge=3, le=24)
    # At most one snapshot per owner per UTC day without force; with force at most
    # FORECAST_SNAPSHOTS_PER_DAY_MAX (5) per UTC day in total (avoids accidental floods).
    force: bool = False


class ForecastSnapshotTarget(StrictSchema):
    """One forecast month of one series, as recomputed by the forecast code when the snapshot was saved."""

    series: ForecastSeriesName
    target_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    horizon: int = Field(ge=1, le=12)  # months ahead of the snapshot's as_of month (1 = the as_of month)
    method: str = Field(min_length=1, max_length=64)
    point: float = Field(allow_inf_nan=False)
    lower: float | None = Field(default=None, allow_inf_nan=False)
    upper: float | None = Field(default=None, allow_inf_nan=False)
    interval_basis: str = Field(min_length=1, max_length=64)
    # Naive baseline as known at save time: the series value of the last complete month of the history used.
    naive_point: float | None = Field(default=None, allow_inf_nan=False)


class ForecastSnapshotSummary(StrictSchema):
    id: UUID
    owner_id: UUID
    created_at: datetime  # when the snapshot was saved (UTC)
    generated_at: datetime  # when the forecast code ran for it (UTC)
    as_of: datetime
    horizon: int = Field(ge=1, le=12)
    min_train: int = Field(ge=3, le=24)
    status: Literal["ok", "insufficient_history"]
    model_version: str = Field(min_length=1, max_length=32)
    months_available: int = Field(ge=0)
    transactions_considered: int = Field(ge=0)
    # Data fingerprint of the history the forecast used (transactions dated before the as_of month): count,
    # latest date, net total, and an order-independent per-month digest (month, cents sum, count).
    history_transaction_count: int = Field(ge=0)
    history_latest_transaction_date: datetime | None = None
    history_net_total: float = Field(allow_inf_nan=False)
    history_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool
    accounts_truncated: bool
    target_count: int = Field(ge=0)


class ForecastSnapshot(ForecastSnapshotSummary):
    targets: list[ForecastSnapshotTarget] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # the forecast's notes at save time
    disclaimer: str


class ForecastSnapshotPage(StrictSchema):
    items: list[ForecastSnapshotSummary]
    total: int = Field(ge=0)
    disclaimer: str


class ForecastAccuracyGroup(StrictSchema):
    series: ForecastSeriesName
    horizon: int | None = None  # set in by_horizon
    method: str | None = None  # set in by_method
    evaluated: int = Field(ge=0)  # distinct forecasts scored (repeats of the same forecast collapsed)
    raw_evaluations: int = Field(ge=0)  # before collapsing repeats
    mae: float
    mean_error: float  # bias: mean of (actual - forecast)
    mape: float | None = None
    mape_note: str | None = None
    interval_level: float
    interval_coverage: float | None = None
    coverage_targets: int = Field(ge=0)
    naive_mae: float
    naive_mean_error: float
    beats_naive: bool
    comparison: str


class ForecastSnapshotEvaluation(StrictSchema):
    snapshot_id: UUID
    created_at: datetime
    as_of: datetime
    model_version: str
    targets_evaluated: int = Field(ge=0)  # completed targets with actuals (raw)
    targets_counted: int = Field(ge=0)  # of those, not a repeat of an earlier snapshot's identical forecast
    targets_pending: int = Field(ge=0)
    targets_without_actuals: int = Field(ge=0)
    # True when transactions in the months before as_of were added, removed, moved or imported after the
    # snapshot; None when it cannot be told because a transaction read was truncated.
    data_changed_since_snapshot: bool | None = None


class ForecastAccuracyReport(StrictSchema):
    status: Literal["ok", "nothing_to_evaluate_yet", "no_actuals_available"]
    generated_at: datetime
    current_month: str
    disclaimer: str
    model_version: str
    interval_level: float
    snapshots_considered: int = Field(ge=0)
    targets_evaluated: int = Field(ge=0)  # distinct forecasts scored
    raw_evaluations: int = Field(default=0, ge=0)  # before collapsing repeats of the same forecast
    duplicate_evaluations_collapsed: int = Field(default=0, ge=0)
    targets_pending: int = Field(ge=0)
    targets_without_actuals: int = Field(ge=0)
    first_pending_month: str | None = None
    first_target_completes_at: datetime | None = None
    data_truncated: bool = False
    overall: list[ForecastAccuracyGroup] = Field(default_factory=list)
    by_horizon: list[ForecastAccuracyGroup] = Field(default_factory=list)
    by_method: list[ForecastAccuracyGroup] = Field(default_factory=list)
    snapshots: list[ForecastSnapshotEvaluation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
