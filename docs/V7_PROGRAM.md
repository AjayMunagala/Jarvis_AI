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
| v7.1 | IMPLEMENTED, TESTED; in review | Python 1491 passed; live PostgreSQL 78 passed |
