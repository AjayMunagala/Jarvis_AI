# JARVIS Browser Intelligence (v2.6)

Browser intelligence lets JARVIS open pages, observe them as structured untrusted evidence, perform a small set of
bounded non-consequential interactions, run evidence-backed browser checks, and research owner-supplied URLs. It is
**off by default** and never performs consequential actions.

## Status at a glance

| Area | Implemented | Verified with |
|---|---|---|
| Provider interface + mock provider | Yes | Unit/API tests (deterministic pages) |
| HTTP provider (`httpx`, no JavaScript) | Yes, with connection-time destination enforcement (DNS-rebinding protection) | `httpx.MockTransport` tests, fake-backend rebinding/redirect tests, and real loopback HTTP server tests |
| Playwright provider (Chromium, JavaScript) | Yes; optional `browser` extra (`playwright==1.64.0`) | **Real headless Chromium 156.0.8078.4 (build 1248)** against a local loopback server (`tests/test_browser_chromium.py`), plus fake-engine wiring tests |
| Sessions, limits, isolation, cleanup | Yes | Unit/API tests |
| Observation (DOM text, links, forms, elements, diagnostics) | Yes | Unit/API tests |
| Interaction (click/type/select/scroll) with validation | Yes (mock and Playwright providers; HTTP provider follows links only) | Mock provider tests |
| Consequential actions | **Never executed**; recorded as `approval_required` | Tests assert the provider is never called |
| Browser checks (v2.5 `VerificationReport`) | Yes; execution off by default | API tests |
| Research over supplied URLs | Yes; no web search | API tests |
| Frontend "Browser" workspace | Yes | Vitest component tests |

## Configuration

All settings use the `JARVIS_` prefix (see `.env.example`).

| Setting | Default | Meaning |
|---|---|---|
| `BROWSER_ENABLED` | `false` | Master switch. Requires `LOCAL_AUTH_TOKEN` and `OWNER_ID`; the browser is never anonymous. |
| `BROWSER_PROVIDER` | `http` | `mock` (offline demo pages), `http` (real fetching, no JavaScript), or `playwright` (optional). |
| `BROWSER_ALLOW_PRIVATE_NETWORK` | `false` | Allows loopback/private destinations for local test pages. Rejected in `production`. |
| `BROWSER_ALLOWED_DOMAINS` / `BROWSER_BLOCKED_DOMAINS` | empty | Comma-separated domain lists (suffix match on label boundaries). |
| `BROWSER_MAX_SESSIONS` | 2 (1–4) | Concurrent active sessions. |
| `BROWSER_SESSION_TTL_SECONDS` | 600 (60–3600) | Absolute session lifetime; expired sessions are closed. |
| `BROWSER_MAX_NAVIGATIONS` | 15 (1–50) | Navigations per session. |
| `BROWSER_MAX_ACTIONS` | 40 (1–100) | Accepted actions per session. |
| `BROWSER_NAVIGATION_TIMEOUT_SECONDS` | 15 (1–60) | Per navigation/operation. |
| `BROWSER_MAX_PAGE_BYTES` | 2,000,000 | Maximum downloaded document size. |
| `BROWSER_MAX_TEXT_CHARS` / `BROWSER_MAX_ELEMENTS` | 20,000 / 150 | Observation bounds. |
| `BROWSER_CHECKS_ENABLED` | `false` | Allows `/browser/checks/run` to open pages. Requires `BROWSER_ENABLED`. |

`verification_execution_enabled` (v2.5 process runner) is unrelated and stays `false`; browser checks never use it.

## API (all routes require the owner bearer token)

