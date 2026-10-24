# JARVIS v7 program — finishing the master plan

Owner decision (2026-10-24): Claude leads JARVIS and proceeds without asking for each version. v7 completes the parts of `JARVIS_MASTER_REFERENCE.txt` phases 11–13 that need none of the owner's accounts, money or devices.

## Scope

| Version | Phase | What | Why |
|---|---|---|---|
| v7.1 | 12 | Import transactions from bank CSV files, with data-quality checks and duplicate detection | Phase 12 asks for data ingestion and quality checks; today transactions can only be typed in one at a time |
| v7.2 | 12 | Store each forecast with its date and measure its accuracy once the months arrive | Phase 12's gate asks for timestamped forecasts and monitoring |
| v7.3 | 11 | Opt-in event sources: RSS/Atom feeds and a watched local folder, isolated from each other | Phase 11 asks for opt-in sources whose failures stay isolated |
| v7.4 | 13 | Travel and decision planning: compare options, itineraries, cost estimates (no booking) | Master reference 3.15 |
| v7.5 | 13 | Life administration: documents with expiry dates, bills and renewals, with reminders | Phase 13 life administration |
| v7.6 | 13 | Creative: reports and documents built from the owner's notes and knowledge | Master reference 3.14 |

Each version passes the same gate as v6: tests, the full Python and live PostgreSQL suites (and the frontend suites when it has UI), an independent review, every finding fixed or explicitly accepted, then acceptance recorded here.

**Not in v7 (needs the owner):** email and calendar accounts, booking or payments, phones and the mobile app, Home Assistant and real devices, paid model plans.

**The Antigravity proposal** (`antigravity_proposed_features/IMPLEMENTATION_PLAN.md`, untracked) was not approved. Parts that fit Phase 13 security/system — a read-only hardware monitor, a secret-leak check before commits, a dependency vulnerability report drafted for review — are candidates after v7. Not adopted: cloning a real actor's voice, monitoring typing to stash git changes automatically, automatic dependency upgrades, and any claim of guaranteed correctness.

## Status ledger

| Version | Status | Evidence |
|---|---|---|
| v7.1 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found no blocking issues; four medium and six low findings fixed | Commit 1e8eaa2 tested alone from a clean copy: Python 1544 passed (the git-only repo test fails outside a checkout, as expected); live PostgreSQL 82 passed |
| v7.3 | IMPLEMENTED, TESTED; in review | Python 1639 passed (with v7.1); live PostgreSQL 86 passed |

## v7.1 — Bank CSV import

### What it does
- **Preview** (`POST /finance/imports/preview`, `finance.write`): upload a CSV (base64, at most 2 MB, 10,000 rows, 50 columns) with a column mapping. Nothing is stored except an in-memory preview (30 minutes; 3 per owner).
  - Encodings: UTF-8 (with or without BOM), UTF-16 with BOM, cp1252 fallback with a warning. The delimiter is sniffed or given.
  - Dates: ISO, DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY, YYYY/MM/DD, DD.MM.YYYY, DD/MM/YY (00–69 → 2000s), DD-Mon-YYYY, DD Mon YYYY, DD-Mon-YY; times with AM/PM or an offset; `utc_offset_minutes` for times without one. A file whose dates look like swapped DD/MM and MM/DD is refused; one where every date is ambiguous gets a warning.
  - Amounts: one signed column or debit/credit columns; dot or comma decimals; 3-digit and Indian lakh grouping; Rs/INR/₹ and other currency signs at the edges; parentheses; CR/DR. A sign that contradicts its debit/credit column is an error.
  - Checks: errors (bad date or amount, future or very old dates, empty description, both or neither debit/credit), warnings (outliers, extra decimals, zero amounts, repeated rows).
- **Commit** (`POST /finance/imports/{id}/commit`): inserts the rows and the balance change in one transaction. Rows already stored are always skipped; same-day repeats within the file are kept (banks rarely repeat rows by mistake). Per-row fingerprints make re-importing the same file add nothing. Balances that would overflow are refused.
- **Batches:** `GET /finance/imports` lists them; `DELETE /finance/imports/{id}` undoes one atomically, reversing exactly its balance effect.
- **Upload size:** the app's 64 KB request limit is raised to about 2.8 MB for the preview route only.
- **Safety:** file content is untrusted; it never appears in logs, audit records or error messages. Descriptions starting with `=`, `+`, `-` or `@` are stored as plain text and never evaluated.
- **Storage:** migration 0069 (import batches, fingerprints with a per-account unique index, a composite owner check between batches and accounts).

### Known limits
- Previews are per process and lost on restart.
- Matching against stored transactions scans at most 50,000 in the file's date range; fingerprints still stop re-imports beyond that.
- No currency conversion.

### v7.1 independent review and acceptance
No blocking issues. Findings, all fixed with tests:
1. **Medium:** swapped DD/MM and MM/DD silently misdated every row with a day of 12 or less.
2. **Medium:** real same-day repeats were dropped by default, so balances drifted on overlapping statements.
3. **Medium:** Indian bank formats (lakh grouping, Rs/INR, DD-Mon-YYYY, two-digit years, IST times) were refused.
4. **Medium:** a negative credit or contradicting CR/DR marker was imported with the wrong sign.
5. **Low:** the store's conflict path was untested (tests passed for the wrong reason).
6. **Low:** undo and commit could deadlock (different lock order).
7. **Low:** an overflowing balance crashed with 500 instead of a refusal.
8. **Low:** preview memory documented; eviction prefers the same owner's oldest preview.
9. **Low:** currency signs only at edges, zero amounts warned, consistent "file too large", file line numbers in results.
10. **Low:** the database now enforces that a batch's account belongs to the same owner.

