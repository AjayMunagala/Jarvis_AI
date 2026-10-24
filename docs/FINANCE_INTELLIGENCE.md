# Finance & External Data Intelligence (v3.7)

v3.7 introduces personal finance intelligence, automatic expense classification, net worth calculation, runway forecasting, and strict financial safety invariants.

## Safety Invariant & Design Decisions

### 1. Zero-Trading Execution Principle
- **Strictly Analytical**: Project JARVIS does not place market orders, execute trades, link live broker execution accounts, or transfer funds.
- An automated Abstract Syntax Tree (AST) audit test (`tests/test_finance.py::test_strict_safety_ast_audit_for_zero_trading_execution`) runs on every test pass to guarantee that no order execution, trading, or funds-transfer methods exist anywhere in the `jarvis/finance/` subsystem.

### 2. Automatic Expense Categorization
- Applies keyword pattern rules to incoming descriptions.
- Supported categories: `housing`, `food`, `transport`, `utilities`, `healthcare`, `entertainment`, `income`, `investment`, `other`.
- Positive transaction values default to `income`; negative values default to categorized expenses.

### 3. Net Worth & Runway Analytics
- **Assets**: Checking, savings, and investment account balances.
- **Liabilities**: Credit cards and loan obligations.
- **Net Worth**: $\text{Assets} - \text{Liabilities}$.
- **Cash Flow**: Sum of transactions over rolling 30-day window.
- **Runway**: Estimated as $\text{Liquid Cash} / |\text{Monthly Burn}|$ whenever net monthly cash flow is negative.

---

## Database Migration (`0013_finance_intelligence.sql`)

- `jarvis_app.financial_accounts`: `id` (UUID PK), `owner_id` (UUID), `name` (VARCHAR 200), `account_type` (VARCHAR 50), `institution` (VARCHAR 200), `balance` (NUMERIC 15,2), `currency` (VARCHAR 10), `metadata` (JSONB), `created_at` (TIMESTAMPTZ), `updated_at` (TIMESTAMPTZ).
  - Index: `idx_financial_accounts_owner` on `(owner_id, account_type)`.
- `jarvis_app.financial_transactions`: `id` (UUID PK), `account_id` (UUID FK), `owner_id` (UUID), `amount` (NUMERIC 15,2), `category` (VARCHAR 100), `description` (VARCHAR 500), `transaction_date` (TIMESTAMPTZ), `created_at` (TIMESTAMPTZ).
  - Indexes: `idx_financial_tx_owner_date`, `idx_financial_tx_account`.
- `jarvis_app.financial_snapshots`: `id` (UUID PK), `owner_id` (UUID), `total_assets` (NUMERIC 15,2), `total_liabilities` (NUMERIC 15,2), `net_worth` (NUMERIC 15,2), `monthly_cash_flow` (NUMERIC 15,2), `breakdown_by_category` (JSONB), `projected_runway_months` (NUMERIC 6,2), `as_of_date` (TIMESTAMPTZ).
  - Index: `idx_financial_snapshots_owner` on `(owner_id, as_of_date DESC)`.

---

## API Endpoints

All endpoints are mounted under `/finance`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/finance/accounts` | `POST` | `FINANCE_WRITE` | Register analytical account |
| `/finance/accounts` | `GET` | `FINANCE_READ` | List accounts with pagination |
| `/finance/accounts/{id}` | `GET` | `FINANCE_READ` | Retrieve specific account |
| `/finance/accounts/{id}` | `DELETE` | `FINANCE_WRITE` | Remove account and cascaded transactions |
| `/finance/transactions` | `POST` | `FINANCE_WRITE` | Record transaction with auto-categorization |
| `/finance/transactions` | `GET` | `FINANCE_READ` | List transactions (optional `account_id` filter) |
| `/finance/transactions/{id}` | `DELETE` | `FINANCE_WRITE` | Delete transaction and reverse balance |
| `/finance/analytics/snapshot` | `POST` | `FINANCE_WRITE` | Generate on-demand net worth snapshot |
| `/finance/analytics/snapshot` | `GET` | `FINANCE_READ` | Retrieve latest snapshot |
| `/finance/analytics/report` | `GET` | `FINANCE_READ` | Generate comprehensive financial intelligence report |

---

## Verification & Test Coverage

- `tests/test_finance.py`: Unit tests for categorization keywords, net worth and runway formulas, balance adjustment and reversal, and the strict safety AST audit.
- `tests/test_finance_api.py`: FastAPI TestClient integration tests verifying authentication, capability checks (401/403), account and transaction CRUD, snapshot generation, and analytics reporting.
