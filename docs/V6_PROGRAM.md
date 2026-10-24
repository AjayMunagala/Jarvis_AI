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
| v6.4 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (mark-as-read used POST; the API only accepts PATCH), since fixed with six other findings | Vitest 141 passed (twice); Playwright E2E 11 passed; TypeScript clean |
| v6.5 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found no blocking issues; one medium and seven low findings fixed | Python 1324 passed; live PostgreSQL 73 passed (tutor live tests rerun after the fixes) |
| v6.6 | ACCEPTED by Claude as owner on 2026-10-24, after an independent review found one blocking defect (an "ok" forecast could rest on one fold with no uncertainty band), since fixed with eight other findings | Python 1371 passed; live PostgreSQL 75 passed; short-history probe: 7–9 months give insufficient_history, 10+ give bands on every point |

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

### v6.4 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: "Mark as read" sent POST, but the API route is PATCH, so every click failed with 405. The panel tests mocked the API, which hid it. An API-level test now checks the method, URL and empty body.

Six more findings, all fixed with tests:
1. **Medium:** after unlocking with nothing unread, the first new notification was never shown on the desktop (the "first load" rule was inferred from an empty set). A separate primed flag now marks the first successful poll.
2. **Low:** a browser whose `Notification` constructor throws (Android Chrome) lost the rest of that batch silently; each call is now guarded, and desktop delivery stops for that page after the first failure.
3. **Low:** older Safari's `requestPermission` returns nothing; the panel now reads the permission back and catches failures.
4. **Low:** an old notification scrolling into the newest-50 window could be announced hours late; only notifications delivered after unlock (with one minute of clock tolerance) are announced.
5. **Low:** the unread badge's label was on a plain span and reused the "SOON" style; it now has screen-reader text inside the button and its own style.
6. **Low:** a poll started under an earlier unlock could seed the new session; unlock now aborts the old requests and stale poll results are dropped.

**Known limit:** the view and the desktop announcements cover the newest 50 unread notifications; the badge count is always the full unread total.

## v6.5 — Tutor

### What it does
- **Learning goals:** topic, level (beginner, intermediate, advanced), weekly minutes, optional target date, 1–30 subtopics. At most 500 goals per owner (archived ones count).
- **Study plan** (`GET /tutor/goals/{id}/plan`): deterministic, no model. A learn and a practice session per subtopic, then one review, spread to the target date. If they don't fit, the plan is still returned, squeezed in, with `feasible: false`, a note and the weekly minutes it would need.
- **Exercises:** multiple choice (`answer_index`) or short answer (`accepted_answers`), written by the owner or generated by the model. At most 2000 per goal. The answer and explanation are never returned before an attempt.
- **Grading** is deterministic: the choice index, or normalized text (Unicode NFKC, case, spaces, trailing punctuation) or numeric equality. "four" does not match "4".
- **Spaced review (Leitner):** a correct answer moves the exercise up a box (review after 1, 2, 4, 8, 16 days); a wrong one sends it back to box 1. The box update is atomic in PostgreSQL.
- **Review queue and progress:** due exercises, accuracy overall, over the last 20 attempts and per subtopic, mastered count.
- **Model-assisted:**
  - `POST /tutor/explain` explains at the learner's level, stores nothing, and is labelled `demo` with the demo provider.
  - `POST /tutor/goals/{id}/exercises/generate` is refused (409) with the demo provider. Model output is validated like owner input and stored all-or-nothing: 502 on bad output, 503 on a provider failure, 504 on a timeout.
- **Storage:** memory, or PostgreSQL (migration 0068, which also adds the `tutor` audit category). Capabilities `tutor.read` and `tutor.write`.

