# JARVIS v7 program — finishing the master plan

Owner decision (2026-10-24): Claude leads JARVIS and proceeds without asking for each version. v7 completes the parts of `JARVIS_MASTER_REFERENCE.txt` phases 11–13 that need none of the owner's accounts, money or devices.

## Scope

| Version | Phase | What | Why |
|---|---|---|---|
| v7.1 | 12 | Import transactions from bank CSV files, with data-quality checks and duplicate detection | Phase 12 asks for data ingestion and quality checks; today transactions can only be typed in one at a time |
| v7.2 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (drift warnings counted repeated daily snapshots as separate months), since fixed with six other findings | Committed state tested alone from a clean copy (see the v7.2 section) |
| v7.3 | 11 | Opt-in event sources: RSS/Atom feeds and a watched local folder, isolated from each other | Phase 11 asks for opt-in sources whose failures stay isolated |
| v7.4 | ACCEPTED by Claude as owner on 2026-10-25, after an independent review found one blocking defect (an option rated on one criterion could win with a perfect score), since fixed with seven other findings | Committed state tested alone from a clean copy (see the v7.4 section) |
| v7.5 | 13 | Life administration: documents with expiry dates, bills and renewals, with reminders | Phase 13 life administration |
| v7.6 | 13 | Creative: reports and documents built from the owner's notes and knowledge | Master reference 3.14 |

Each version passes the same gate as v6: tests, the full Python and live PostgreSQL suites (and the frontend suites when it has UI), an independent review, every finding fixed or explicitly accepted, then acceptance recorded here.

**Not in v7 (needs the owner):** email and calendar accounts, booking or payments, phones and the mobile app, Home Assistant and real devices, paid model plans.

**The Antigravity proposal** (`antigravity_proposed_features/IMPLEMENTATION_PLAN.md`, untracked) was not approved. Parts that fit Phase 13 security/system — a read-only hardware monitor, a secret-leak check before commits, a dependency vulnerability report drafted for review — are candidates after v7. Not adopted: cloning a real actor's voice, monitoring typing to stash git changes automatically, automatic dependency upgrades, and any claim of guaranteed correctness.

## Status ledger

| Version | Status | Evidence |
|---|---|---|
| v7.1 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found no blocking issues; four medium and six low findings fixed | Commit 1e8eaa2 tested alone from a clean copy: Python 1544 passed (the git-only repo test fails outside a checkout, as expected); live PostgreSQL 82 passed |
| v7.2 | IMPLEMENTED, TESTED; in review | 32 new tests pass; live PostgreSQL 88 passed; one v7.3 test assumes 0070 is the newest migration and is being updated with the v7.3 fixes |
| v7.3 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (a compressed feed could expand to gigabytes in memory), since fixed with ten other findings | Committed state tested alone from a clean copy (see the v7.3 section) |
| v7.4 | IMPLEMENTED, TESTED; in review | Python 1755 passed; live PostgreSQL 93 passed |
| v7.5 | IMPLEMENTED, TESTED; in review | 26 unit and 3 live PostgreSQL tests pass; commit tested from a clean copy |

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

## v7.3 — Opt-in event sources

### What it does
- **Sources** (`/sources`, scheduler capabilities): RSS/Atom feeds and watched folders, at most 50 per owner. Nothing is fetched or watched until the owner creates a source; with the scheduler runner off (the default), sources poll only on `POST /sources/{id}/poll`.
- **Feeds:** https on port 443 to public addresses only, enforced at connect time for every request and redirect (up to 3); `Accept-Encoding: identity`, at most one gzip/deflate layer decoded with a hard output cap, 2 MB, 15 s; DTDs and entities refused before parsing; titles, links and summaries capped and treated as untrusted.
- **Folders:** only inside roots listed in `JARVIS_SOURCES_FOLDER_ROOTS`; UNC and device paths refused unless the root is UNC; links and junctions never followed; cloud placeholders listed but never opened; file contents never read; at most 500 entries per poll, scanned lazily.
- **Notifications:** the first poll records existing items silently. After that, each poll sends at most one notification per source: the item itself, or a summary of several. Quiet hours apply, and each source has its own throttle so one chatty source cannot hide another. New or changed files are reported once they are stable across two polls.
- **Isolation:** each source polls independently, at most 4 at a time within a 25-second budget per cycle, off the reminder tick. Failures back off exponentially (up to 24 hours); after 10 in a row the source turns itself off with one notice. A storage failure after a fetch also backs off.
- **Storage:** migration 0070 (sources and seen items).

### Known limits
- Items suppressed by a source's throttle stay listed under `/sources/{id}/items` but are not re-sent.
- A timed-out fetch thread cannot be killed; at most 8 may be outstanding.
- Feeds in encodings expat does not support (such as windows-1252) fail as malformed.
- A new file is reported one poll after it appears.

### v7.3 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: the 2 MB size cap was checked only after decompression, so an 824-byte doubly gzipped feed made the process allocate 871 MB, and slightly more would crash JARVIS. The browser's HTTP provider had the same pattern and is fixed the same way; a 200 MiB bomb is now refused with under 20 MB of memory.

