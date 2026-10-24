-- Purpose: life administration (v7.5): documents with expiry dates, bills with recurrence, a payment history and
-- reminder links. Owner-entered metadata only: no document image or file, no full identifier number (at most the
-- last 4 letters or digits), no bank or card data. A payment row records what the owner says they paid elsewhere;
-- nothing here moves money.
-- Payments and reminder links belong to an item of the same owner (composite foreign keys) and go with it.
-- bills.account_id names one of the owner's finance accounts for context. It has no foreign key: the application
-- checks ownership when it is set, and a later-deleted account leaves only a stale reference (financial_accounts
-- has no (id, owner_id) key to point at, and this migration does not change finance tables).
-- Reminder links point at assistant reminders by id without a foreign key: the assistant owns those rows.
-- Audit events use the existing 'assistant' category (event types 'lifeadmin.*'), so the audit category CHECK
-- from 0072 is not rebuilt.
CREATE TABLE jarvis_app.lifeadmin_documents (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    holder_name text,
    issuer text,
    identifier_last4 text,
    issued_on date,
    expires_on date,
    renewal_lead_days integer NOT NULL,
    notes text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT lifeadmin_documents_id_owner UNIQUE (id, owner_id),
    CONSTRAINT lifeadmin_documents_kind_allowed CHECK (kind IN ('passport', 'national_id', 'driving_licence', 'visa',
                                                                'insurance', 'warranty', 'vehicle_registration',
                                                                'lease', 'subscription', 'other')),
    CONSTRAINT lifeadmin_documents_status_allowed CHECK (status IN ('active', 'renewed', 'expired', 'archived')),
    CONSTRAINT lifeadmin_documents_title_length CHECK (char_length(title) BETWEEN 1 AND 200),
    CONSTRAINT lifeadmin_documents_holder_length CHECK (holder_name IS NULL OR char_length(holder_name) BETWEEN 1 AND 100),
    CONSTRAINT lifeadmin_documents_issuer_length CHECK (issuer IS NULL OR char_length(issuer) BETWEEN 1 AND 200),
    CONSTRAINT lifeadmin_documents_last4 CHECK (identifier_last4 IS NULL
                                                OR (char_length(identifier_last4) BETWEEN 1 AND 4
                                                    AND identifier_last4 ~ '^[A-Za-z0-9]{1,4}$')),
    CONSTRAINT lifeadmin_documents_dates CHECK (
        (issued_on IS NULL OR issued_on BETWEEN DATE '1900-01-01' AND DATE '2200-12-31')
        AND (expires_on IS NULL OR expires_on BETWEEN DATE '1900-01-01' AND DATE '2200-12-31')
        AND (issued_on IS NULL OR expires_on IS NULL OR expires_on >= issued_on)),
    CONSTRAINT lifeadmin_documents_lead_range CHECK (renewal_lead_days BETWEEN 1 AND 730),
    CONSTRAINT lifeadmin_documents_notes_length CHECK (notes IS NULL OR char_length(notes) BETWEEN 1 AND 2000)
);
CREATE INDEX lifeadmin_documents_owner_expiry ON jarvis_app.lifeadmin_documents (owner_id, expires_on);

