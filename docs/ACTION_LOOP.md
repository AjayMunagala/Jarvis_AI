# JARVIS Action Loop: request → tools → verification (v2.8)

v2.8 implements master-reference Phase 6 ("first complete action loop"). When enabled, a chat request can use the
v2.7 read-only tools: a proposer suggests tool calls, **trusted code validates and executes** them through the v2.7
`ToolRuntime`, results are given to the answering model as untrusted data, and the final answer is checked
independently against the tool evidence. It is **off by default** (`JARVIS_CHAT_TOOLS_ENABLED=false`) and requires
`JARVIS_TOOLS_ENABLED=true`.

## Flow (inside `POST /chat`, after memory/context assembly, within the shared request deadline)

1. **Offer.** `ToolRuntime.describe` lists tools that are invocable for the authenticated owner right now (enabled,
   bound, LOW-risk read-only, permitted, dependency available). Only these are offered.
2. **Propose.**
   - Demo/offline providers: `RuleBasedProposer` (deterministic patterns for arithmetic, symbol lookup, repository
     search, git status/diff, memory search), one round.
   - Real providers: `ModelProposer` asks the model for a `ToolProposal` via the existing strict structured-output
     contract. The prompt contains the request, the offered tools (id, description, input schema) as data, and earlier
     tool results as untrusted JSON.
3. **Validate (trusted code).** A proposed tool not in the offered set is recorded `denied` / `not_offered` and never
   reaches the runtime. `arguments_json` must decode to a JSON object (`invalid_input` / `arguments_not_object`
   otherwise). Identical calls in one request run once. The runtime then re-applies every v2.7 check (schema,
   permissions against the live policy, risk, availability, limits).
4. **Execute.** `ToolRuntime.invoke`, with the chat execution id as the audit correlation id.
5. **Repeat** up to `JARVIS_MAX_TOOL_ROUNDS` (existing setting, ≤5) rounds and `JARVIS_CHAT_TOOL_MAX_CALLS` calls
   (default 4, ≤8), stopping when the proposer is done, a limit is hit, the deadline is near, the proposal is
   invalid, or the provider fails.
6. **Answer.** Experts receive tool results as escaped JSON under "Untrusted tool result records" with refs `T1…`,
   plus an instruction to cite refs and repeat computed values exactly.
7. **Verify independently** (`jarvis/agent/verification.py`): per-call runtime status; citations to failed or
   non-existent calls; whether each successful result is cited; and, for calculator results, whether the computed
   value appears in the answer (digits inside citation markers do not count). Status:
   `verified` (all successful results cited and consistent), `partially_verified`, `unverified` (no successful call,
   wrong value, or incorrect citations), or `not_applicable` (no tool calls).

## Response

`ChatResponse.tool_activity` (omitted when the loop is disabled or not run): proposer (`rules`/`model`), rounds,
each call (`ref`, round, tool, purpose, status, error code, whether it executed, evidence, duration, call/audit ids,
untrusted flag), stop reason, verification (status, checks, cited refs, unknown-citation count, summary), and
limitations. The chat UI shows a "Tools (… validated by JARVIS)" line with statuses and the verification result.
`CapabilityDescriptor.autonomous` is `true` only for invocable tools when chat tools are enabled.

## Security boundaries

- The model's proposal carries no permissions, risk, or execution flags; it can only name a tool and arguments.
- Only offered (currently invocable) tools can run; unregistered, unbound, unavailable, or non-permitted tools
  are refused before the runtime, and the runtime re-checks everything.
- Only LOW-risk read-only tools exist; consequential actions, shell, arbitrary Python, patch application, and
  verification execution remain impossible.
- Tool outputs and earlier results are untrusted JSON; they cannot change the offered set or policy.
- Anonymous stateless chat never runs tools; the loop requires the authenticated owner.
- Bounded by rounds, calls, deduplication, the request deadline, and v2.7 per-tool limits.

## Limitations

- The default `JARVIS_REQUEST_TIMEOUT_SECONDS=5` may be too short for several model round-trips plus tools; raise it
  when using a real provider.
- The demo provider cannot cite, so offline answers are reported `unverified` even when the tool ran correctly.
- Verification is deterministic and evidence-based; it checks citations and calculator values, not the correctness
  of free-text reasoning about other tool outputs.
- The rule-based proposer recognizes a fixed set of English patterns only.
- Live LLM proposal quality is untested (scripted-provider tests only).
- Audit remains in-memory (v2.9 adds durable audit and approvals).

## Configuration

| Setting | Default | Notes |
|---|---|---|
| `JARVIS_CHAT_TOOLS_ENABLED` | `false` | requires `JARVIS_TOOLS_ENABLED=true` |
| `JARVIS_CHAT_TOOL_MAX_CALLS` | 4 | 1–8 per request |
| `JARVIS_MAX_TOOL_ROUNDS` | 5 | existing setting, 1–5 |

## Testing

`.venv-dev\Scripts\python.exe -m pytest tests/test_action_loop.py -v` uses a scripted provider (proposals and
answers) with the real v2.7 runtime; the frontend test is in `src/App.test.tsx`.
