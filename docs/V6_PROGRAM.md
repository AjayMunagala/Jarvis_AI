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
| v6.1 | IMPLEMENTED, TESTED; in review | Python 1237 passed; live PostgreSQL 68 passed; real NVIDIA call recorded 48/381 tokens |

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
