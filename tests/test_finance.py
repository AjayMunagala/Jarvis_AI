import ast
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import pytest

from jarvis.finance.analytics import (
    categorize_transaction,
    compute_cash_flow_and_runway,
    compute_net_worth,
)
from jarvis.finance.models import (
    AccountCreateRequest,
    FinancialAccount,
    FinancialTransaction,
    TransactionCreateRequest,
)
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore

TEST_OWNER = uuid4()


def test_expense_categorization():
    assert categorize_transaction("Monthly Apartment Rent Payment", -1500.0) == "housing"
    assert categorize_transaction("Whole Foods Grocery Market", -85.50) == "food"
    assert categorize_transaction("Uber Ride to Airport", -42.0) == "transport"
    assert categorize_transaction("ConEd Electric Utility Bill", -65.20) == "utilities"
    assert categorize_transaction("CVS Pharmacy Prescription", -25.0) == "healthcare"
    assert categorize_transaction("Netflix Subscription", -19.99) == "entertainment"
    assert categorize_transaction("Acme Corp Payroll Direct Dep", 4500.0) == "income"
    assert categorize_transaction("Random Unidentified Vendor", -12.0) == "other"


def test_net_worth_and_runway_calculations():
    now = datetime.now(timezone.utc)
    acc1 = FinancialAccount(
        id=uuid4(),
        owner_id=TEST_OWNER,
        name="Checking",
        account_type="checking",
        institution="Bank of America",
        balance=10000.0,
        currency="USD",
        metadata={},
        created_at=now,
        updated_at=now,
    )
    acc2 = FinancialAccount(
        id=uuid4(),
        owner_id=TEST_OWNER,
        name="Brokerage Index",
        account_type="investment",
        institution="Vanguard",
        balance=25000.0,
        currency="USD",
        metadata={},
        created_at=acc1.created_at,
        updated_at=acc1.updated_at,
    )
    acc3 = FinancialAccount(
        id=uuid4(),
        owner_id=TEST_OWNER,
        name="Credit Card",
        account_type="credit",
        institution="Chase",
        balance=5000.0,  # Liability
        currency="USD",
        metadata={},
        created_at=acc1.created_at,
        updated_at=acc1.updated_at,
    )

    # Net worth: 10000 + 25000 - 5000 = 30000
    assets, liabilities, nw = compute_net_worth([acc1, acc2, acc3])
    assert assets == 35000.0
    assert liabilities == 5000.0
    assert nw == 30000.0

    # Cash flow and runway
    tx1 = FinancialTransaction(
        id=uuid4(),
        account_id=acc1.id,
        owner_id=TEST_OWNER,
        amount=3000.0,
        category="income",
        description="Salary",
        transaction_date=acc1.created_at,
        created_at=acc1.created_at,
    )
    tx2 = FinancialTransaction(
        id=uuid4(),
        account_id=acc1.id,
        owner_id=TEST_OWNER,
        amount=-5000.0,
        category="housing",
        description="Rent and Expenses",
        transaction_date=acc1.created_at,
        created_at=acc1.created_at,
    )

    # Net burn: -2000. Liquid assets: 10000 (checking only). Runway = 10000 / 2000 = 5.0 months
    cash_flow, breakdown, runway = compute_cash_flow_and_runway([acc1, acc2, acc3], [tx1, tx2])
    assert cash_flow == -2000.0
    assert runway == 5.0
    assert breakdown["income"] == 3000.0
    assert breakdown["housing"] == -5000.0


def test_strict_safety_ast_audit_for_zero_trading_execution():
    """Validates that the entire jarvis.finance module contains ZERO trade/order execution APIs."""
    finance_dir = Path(__file__).resolve().parent.parent / "jarvis" / "finance"
    assert finance_dir.is_dir()

    forbidden_names = {
        "buy",
        "sell",
        "place_order",
        "submit_order",
        "execute_trade",
        "trade",
        "wire_funds",
        "transfer_money",
        "market_order",
        "limit_order",
    }

    for py_file in finance_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name.lower()
                assert func_name not in forbidden_names, (
                    f"Forbidden execution function '{func_name}' detected in {py_file.name}! "
                    f"v3.7 strict safety forbids live broker or order placement interfaces."
                )


def test_finance_service_lifecycle():
    store = MemoryFinanceStore()
    service = FinanceService(store)

    # 1. Create checking account
    acc = service.create_account(
        TEST_OWNER,
        AccountCreateRequest(
            name="Primary Checking",
            account_type="checking",
            institution="Bank of America",
            balance=2000.0,
        ),
    )
    assert acc.balance == 2000.0

    # 2. Record income transaction (+1000)
    tx_inc = service.record_transaction(
        TEST_OWNER,
        TransactionCreateRequest(
            account_id=acc.id,
            amount=1000.0,
            description="Consulting Invoice",
        ),
    )
    assert tx_inc.category == "income"
    updated_acc = service.get_account(TEST_OWNER, acc.id)
    assert updated_acc.balance == 3000.0

    # 3. Record expense transaction (-200)
    tx_exp = service.record_transaction(
        TEST_OWNER,
        TransactionCreateRequest(
            account_id=acc.id,
            amount=-200.0,
            description="Supermarket Groceries",
        ),
    )
    assert tx_exp.category == "food"
    updated_acc2 = service.get_account(TEST_OWNER, acc.id)
    assert updated_acc2.balance == 2800.0

    # 4. Generate snapshot
    snapshot = service.generate_snapshot(TEST_OWNER)
    assert snapshot.net_worth == 2800.0
    assert snapshot.monthly_cash_flow == 800.0
    assert snapshot.projected_runway_months is None  # Positive cash flow

    # 5. Generate analytics report
    report = service.get_analytics_report(TEST_OWNER)
    assert report.accounts_count == 1
    assert report.transactions_count == 2
    assert "Net worth is positive" in report.insights[0]

    # 6. Delete transaction (reverses balance)
    assert service.delete_transaction(TEST_OWNER, tx_exp.id) is True
    reverted_acc = service.get_account(TEST_OWNER, acc.id)
    assert reverted_acc.balance == 3000.0