| Route | Capabilities | Purpose |
|---|---|---|
| `GET /browser/status` | `browser.read` | Provider, availability, capabilities, limits, policy summary. |
| `POST /browser/sessions` `{url?}` | `browser.read` | Open a session (optionally navigate). |
| `GET /browser/sessions`, `GET /browser/sessions/{id}` | `browser.read` | Owner's sessions; foreign and unknown ids both return 404. |
| `DELETE /browser/sessions/{id}` | `browser.read` | Close; page content and screenshots are discarded. |
| `POST /browser/sessions/{id}/navigate` `{url}` | `browser.read` | Navigate (policy-checked). |
| `POST /browser/sessions/{id}/actions` | `browser.read`; `browser.interact` for click/type/select/scroll | `navigate`, `back`, `reload`, `observe`, `screenshot`, `click`, `type`, `select`, `scroll`. |
| `GET /browser/sessions/{id}/screenshots/{shot}` | `browser.read` | PNG, `Cache-Control: no-store`. |
| `GET /browser/sessions/{id}/audit` | `browser.read` | Bounded, redacted audit events. |
| `POST /browser/research` | `browser.read` (+ `chat.advisory` for model interpretation) | Evidence report over ≤5 supplied URLs. |
| `POST /browser/checks/plan` | `browser.read` + `verify.read` | Planned checks (`not_checked`); nothing is opened. |
| `POST /browser/checks/run` | `browser.read` + `verify.read` | v2.5 `VerificationReport`. |

Actions always return an `ActionRecord` with `outcome` = `performed`, `blocked`, `approval_required`, `failed`,
`unsupported`, or `timed_out`, a reason, the classification, and URL transition (`navigated`, `same_page`,
`unexpected_navigation`). Proposed or blocked actions are never reported as performed.

## Security boundaries

- **Untrusted content.** Page text, labels, and links are evidence. Nothing a page contains is parsed for instructions
  or influences policy. Research prompts embed page excerpts as escaped JSON marked untrusted.
- **Permissions in code.** Capabilities are checked server-side before the service runs; the model and the page cannot
  classify their own actions. `external_action.execute` is still never granted.
- **Consequential actions.** Submit buttons, implicit-submit buttons in POST forms, and any control or link whose
  label/URL indicates buying, paying, sending, posting, publishing, deleting, confirming, agreeing, signing up/out,
  account changes, uploads, and similar are classified `consequential` and recorded as `approval_required`. **No
  approval-execution path exists in v2.6.**
- **Element validation.** Element actions must cite the latest `observation_id`; otherwise they are rejected as stale.
  Targets must exist in that observation and be enabled. A type/select/scroll that changes the URL is reported as
  `unexpected_navigation`.
- **Sensitive fields.** Password, hidden, file, payment, CVV, one-time-code and similar fields are flagged; JARVIS
  refuses to type into them, never reads their values, and refuses to type text that looks like a credential.
- **Destinations (SSRF/local files).** Only `http`/`https`; no credentials in URLs; every resolved address must be
  public (loopback, private, link-local incl. `169.254.169.254`, CGNAT, multicast, reserved, IPv4-mapped IPv6, and
  numeric spellings such as `2130706433` are refused). Checked before each navigation and each redirect hop;
  Playwright also checks every subresource request. `file:`, `data:` documents, `javascript:` and browser-internal
  schemes are refused.
- **Session isolation.** Each session has its own provider context (isolated cookie jar / browser context), bound to
  the owner; nothing persists across sessions; downloads, service workers, permissions and dialogs are disabled or
  dismissed (Playwright). Session ids are 96-bit random.
- **Data minimization.** Text, labels, console messages and audit details pass through secret redaction; URLs drop
  fragments and mask sensitive query values; network failures record method/status/sanitized URL only — no bodies,
  headers or cookies.
- **Bounded work.** Session TTL, navigation/action counts, timeouts, page bytes, text/element caps, ≤5 screenshots
  (≤4 MiB each), ≤200 audit events, and loop detection (3 identical actions on an unchanged page).

**DNS rebinding.** The HTTP provider's default transport (`guarded_transport.py`) resolves each host itself at
connection time, requires every resolved address to pass the policy, and opens the socket to that validated IP; TLS
still verifies the certificate against the original host name (SNI). The address checked is the address connected
to, for the first request and every redirect hop. No proxy is used.

**Residual risks.** In the Playwright provider Chromium resolves DNS itself: the policy is applied to every request URL
(navigations, redirects, subresources) before the request is sent, but the connected address is not pinned, so DNS
rebinding is mitigated but not eliminated for that provider. Lexical consequential-action detection
can produce false positives (safe) and could miss unusually labelled controls; in v2.6 the only interaction
capabilities are type/select/scroll/click of non-submit controls, and nothing is ever submitted.

