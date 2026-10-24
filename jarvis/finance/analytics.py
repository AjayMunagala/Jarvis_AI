"""Financial analytics, net worth calculation, runway forecasting, and auto-categorization (v3.7).

Strict safety invariant: Contains ZERO broker, exchange, or order execution APIs.
Purely analytical computation and intelligence.
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence

from jarvis.finance.models import FinancialAccount, FinancialTransaction

CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("housing", ["rent", "mortgage", "lease", "property", "hoa"]),
    ("food", ["grocery", "supermarket", "restaurant", "cafe", "coffee", "uber eats", "doordash"]),
    ("transport", ["gas", "fuel", "uber", "lyft", "metro", "transit", "parking", "airline"]),
    ("utilities", ["electric", "power", "water", "internet", "broadband", "phone", "verizon", "at&t"]),
    ("healthcare", ["pharmacy", "cvs", "walgreens", "hospital", "doctor", "dental", "clinic"]),
    ("entertainment", ["netflix", "spotify", "hulu", "steam", "movie", "theater", "game"]),
    ("income", ["payroll", "salary", "dividend", "interest", "bonus", "direct dep"]),
]


def categorize_transaction(description: str, amount: float) -> str:
    """Classifies transaction into a standard category using rule-based keyword matching."""
    desc_lower = description.lower()
    if amount > 0 and any(kw in desc_lower for kw in ["salary", "payroll", "dividend", "interest", "bonus"]):
        return "income"

    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in desc_lower for kw in keywords):
            return category

    return "income" if amount > 0 else "other"


def compute_net_worth(accounts: Sequence[FinancialAccount]) -> tuple[float, float, float]:
    """Computes (total_assets, total_liabilities, net_worth).

    Assets include checking, savings, and investment accounts.
    Liabilities include credit cards and loans.
    """
    total_assets = 0.0
    total_liabilities = 0.0

    for acc in accounts:
        if acc.account_type in ("checking", "savings", "investment"):
            if acc.balance >= 0:
                total_assets += acc.balance
            else:
                total_liabilities += abs(acc.balance)
        elif acc.account_type in ("credit", "loan"):
            total_liabilities += abs(acc.balance)

    net_worth = round(total_assets - total_liabilities, 2)
    return round(total_assets, 2), round(total_liabilities, 2), net_worth


def compute_cash_flow_and_runway(
    accounts: Sequence[FinancialAccount],
    transactions: Sequence[FinancialTransaction],
    window_days: int = 30,
    as_of: datetime | None = None,
) -> tuple[float, dict[str, float], float | None]:
    """Computes monthly cash flow, category breakdown, and estimated runway in months."""
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    cutoff = as_of - timedelta(days=window_days)

    def _to_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    recent_txs = [t for t in transactions if _to_utc(t.transaction_date) >= cutoff]
    monthly_cash_flow = round(sum(t.amount for t in recent_txs), 2)

    breakdown: dict[str, float] = {}
    for t in recent_txs:
        cat = t.category
        breakdown[cat] = round(breakdown.get(cat, 0.0) + t.amount, 2)

    # Liquid assets = checking + savings
    liquid_assets = sum(
        acc.balance for acc in accounts
        if acc.account_type in ("checking", "savings") and acc.balance > 0
    )

    # Runway in months if spending exceeds income
    projected_runway_months = None
    if monthly_cash_flow < 0 and liquid_assets > 0:
        monthly_burn = abs(monthly_cash_flow)
        projected_runway_months = round(liquid_assets / monthly_burn, 1)

    return monthly_cash_flow, breakdown, projected_runway_months