Ten more findings, all fixed with tests:
1. **Medium:** polling ran inside the scheduler tick and could delay reminders by minutes.
2. **Medium:** a shared throttle silently dropped items and let one source hide the others.
3. **Medium:** a storage failure after a fetch skipped backoff, so the feed was fetched every 30 seconds.
4. **Low:** a UNC path in a folder source opened an SMB connection before the root check.
5. **Low:** folder scans read the whole directory before applying their caps; large feeds kept the oldest items.
6. **Low:** OneDrive placeholders were skipped as links.
7. **Low:** modified and growing files re-notified as "New item".
8. **Low:** failure counts and the turn-off notice were not safe across processes.
9. **Low:** the URL check treated deprecated site-local IPv6 addresses as public.
10. **Low:** the status report now shows failing, truncated and stuck sources.

## v7.2 — Forecast snapshots and accuracy tracking

### What it does
- **Snapshots** (`POST /finance/forecast/snapshots`, `finance.write`): the forecast recomputed at save time with the same code and data read as `GET /finance/forecast`, stored with its date, model version, notes, every target (series, month, horizon, method, point, band, the naive value it saw) and a fingerprint of the history it used. Snapshots cannot be changed (a database trigger refuses updates). One per UTC day unless forced, at most 5 a day in total, at most 500 kept; eviction removes repeats within a month first.
- **Accuracy** (`GET /finance/forecast/accuracy`): scores targets whose month has finished against actuals from current data — MAE, bias, MAPE (income and spending only), 80% band coverage, and the naive "same as last month" baseline as it stood at save time — per series, horizon and method. Repeated snapshots of the same forecast are counted once; raw counts are reported too.
- **Drift warning:** over the last 6 distinct target months (each scored once at its shortest horizon), warns when the band caught fewer than half or every error had the same sign.
- **Data changes:** flags snapshots whose history changed afterwards (for example an older bank file imported later), including transactions moved between months.
- **Storage:** migration 0071 (snapshots and a child table of targets).

### Known limits
- Accuracy reads the newest 5000 transactions; if that read is cut off, data changes cannot be checked.
- Snapshots are taken on request; a monthly scheduled snapshot can come later.

### v7.2 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: daily snapshots of the same forecast were counted as separate evaluations, so one unusual month produced five false drift warnings stated as fact.

Six more findings, all fixed with tests:
1. **Medium:** forced snapshots plus the cap could evict the whole history.
2. **Medium:** the naive baseline used data imported after the forecast was made.
3. **Low:** moving a transaction between months went undetected.
4. **Low:** net-cash-flow MAPE exploded near zero; it is no longer reported for net.
5. **Low:** the memory store returned mutable stored objects.
6. **Low:** wording now says a snapshot is a recomputation at save time.

## v7.4 — Travel and decision planning

### What it does
- **Decisions** (`/travel/decisions`): options scored against weighted criteria (higher or lower better), scaled across the options. A criterion is used only when at least two options have different values; others are listed as excluded with the reason. Each option shows its coverage (how much of the used weight it has values for); when the winner's coverage is under 50% or below the runner-up's, the result is flagged and the explanation says it is not a clear winner. Sensitivity reports, per criterion, the exact weight at which the winner changes.
- **Trips** (`/travel/trips`): segments (flight, train, bus, car, ferry, stay, activity, other) with local times and IANA time zones; nonexistent local times are refused, repeated ones use the first occurrence and are noted. The summary shows the timeline in local and home time, overlaps, nights without a stay, tight connections, segments outside the trip dates, and costs.
- **Costs:** exact amounts in the currency's own decimal places (0 for JPY, 3 for KWD), totals by kind, per traveller and against budget, plus daily estimates. No currency conversion.
- **Departure reminders** through the assistant: one per transport segment, idempotent, following segment edits, keeping the owner's snooze and status unless the departure moves.
- **Suggestions** (`POST /travel/trips/{id}/suggest`): the model sees the trip as fenced untrusted data without booking references or notes, and answers are labelled unverified (and demo with the demo provider). Nothing is stored.
- **No booking, payments, account access or network lookups.** Booking references are never in audit records, logs or prompts.
- **Storage:** migration 0072 (travel tables and the `travel` audit category). `tzdata` is now a main dependency; without a time zone database `/status` shows travel as unavailable.

### Known limits
- Reminders and travel data are separate stores; a reminder that cannot be deleted is reported as orphaned.
- The per-trip lock works within one process; across processes the database's one-link-per-segment rule prevents duplicates.

### v7.4 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: a criterion with only one known value counted as "constant" and gave that option full marks, so a 1-star place with no price beat two fully priced hotels with a perfect 100.

Seven more findings, all fixed with tests:
1. **Medium:** sensitivity was wrong for criteria with weight 0; it is now checked against a brute-force test over 200 random decisions.
2. **Medium:** `tzdata` was not a declared dependency, so a fresh install could not use travel at all.
3. **Medium:** a reminder failure after a saved edit returned 500; the edit now succeeds and reports the reminder separately.
4. **Low:** reminders rewrote themselves inside the lead window, re-armed after dismissal, overwrote snoozes, dropped orphans silently, and shared one process-wide lock.
5. **Low:** money ignored each currency's decimal places.
6. **Low:** changing a start zone kept a stale end zone; segments outside the trip dates went unflagged; place mismatches were noisy; date-line flights never counted as overnight travel.
7. **Low:** caps are now tested under concurrency in PostgreSQL.

