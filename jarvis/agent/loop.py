"""Bounded action loop: propose -> validate -> execute via ToolRuntime -> feed back (v2.8).

Trusted-code rules, independent of any proposal:

- only tools the owner can currently invoke (``CapabilityDescriptor.invocable``)
  are offered; a proposal naming anything else is recorded ``denied`` and not run;
- ``arguments_json`` must decode to a JSON object; the runtime then re-validates
  the schema, permissions, risk (LOW read-only only), availability, and limits;
- identical calls in one request run once;
- at most ``JARVIS_MAX_TOOL_ROUNDS`` rounds and ``JARVIS_CHAT_TOOL_MAX_CALLS`` calls,
  all inside the chat request's shared deadline;
- tool outputs reach the answering model only as untrusted JSON records.
"""

import asyncio
import json
from time import monotonic
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from jarvis.agent.models import ToolActivity, ToolActivityCall, ToolEvidenceRecord
from jarvis.agent.proposers import ModelProposer, RuleBasedProposer
from jarvis.tools.handlers import ToolContext
from jarvis.tools.models import ToolStatus
from jarvis.tools.runtime import ToolRuntime

MAX_EVIDENCE_OUTPUT_CHARS = 6000
MIN_SECONDS_FOR_A_CALL = 0.25


def _evidence(ref: str, result) -> ToolEvidenceRecord:
    output = json.dumps(result.output, ensure_ascii=True, separators=(",", ":")) if result.output is not None else "null"
    truncated = len(output) > MAX_EVIDENCE_OUTPUT_CHARS
    return ToolEvidenceRecord(
        ref=ref, tool_id=result.tool_id, status=result.status.value,
        output_json=output[:MAX_EVIDENCE_OUTPUT_CHARS], output_truncated=truncated,
        error=f"{result.error.code}: {result.error.message}"[:500] if result.error else None,
    )


class ActionLoop:
    def __init__(self, runtime: ToolRuntime, provider, settings):
        self.runtime = runtime
        self.provider = provider
        self.settings = settings

    async def run(self, *, owner_id: UUID, message: str, context: ToolContext, policy, deadline: float,
                  execution_id: str) -> tuple[ToolActivity, tuple[ToolEvidenceRecord, ...]]:
        catalog = await run_in_threadpool(self.runtime.describe, owner_id, context, policy=policy)
        offered = [item for item in catalog.capabilities if item.invocable]
        offered_ids = {item.tool_id for item in offered}
        use_model = not getattr(self.provider, "is_demo", False)
        proposer_name = "model" if use_model else "rules"
        rules, model = RuleBasedProposer(), ModelProposer(self.provider)
        max_rounds = self.settings.max_tool_rounds
        max_calls = self.settings.chat_tool_max_calls
        calls: list[ToolActivityCall] = []
        evidence: list[ToolEvidenceRecord] = []
        seen: set[tuple[str, str]] = set()
        limitations: list[str] = []
        stopped = "round_limit"
        rounds_used = 0

        if not offered:
            limitations.append("No tool is currently invocable for this owner and configuration.")
            return ToolActivity(proposer=proposer_name, rounds_used=0, stopped_reason="no_tools_needed",
                                limitations=tuple(limitations)), ()

        for round_number in range(1, max_rounds + 1):
            remaining = deadline - monotonic()
            if remaining <= MIN_SECONDS_FOR_A_CALL:
                stopped = "deadline"
                break
            try:
                if use_model:
                    proposal = await asyncio.wait_for(
                        model.propose(message, offered, evidence, round_number, max_rounds - round_number + 1),
                        timeout=remaining,
                    )
                else:
                    proposal = rules.propose(message, offered_ids, round_number)
            except asyncio.TimeoutError:
                stopped = "deadline"
                break
            except (ValueError, TypeError):
                stopped = "proposal_invalid"
                limitations.append("The model returned a tool proposal that failed validation; no tool was run for it.")
                break
            except Exception:
                stopped = "provider_failed"
                limitations.append("The provider failed while proposing tools; the answer uses no further tool results.")
                break
            rounds_used = round_number
            if not proposal.calls:
                stopped = "completed" if calls else "no_tools_needed"
                break
            for proposed in proposal.calls:
                if len(calls) >= max_calls:
                    stopped = "call_limit"
                    break
                if deadline - monotonic() <= MIN_SECONDS_FOR_A_CALL:
                    stopped = "deadline"
                    break
                ref = f"T{len(calls) + 1}"
                key = (proposed.tool_id, proposed.arguments_json.strip())
                if proposed.tool_id not in offered_ids:
                    calls.append(ToolActivityCall(
                        ref=ref, round=round_number, proposer=proposer_name, tool_id=proposed.tool_id[:80],
                        purpose=proposed.purpose[:200], status=ToolStatus.DENIED.value, error_code="not_offered",
                        executed=False, duration_ms=0,
                    ))
                    continue
                if key in seen:
                    continue
                seen.add(key)
                try:
                    arguments = json.loads(proposed.arguments_json or "{}")
                except ValueError:
                    arguments = None
                if not isinstance(arguments, dict):
                    calls.append(ToolActivityCall(
                        ref=ref, round=round_number, proposer=proposer_name, tool_id=proposed.tool_id,
                        purpose=proposed.purpose[:200], status=ToolStatus.INVALID_INPUT.value,
                        error_code="arguments_not_object", executed=False, duration_ms=0,
                    ))
                    continue
                result = await run_in_threadpool(
                    self.runtime.invoke, owner_id, proposed.tool_id, arguments, context,
                    correlation_id=execution_id, policy=policy,
                )
                calls.append(ToolActivityCall(
                    ref=ref, round=round_number, proposer=proposer_name, tool_id=result.tool_id,
                    purpose=proposed.purpose[:200], status=result.status.value,
                    error_code=result.error.code if result.error else None,
                    executed=result.status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.TIMED_OUT, ToolStatus.PARTIAL},
                    evidence=result.evidence, duration_ms=result.duration_ms, call_id=result.call_id,
                    audit_event_id=result.audit_event_id, untrusted_content=result.untrusted_content,
                ))
                evidence.append(_evidence(ref, result))
            if stopped in {"call_limit", "deadline"}:
                break
            # Rule-based proposals are single-round; a model may declare itself done.
            if not use_model or proposal.done:
                stopped = "completed"
                break
        if any(item.untrusted_content for item in calls):
            limitations.append("Some tool results contain untrusted external or document content.")
        return ToolActivity(proposer=proposer_name, rounds_used=rounds_used, calls=tuple(calls),
                            stopped_reason=stopped, limitations=tuple(limitations)), tuple(evidence)
