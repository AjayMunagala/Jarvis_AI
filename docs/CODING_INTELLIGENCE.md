# Core v2.4 — Coding intelligence and proposed patches

JARVIS remains a modular personal AI operating system. This increment adds a bounded software-engineering advisory workflow to its existing repository intelligence and orchestration. It does not add an executor, verification engine, browser agent, voice system, automatic extraction, or self-modification. The project owner accepted v2.4 on 2026-10-17 with the documented limitations: live-provider behavior/model reasoning remain unverified, PostgreSQL was not rerun against the v2.4 tree, and the existing single-owner security boundaries remain. Patches are advisory only; automatic application and tool execution remain disabled.

## API and identity

`POST /coding/analyze`, JSON, requires the configured single-owner bearer credential and **both** server-side `repo.read` and `chat.advisory` capabilities. It is available without PostgreSQL, but never anonymously. Anonymous stateless mock `/chat` remains available as before. There is no client-selectable repository root, owner, grant, tool, command, or apply endpoint. Missing/indeterminate policy decisions fail closed.

Example request (paths are relative to the server-authorized repository root):

```json
{
  "request": "Propose a minimal addition fix and identify regression tests",
  "workflow": "patch",
  "target_paths": ["example.py"],
  "reported_observation": "User-reported: addition returned an unexpected value",
  "changes": [
    {
      "path": "example.py",
      "operation": "modify",
      "reason": "Propose addition instead of subtraction; not yet tested",
      "replacements": [{"old_text": "left - right", "new_text": "left + right"}]
    }
  ],
  "allow_provider_context": false
}
```

All contracts reject extra fields. `request` is nonblank and at most 4,000 characters; `reported_observation` is at most 8,000; at most eight 240-character target paths and five file changes are accepted. The union of explicit target and edit paths must also fit eight files. Redacted request/observation strings are clipped to their respective bounds if replacement markers expand them. `workflow` is `auto`, `explain`, `locate`, `trace`, `diagnose`, `recommend`, `patch`, `review_patch`, or `tests`. Automatic classification is deterministic routing, not an authorization decision. The existing request-body middleware enforces its byte bound (default 65,536 bytes), including streamed requests. There is no new configuration or migration.

`CodingReport` contains an execution ID, resolved workflow and interpretation, `analysis_origin` (`static` or `model`), provider, bounded evidence, server-derived facts, a clearly user-reported observation, an advisory proposal (summary/hypotheses/recommendations/risks/missing information), patch status and diffs, verified-existing test paths, verification recommendations and limitations. Successful reports always state `verification_status="not_run"`, `repository_modified=false`, and `execution_enabled=false`. Returned proposals omit the original replacement array after validating it: the diffs are the patch presentation. SHA-256 preimage and proposed-content hashes accompany each diff. Hashes refer to UTF-8 source text preserving CRLF/LF; they are not a guarantee the file has not subsequently changed.

Errors use the existing `{error, detail}` envelope: 401 authentication, 403 capability denial, 413 body size, 422 invalid contract/targets/explicit changes, 502 invalid model proposal, 503 unavailable evidence/wiring, and 504 deadline. Public details never include filesystem exceptions, input text, provider output, SQL, or credentials. The existing envelope maps coding 504 to `provider_timeout` and 503 to `persistence_unavailable`; those legacy error identifiers are retained for compatibility, while the sanitized detail describes the coding operation. Logs contain stable execution/outcome metadata only.

## Evidence and provider boundary

The service reuses `RepoIntelligenceService.get_inventory`, guarded text reading, Python symbol extraction, affected-test mappings and secret redaction. It inventories at most 500 entries (depth 12, at most two seconds), automatically reads at most 24 candidate files of at most 64 KiB each, and returns at most eight excerpts of 1,600 characters. Explicit paths bypass automatic selection but not validation. Existing test paths are checked before presentation, capped at 20. Missing, excluded, oversized, NUL-containing or replacement-decoded files do not become evidence. No Git or shell commands run in this coding workflow.

Paths are repository-relative and reject traversal, absolute/drive/UNC paths, ADS, controls, reserved names, symlink/reparse components and protected exclusions. Coding context additionally excludes dotfiles, common credential filenames and private/credentials/secrets directories. Supported text suffixes are `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.json`, `.toml`, `.md`, `.txt`, `.yaml`, `.yml`, `.css`, `.html`, `.sql`. Evidence contains relative paths, source hashes, redacted excerpts with start lines, and bounded Python symbols/imports/syntactic calls. Calls are not a resolved runtime call graph. Discovery is bounded and may miss relevant files; other language code is readable evidence, without equivalent AST relationship extraction.