-- anchor_day is the day of the month monthly, quarterly and yearly bills return to (Jan 31 -> Feb 28 -> Mar 31).
-- next_due_on is NULL once a one-time bill is paid or the next date would pass end_on.
CREATE TABLE jarvis_app.lifeadmin_bills (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    name text NOT NULL,
    payee text,
    amount numeric(15, 3),
    currency text NOT NULL,
    recurrence text NOT NULL,
    interval_days integer,
    next_due_on date,
    anchor_day integer NOT NULL,
    end_on date,
    autopay boolean NOT NULL DEFAULT false,
    account_id uuid,
    category text,
    notes text,
    remind_days_before integer NOT NULL DEFAULT 3,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT lifeadmin_bills_id_owner UNIQUE (id, owner_id),
    CONSTRAINT lifeadmin_bills_name_length CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT lifeadmin_bills_payee_length CHECK (payee IS NULL OR char_length(payee) BETWEEN 1 AND 200),
    CONSTRAINT lifeadmin_bills_amount_range CHECK (amount IS NULL OR amount BETWEEN 0 AND 1000000000),
    CONSTRAINT lifeadmin_bills_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT lifeadmin_bills_recurrence CHECK (
        (recurrence IN ('one_time', 'weekly', 'monthly', 'quarterly', 'yearly') AND interval_days IS NULL)
        OR (recurrence = 'every_n_days' AND interval_days BETWEEN 1 AND 3650)),
    CONSTRAINT lifeadmin_bills_anchor_range CHECK (anchor_day BETWEEN 1 AND 31),
    CONSTRAINT lifeadmin_bills_dates CHECK (
        (next_due_on IS NULL OR next_due_on BETWEEN DATE '2000-01-01' AND DATE '2200-12-31')
        AND (end_on IS NULL OR end_on BETWEEN DATE '2000-01-01' AND DATE '2200-12-31')),
    CONSTRAINT lifeadmin_bills_category_length CHECK (category IS NULL OR char_length(category) BETWEEN 1 AND 100),
    CONSTRAINT lifeadmin_bills_notes_length CHECK (notes IS NULL OR char_length(notes) BETWEEN 1 AND 2000),
    CONSTRAINT lifeadmin_bills_remind_range CHECK (remind_days_before BETWEEN 0 AND 30),
    CONSTRAINT lifeadmin_bills_status_allowed CHECK (status IN ('active', 'archived'))
);
CREATE INDEX lifeadmin_bills_owner_due ON jarvis_app.lifeadmin_bills (owner_id, next_due_on);

CREATE TABLE jarvis_app.lifeadmin_payments (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    bill_id uuid NOT NULL,
    paid_on date NOT NULL,
    due_on date NOT NULL,
    amount numeric(15, 3),
    currency text NOT NULL,
    note text,
    created_at timestamptz NOT NULL,
    CONSTRAINT lifeadmin_payments_bill FOREIGN KEY (bill_id, owner_id)
        REFERENCES jarvis_app.lifeadmin_bills (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT lifeadmin_payments_amount_range CHECK (amount IS NULL OR amount BETWEEN 0 AND 1000000000),
    CONSTRAINT lifeadmin_payments_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT lifeadmin_payments_note_length CHECK (note IS NULL OR char_length(note) BETWEEN 1 AND 500)
);
CREATE INDEX lifeadmin_payments_bill_paid ON jarvis_app.lifeadmin_payments (owner_id, bill_id, paid_on DESC);

-- One link per item. item_id repeats document_id or bill_id (whichever item_kind names) so that one key serves
-- both kinds while each kind keeps a real cascading foreign key.
CREATE TABLE jarvis_app.lifeadmin_reminder_links (
    item_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    item_kind text NOT NULL,
    document_id uuid,
    bill_id uuid,
    reminder_id uuid NOT NULL,
    due_on date NOT NULL,
    remind_at timestamptz NOT NULL,
    scheduled_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT lifeadmin_reminder_links_document FOREIGN KEY (document_id, owner_id)
        REFERENCES jarvis_app.lifeadmin_documents (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT lifeadmin_reminder_links_bill FOREIGN KEY (bill_id, owner_id)
        REFERENCES jarvis_app.lifeadmin_bills (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT lifeadmin_reminder_links_kind CHECK (
        (item_kind = 'document' AND document_id = item_id AND bill_id IS NULL)
        OR (item_kind = 'bill' AND bill_id = item_id AND document_id IS NULL))
);
CREATE INDEX lifeadmin_reminder_links_owner ON jarvis_app.lifeadmin_reminder_links (owner_id);
