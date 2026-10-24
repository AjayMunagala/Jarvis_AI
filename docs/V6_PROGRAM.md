# JARVIS v6 program

Owner decision (2026-10-24): after v4.1–v5.0 was merged into `main` (`af281c2`), the owner gave Claude full ownership of the project ("do whatever"). The v6 scope comes from `JARVIS_MASTER_REFERENCE.txt`: the phases that are still incomplete, under its rules to start no new framework or service without a concrete need and not to start every domain at once.

Each version follows the same gate as v4.1–v5.0:
1. Implementation, with tests for every behaviour.
2. The full Python suite and the live PostgreSQL suite.
3. An independent review.
4. Every finding fixed, or documented with a reason.
5. An acceptance decision, recorded below.

The work lives on the `claude/jarvis-v6` branch and reaches `main` through a pull request that the owner merges.

## Scope

| Version | Blueprint phase | Scope | Why now |
|---|---|---|---|
| v6.1 | 3, 15 | Model usage and cost tracking; a fallback model for transient failures | Usage/cost tracking was a Phase 3 deliverable that never existed; a fallback gives the single-provider setup resilience |
| v6.2 | 3.5 | PDF text extraction with `pypdf`, under the same limits as DOCX | PDFs are the most common document type; the owner delegated the dependency decision |
| v6.3 | 10 | Daily briefing and calendar conflict detection | Assistant data exists, but nothing summarizes it or warns about clashes |
| v6.4 | 11 | Notifications delivered outside the app (browser or desktop) | Reminders fire, but nobody sees them unless the app is open |
| v6.5 | 13 | Tutor: study plans, exercises and progress | A blueprint capability that needs no external account |
| v6.6 | 12 | Finance forecasts with out-of-sample backtesting and uncertainty, analysis only | Phase 12 gate: forecasts must be evaluated, never guaranteed |
| v6.7 | Interfaces | UI views for workflows, scheduler, approvals and integrations | These features exist only through the API today |

### Deferred
These need the owner's accounts, money or hardware:
- Email and calendar connectors (OAuth);
- the mobile client (React Native);
- a real Home Assistant adapter;
- paid model plans.

The v5.1 router stays available for evaluations. Routing chat across several models becomes worthwhile once more than one real model is configured and measured.

## Status ledger

| Version | Status | Evidence |
|---|---|---|
| v6.1 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (recording blocked the request), since fixed with six other findings | Python 1247 passed; live PostgreSQL 68 passed (clean e371e13); real NVIDIA call recorded 48/383 tokens |
| v6.2 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (pypdf limit errors reached the API as HTTP 500), since fixed with five other findings | Python 1261 passed (with v6.1 and v6.3); hostile PDFs refused with 422 in under 1 s |
| v6.3 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found no blocking issues; two medium and five low findings fixed | Python 1266 passed; live PostgreSQL 69 passed |
| v6.4 | IMPLEMENTED, TESTED; in review | Vitest 138 passed; Playwright E2E 11 passed; TypeScript clean |

## v6.1 — Usage and cost tracking, fallback model

### What it records
- **One event per call** to a real model provider:
  - the provider and model;
  - whether it was the primary or the fallback model;
  - the outcome (succeeded, failed or timeout);
  - the input and output token counts the provider reported (`null` when it reports none);
  - the duration;
  - the request's correlation ID.
- **No text:** prompts and replies are never recorded.
- **Storage:** in memory, or in PostgreSQL (migration 0067, `usage_events`) when persistence is enabled.
- **Best effort:** the write runs in a worker thread. A storage failure is counted and logged, and never affects the reply.

### Summary
`GET /usage?days=N` (1–90; authenticated, `status.read`) returns per-model, per-day totals:
- calls, failed calls and fallback calls;
- tokens;
- calls without token counts.

**Cost.** Cost is estimated only from a declared price:
- `JARVIS_OPENAI_PRICE_INPUT_PER_MILLION`, `JARVIS_OPENAI_PRICE_OUTPUT_PER_MILLION` and `JARVIS_OPENAI_PRICE_SOURCE`;
- without them, cost is "unknown: no price declared";
- a total cost appears only when every model used has a price.

### Fallback model
- **Setting:** `JARVIS_OPENAI_FALLBACK_MODEL` names a second model on the same endpoint.
- **When it is used:** only for transient failures (timeouts, network errors, HTTP 429 and 5xx). The same request is then retried once on the fallback.
- **When it isn't:** a request the provider rejects (other 4xx) is never sent again.
- **Visibility:** both calls appear in the usage log, so it shows which model answered, and `/status` lists the configured fallback.

### Tests
- `tests/test_v61_usage_fallback.py`: 7 tests.
- `integration_tests/test_v61_postgres.py`: durable usage across a restart.
- Real NVIDIA check: one chat recorded 48 input and 381 output tokens. The cost showed as unknown, because no price is declared.

### v6.1 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: usage recording was awaited inside the request. A slow or unavailable database therefore delayed every chat and could overrun its deadline. The measurement: a 0.5 s deadline returned after 2.57 s with a slow store.

**The fix.** `record()` only puts the event on a bounded queue. One background writer thread stores events over one reused connection. A full queue drops events and counts them.

