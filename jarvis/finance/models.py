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
