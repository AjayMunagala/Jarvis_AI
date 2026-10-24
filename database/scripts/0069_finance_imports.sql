-- Purpose: bank CSV imports (v7.1).
-- One row per committed import batch (owner, account, file name and counts; no file content). Imported
-- transactions carry the batch (import_batch_id) and a fingerprint of (account, day, cents, normalized
-- description, occurrence in the file); the partial unique index makes a re-import of the same rows add nothing.
-- A batch belongs to an account of the same owner and is removed with it. Undoing an import is done by the
-- application, which deletes the batch's transactions and reverses their balance in one transaction; the foreign
-- key only clears import_batch_id if a batch row is ever removed some other way. Manual transactions keep both
-- columns NULL. The batch's account must belong to the same owner (composite foreign key on the new
-- (id, owner_id) key of financial_accounts).
ALTER TABLE jarvis_app.financial_accounts ADD CONSTRAINT financial_accounts_id_owner UNIQUE (id, owner_id);

CREATE TABLE jarvis_app.financial_import_batches (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    account_id uuid NOT NULL,
    filename text NOT NULL,
    rows_in_file integer NOT NULL,
    rows_inserted integer NOT NULL,
    rows_skipped integer NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT financial_import_batches_id_owner UNIQUE (id, owner_id),
    CONSTRAINT financial_import_batches_account FOREIGN KEY (account_id, owner_id)
        REFERENCES jarvis_app.financial_accounts (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT financial_import_batches_filename_length CHECK (char_length(filename) BETWEEN 1 AND 255),
    CONSTRAINT financial_import_batches_counts CHECK (
        rows_in_file BETWEEN 0 AND 10000 AND rows_inserted >= 0 AND rows_skipped >= 0
        AND rows_inserted + rows_skipped <= rows_in_file)
);
CREATE INDEX financial_import_batches_owner_created
    ON jarvis_app.financial_import_batches (owner_id, created_at DESC);
CREATE INDEX financial_import_batches_account ON jarvis_app.financial_import_batches (account_id);

ALTER TABLE jarvis_app.financial_transactions
    ADD COLUMN import_batch_id uuid,
    ADD COLUMN import_fingerprint text,
    ADD CONSTRAINT financial_transactions_import_batch FOREIGN KEY (import_batch_id, owner_id)
        REFERENCES jarvis_app.financial_import_batches (id, owner_id) ON DELETE SET NULL (import_batch_id),
    ADD CONSTRAINT financial_transactions_import_fingerprint_format
        CHECK (import_fingerprint IS NULL OR import_fingerprint ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX financial_transactions_owner_account_fingerprint
    ON jarvis_app.financial_transactions (owner_id, account_id, import_fingerprint)
    WHERE import_fingerprint IS NOT NULL;
CREATE INDEX financial_transactions_import_batch
    ON jarvis_app.financial_transactions (import_batch_id)
    WHERE import_batch_id IS NOT NULL;