The mock/local static path describes inspected structure, test candidates and limitations. It does **not** simulate model reasoning or invent a fix. Exact structured changes can produce a useful offline diff. Without explicit changes, a static patch request reports `unavailable` with missing information.

For a configured non-demo provider, analysis is invoked only if `allow_provider_context=true`, readable evidence exists, and the request has no explicit `changes`. Exact supplied edits stay local even when that flag is true. This flag is a data-disclosure preference, never a permission grant. Context is redacted before transmission and JSON-framed as untrusted data; fixed server instructions forbid obeying embedded source instructions. Serialized context is at most 20,000 characters; auxiliary symbols/relationships are dropped and excerpts/request/observation shortened if needed. The provider returns the closed `Proposal` schema through the existing provider adapter and shared orchestration executor. Local validation enforces bounds (raw output at most 32,000 characters), known evidence references, workflow, paths, preimages and secret checks. Referenced evidence establishes traceability, not correctness. All model reasoning remains unverified advice, separate from server facts. No live-provider quality or compatibility is claimed by fake-provider tests.

The existing request deadline covers evidence and provider work; the shared executor retains bounded transient retries. Thread cancellation cannot forcibly interrupt an in-flight OS file operation, but each file read and scan has explicit bounds. Filesystem races remain possible. Redaction remains heuristic, so opt-in cannot guarantee arbitrary private information is removed. Keep this single-owner prototype off shared/public deployments; bearer transport, in-memory-token/XSS and conversation-isolation limitations remain unchanged.

## Patches and frontend

Only `modify` (at most eight unique exact-match replacements per file, each side at most 4,000 characters) and `create` (content at most 8,000 characters, explicitly declared new target, no existing file) are supported. Modification snapshots must still match the current guarded read. Repeated targets, absent/ambiguous old text, no-ops, unsafe paths and secret-bearing pre/postimages are rejected. Each proposed file is capped at 64 KiB, each diff at 16,000 characters and combined diffs at 24,000. Diffs preserve final-newline markers. No delete, rename, binary patch, application, command execution, test execution, Git mutation or persistence is exposed. A model cannot grant permissions through JSON or source instructions.

The existing workspace has a Coding tab with bounded request/workflow/paths/observation fields, optional structured-edit JSON, and an unchecked provider-disclosure control. Reports distinguish facts, untrusted source, hypotheses, changes, diff previews, risks and tests not run. Source, errors and diffs are React text nodes, never executed or rendered as HTML/Markdown. Loading/error/empty states are accessible. Lock/401 unmounts the report and clears sensitive input; unmount or credential changes abort pending calls and reject late responses. The frontend uses the existing authenticated API transport and accepts only advisory response invariants. It never automatically retries or applies a patch.

## Development verification

Use installed `.venv-dev` and frontend dependencies. On Windows use a fresh external temporary directory rather than deleting or repairing an uncertain pytest temporary object:

```powershell
$testBase = Join-Path ([IO.Path]::GetTempPath()) ('jarvis-v24-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $testBase) { throw 'Refusing existing test directory' }
& .\.venv-dev\Scripts\python.exe -m pytest -q -rs tests --basetemp=$testBase
& .\.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests database
npm.cmd --prefix frontend test -- --run
npm.cmd --prefix frontend run build
npm.cmd --prefix frontend run test:e2e
git diff --check
```

`tests/test_coding.py` uses synthetic repositories and fake providers for classification, source/hash/AST discovery, closed input/output, redaction, exact diffs, stale/ambiguous/unsafe changes, provider deadlines/retries, authentication, both capabilities, missing policy, denied execution and evidence limits. Frontend component/API tests cover request transport, escaped source, loading/errors, duplicate submits, provider consent, malformed responses, session clearing and late results. Browser coverage uses real authenticated loopback mock coding requests and an unapplied synthetic new-file proposal; existing conversation interception remains separate from PostgreSQL evidence. Native link tests may skip when Windows denies symlink creation and must be reported as skipped. PostgreSQL suites remain opt-in and isolated; this phase adds no database behavior. Current run results and remaining verification are recorded in `HANDOFF.md`.

**OWNER-RUN RESULTS REPORTED BY THE PROJECT OWNER:** the 17 previously skipped native Windows cases passed with 0 failures, 0 skips, 14 deselected mocked variants and 1 existing warning in 1.73s (exit 0). This closes native verification using owner-provided evidence; it does not replace the separately recorded skipped Codex runs or establish live-provider/PostgreSQL verification.
