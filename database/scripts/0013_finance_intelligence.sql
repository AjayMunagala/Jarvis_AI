-- 0013_finance_intelligence.sql
-- v3.7 Finance & External Data Intelligence
-- Stores analytical financial accounts, categorised transactions, and net worth snapshots.

CREATE TABLE IF NOT EXISTS jarvis_app.financial_accounts (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    name VARCHAR(200) NOT NULL,
    account_type VARCHAR(50) NOT NULL,
    institution VARCHAR(200) NOT NULL,
    balance NUMERIC(15, 2) NOT NULL DEFAULT 0.00,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_financial_accounts_owner ON jarvis_app.financial_accounts(owner_id, account_type);

CREATE TABLE IF NOT EXISTS jarvis_app.financial_transactions (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES jarvis_app.financial_accounts(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL,
    amount NUMERIC(15, 2) NOT NULL,
    category VARCHAR(100) NOT NULL,
    description VARCHAR(500) NOT NULL,
    transaction_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_financial_tx_owner_date ON jarvis_app.financial_transactions(owner_id, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_financial_tx_account ON jarvis_app.financial_transactions(account_id, transaction_date DESC);

CREATE TABLE IF NOT EXISTS jarvis_app.financial_snapshots (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    total_assets NUMERIC(15, 2) NOT NULL,
    total_liabilities NUMERIC(15, 2) NOT NULL,
    net_worth NUMERIC(15, 2) NOT NULL,
    monthly_cash_flow NUMERIC(15, 2) NOT NULL,
    breakdown_by_category JSONB NOT NULL DEFAULT '{}'::jsonb,
    projected_runway_months NUMERIC(6, 2),
    as_of_date TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_financial_snapshots_owner ON jarvis_app.financial_snapshots(owner_id, as_of_date DESC);