It also found six more issues, all fixed with tests:
1. Deadline cancellations were recorded as "failed"; they are now `cancelled`.
2. A failing fallback let the orchestrator's retries multiply calls; that error is now final.
3. The router and evaluation credited the wrong model; they now measure the primary model directly.
4. A partial price or a fallback equal to the primary was silently accepted; both are now refused.
5. The fallback model could not be priced; it now has its own price settings.
6. The summary had loose semantics. It now:
   - covers whole UTC days;
   - uses typed totals;
   - aggregates in SQL;
   - notes that failed calls may still be billed;
   - reports dropped and evicted events.

## v6.2 — PDF text extraction

### What it does
- **Library:** `pypdf` (pure Python), the optional `documents` extra: `pip install -e ".[documents]"`, pinned `>=6.21,<7` because earlier versions lack configurable decompression and parsing limits.
- **Without the library:** a PDF is refused with a message saying how to enable it, and `/status` shows "PDF extraction not installed".
- **Refused:** encrypted PDFs, PDFs with no extractable text (scanned images; there is no OCR), and damaged or hostile files.
- **Limits:**
  - the first 100 pages, the 500 KB text cap and a 20 s budget checked between pages; a visible `[JARVIS: ...]` note gives the real counts when one stops extraction;
  - pypdf's own limits are tightened (`PDF_LIMITS`): 16 MB per decoded stream, 200,000 content-stream operands, page tree depth 32, 500 form XObjects per page.
- **Page count** is the number of pages actually read.
- The text is fenced as untrusted content, like every other document.

### v6.2 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: pypdf's limit and parse errors (`LimitReachedError`, `ParseError` and others) and `DependencyError` were not caught, so a deep page tree, a decompression bomb or a content stream with millions of operands produced HTTP 500.

**The fix.** Every exception from pypdf becomes "The PDF could not be read" (HTTP 422), and the tighter limits make those files fail in well under a second.

Five more findings, all addressed:
1. The version floor admitted pypdf releases without the limits; it is now `>=6.21`.
2. One page could run for 5–15 s before the clock was checked; the tighter limits now bound a single page.
3. Damaged files flooded the log with repair warnings; the `pypdf` logger is set to ERROR during extraction.
4. The page count after an early stop could overstate what was read, and the stop note looked like document text; both are fixed.
5. Test gaps: tests now cover three hostile files (each must fail on a pypdf limit), the 422 through the API, and the "not installed" status.

**Known flaky test (not v6.2):** `tests/test_memory.py::test_retrieval_diagnostics_are_allowlisted_and_privacy_safe` failed once in a full run and passed in the next full run and alone.

## v6.3 — Daily briefing and calendar conflicts

### What it does
- `GET /assistant/briefing?utc_offset_minutes=N` (authenticated, `assistant.read`): for the owner's local day:
  - overdue tasks (due before the local day began) and tasks due today;
  - today's events (those overlapping the local day; one ending exactly at midnight belongs to the day before);
  - pending reminders in the next 24 hours;
  - calendar conflicts in the next 7 days;
  - a plain-text summary built only from stored records; no model writes it.
- `GET /assistant/calendar/conflicts?days=1..90` returns `{conflicts, truncated}`. Overlapping pairs are listed once; events that only touch are not conflicts.
- A scheduler job with `action_type: "daily_briefing"` sends the summary as a "briefing" notification. Quiet hours hold it; the anti-fatigue throttle never drops it.
- **Cron is UTC.** For 07:00 at UTC+05:30, schedule `30 1 * * *`.
- **Date-only due dates:** send full local timestamps; midnight UTC can fall on the previous local day for a negative offset.
- **Limits:** at most 50 items per list. `truncated` is true whenever a list was shortened or a read limit was reached; nothing is cut off silently.

### v6.3 independent review and acceptance
No blocking issues. Findings, all fixed with tests:
1. **Medium:** stale past-due reminders filled the 200-row read and hid upcoming ones. The query now has a lower bound (`due_after`), in memory and in SQL.
2. **Medium-low:** events ending at midnight, or starting at the next midnight, appeared in today's events. They are now filtered to real overlap.
3. **Low:** the 500-event read for conflicts could cut off later days silently; a full page now sets `truncated`.
4. **Low:** a second briefing within 15 minutes was dropped by the throttle; briefings are now exempt, like reminders.
5. **Low (docs):** cron runs in UTC; documented with an example.
6. **Low (design):** the overdue and date-only rules are documented.
7. **Low:** tests added for a negative offset, day boundaries, out-of-range offsets, stale reminders, conflict truncation, the throttle, and a live PostgreSQL run.

## v6.4 — Notifications view and desktop delivery

### What it does
- **Notifications view** (new nav item with an unread badge): lists reminders, briefings and alerts from `GET /scheduler/notifications`, and marks them read.
- **Desktop delivery:** while JARVIS is open and unlocked, it checks for unread notifications every 30 seconds. Each new one is shown once as a browser desktop notification, only after the owner clicks "Enable desktop notifications" and the browser grants permission. Notifications already unread at unlock are not re-announced.
- **Nothing leaves the device:** no push service, no email, no phone. Delivery to other devices needs the owner's accounts and stays deferred.
- **Assistant view:** it was built in v3.1 but its nav item was marked "SOON" and could not be opened. v6.4 makes it reachable, and corrects the v4.7 record that said otherwise.

### Tests
- `NotificationsPanel.test.tsx`: locked state, listing and mark-as-read, permission asked only on click, blocked/unsupported/error states.
- `App.test.tsx`: both views open; polling after unlock, the unread badge, and desktop notifications only for new items.