## Browser checks and verification

Checks are expressed with the accepted v2.5 contracts (`VerificationReport`, `CheckResult`, `VerificationEvidence`)
without modifying them. `plan` returns `not_checked` items. `run` with `BROWSER_CHECKS_ENABLED=false` returns
`check_blocked` results with `execution_enabled=false` and `static_analysis` provenance. When enabled, each check
opens one page and evaluates assertions (`url_equals`, `url_contains`, `title_contains`, `text_present`,
`text_absent`, `element_present`, `status_code`, `no_console_errors`, `no_network_failures`). A check is
`check_passed` only when every assertion was evaluated and passed; evidence records the observation id, content hash,
expected and observed values.

## Research evidence

`POST /browser/research` reads only the supplied URLs (no web search). Each source reports `retrieved`, `failed`,
`blocked` or `timed_out`, with final URL, title, retrieval time, HTTP status, content hash, best-matching excerpt and
matched terms. `observations` are verbatim sentences from retrieved pages. An optional `interpretation` is labelled
`origin: model`, `verified: false`, and lists only citations to sources actually retrieved, counting invented ones.

## Provider dependency (Python Playwright) — owner-authorized and installed in `.venv-dev`

- **Why:** JavaScript-rendered pages, real interaction, screenshots of rendered pages, console capture.
- **Change:** `pyproject.toml` optional extra `browser = ["playwright==1.64.0"]` (exact pin: each release expects one
  Chromium build). Installed into `.venv-dev` only with `.venv-dev\Scripts\python.exe -m pip install "playwright==1.64.0"`
  (pulls `greenlet-3.5.6` and `pyee-14.0.0`). `pip install -e ".[browser]"` was deliberately not used from a worktree
  because the shared `.venv-dev` holds an editable install of the main checkout and would be repointed.
- **Browser binaries:** Python Playwright 1.64.0 requires `chromium-1248`, `chromium_headless_shell-1248`,
  `ffmpeg-1013`, and `winldd-1007` in `%LOCALAPPDATA%\ms-playwright`; all were already present (installed by the
  frontend's Playwright), so nothing was downloaded. On another machine run
  `.venv-dev\Scripts\python.exe -m playwright install chromium` (~150 MB; requires authorization).
- **Without it:** `mock` and `http` providers work; `playwright` reports itself unavailable via `/browser/status`
  and session creation returns 503. No startup failure.
- **Windows:** the adapter uses the sync API on one dedicated worker thread per session to avoid asyncio event-loop
  policy conflicts under Uvicorn. Antivirus scanning can slow first launch.

## Manual testing

1. Set `JARVIS_BROWSER_ENABLED=true`, `JARVIS_BROWSER_PROVIDER=mock`, a 32+ character `JARVIS_LOCAL_AUTH_TOKEN`, and
   `JARVIS_OWNER_ID`; start the API and the web UI; unlock the workspace; open **Browser**.
2. Open `https://demo.jarvis.test/`, type into Search, choose a scope, click **Show tips** (console error appears),
   click **Request** on the Search submit button (approval-required banner; not performed).
3. Open `https://demo.jarvis.test/injection`: the injected text appears as untrusted page text; **Pay now** is
   approval-required.
4. Try `http://169.254.169.254/`, `file:///C:/Windows/win.ini`, `http://127.0.0.1:8000/`: all blocked.
5. Research tab: question + `https://demo.jarvis.test/docs` and `http://10.0.0.1/`: one retrieved, one blocked.
6. With `JARVIS_BROWSER_PROVIDER=http`, real public pages can be observed (no JavaScript, link following only).

## Not implemented (deferred)

Approval execution for consequential actions (not authorized); multi-step autonomous browser workflows driven by
the model; web search; authenticated sites / credential handling; persistent profiles; file uploads/downloads;
connection-address pinning inside Chromium; browser scenarios in the Playwright E2E suite.
