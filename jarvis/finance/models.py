"""Domain models and schemas for personal finance and analytical intelligence (v3.7)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    balance: float = 0.0
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


class TransactionCreateRequest(StrictSchema):
    account_id: UUID
    amount: float
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