### v6.5 independent review and acceptance
No blocking issues. Findings, all fixed with tests unless noted:
1. **Medium:** an infeasible plan's note could ask for fewer weekly minutes than the owner already gives. The need now uses the same basis as the feasibility check, so it is always higher.
2. **Low/medium:** the goal and exercise caps were checked outside the insert, so concurrent requests could overshoot. The check is now inside the insert's lock (memory) or transaction (a per-owner advisory lock for goals, `FOR UPDATE` on the goal row for exercises).
3. **Low:** a plan running past the last supported date crashed with 500; it is now a 422.
4. **Low:** an accepted answer of only punctuation matched any punctuation; such answers are now refused. Trailing punctuation and spaces are stripped together ("4. !" matches "4").
5. **Low:** a real provider timeout was reported as 503; it is now 504.
6. **Low:** the goal-cap message suggested archiving, which frees nothing; it now says archived goals count.
7. **Low:** generation called the model before checking the exercise cap; it now checks first.
8. **Low:** tests added for the caps, short-span plans, the date limit, timeouts and cross-owner updates.
9. **Design note (not changed):** `explain` is a model call gated by `tutor.read`. JARVIS has a single owner today; revisit if read-only tokens are ever issued.

## v6.6 — Finance forecasts (analysis only)

### What it does
`GET /finance/forecast?horizon=1..12&min_train=3..24` (authenticated, `finance.read`) forecasts monthly net cash flow, income and spending from stored transactions.
- **History:** complete UTC months before today; the current month is horizon 1. Future-dated transactions are excluded from history and added back to the starting balance so they are not counted twice.
- **Methods:** naive (last month), trailing mean of 3 and 6 months, seasonal naive (same month last year), and simple exponential smoothing whose smoothing factor is chosen only on each training window.
- **Evaluation:** rolling-origin backtest on months each method did not see; all methods are scored on the same origins. The lowest out-of-sample MAE wins, ties go to the simpler method, and the response says plainly whether it beats the naive baseline.
- **Uncertainty:** 80% intervals from out-of-sample errors, with the measured coverage. A minimum band width (5% of the typical monthly level, at least 1.00) stops a steady series from looking certain; coverage is not reported where it would not mean anything.
- **Balance scenario:** checking and savings plus the cumulative forecast flow, with a band and the first month its lower bound goes below zero. It is labelled a scenario, not a prediction.
- **Not enough data:** "ok" requires at least 3 backtest origins and 8 out-of-sample errors; otherwise the status is `insufficient_history` with the months needed (10 with the defaults). Seasonal naive needs 16 months for horizon 3 or more.
- **Every response** says it is analysis only, not financial advice, that past patterns may not continue, and that nothing is executed. There is no trading or broker code.
- **Data quality:** non-finite amounts are refused on write (422), and any stored ones are skipped and counted. Reads are bounded (5000 transactions, 1000 accounts) and flagged when cut off.

### Known limits
- Transfers between your own accounts, refunds (counted as income), and credit-card purchases plus their payments can inflate income and spending.
- The chosen method's error, coverage and intervals come from the same folds used to choose it, so they may be slightly optimistic; on noisy data the measured coverage is often below 80%.
- Forecasts are computed on demand and not stored, so their accuracy is not tracked over time yet.

### v6.6 independent review and acceptance
The reviewer found one blocking defect, fixed before acceptance: with 7 months of history the forecast returned "ok", claimed to beat the naive baseline from a single test month, and had no uncertainty band. "ok" now needs at least 3 origins and 8 errors, and an "ok" result can never lack a band.

Eight more findings, all fixed with tests:
1. **Medium:** a constant series (a fixed salary) had a zero-width "80%" band with 100% coverage; a minimum width now applies and coverage is marked not meaningful.
2. **Medium:** future-dated transactions were counted twice in the balance scenario.
3. **Medium:** paged reads without a unique sort order could skip or repeat transactions; reads are now ordered by date and id, in one bounded query.
4. **Low/medium:** NaN and infinite amounts were accepted and corrupted the forecast.
5. **Low:** notes added on selection optimism and the unmeasured balance band.
6. **Low:** the seasonal method's real history requirement is now computed and stated.
7. **Low:** the error minimum rose to 8, and the cumulative balance band widens with the horizon, not its square root.
8. **Low:** account-read truncation is flagged; refunds and credit-card double counting are noted.

