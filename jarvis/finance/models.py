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

ImportDateFormat = Literal["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "YYYY/MM/DD", "DD.MM.YYYY"]
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
    data_base64: str = Field(min_length=1, max_length=2_796_204)  # a 2 MB file as base64
    mapping: ImportMapping


class ImportRowResult(StrictSchema):
    row_number: int = Field(ge=1)
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
    # In-file repeats may be inserted with skip_duplicates=false; rows already stored are always skipped.
    skip_duplicates: bool = True


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
