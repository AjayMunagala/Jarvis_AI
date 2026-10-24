"""Independent, deterministic verification of an answer against tool evidence (v2.8).

Checks only what the application can observe; the model's claims are never
trusted. A ``verified`` status requires every successful tool result to be
cited, no citations to failed or unknown calls, and value consistency for
calculator results. Mock/demo providers do not cite, so their answers are
reported ``unverified`` rather than assumed correct.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from jarvis.agent.models import ToolActivity, ToolEvidenceRecord, ToolVerification, VerificationCheck

_CITATION = re.compile(r"\[(T[0-9]{1,2})\]")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    values = set()
    for match in _NUMBER.findall(text.replace(",", "")):
        try:
            values.add(Decimal(match).normalize())
        except InvalidOperation:
            continue
    return values


def verify(reply: str, activity: ToolActivity, evidence: tuple[ToolEvidenceRecord, ...]) -> ToolVerification:
    if not activity.calls:
        return ToolVerification(status="not_applicable", summary="No tool was called for this request.")
    by_ref = {item.ref: item for item in activity.calls}
    succeeded = {item.ref for item in activity.calls if item.status == "succeeded"}
    cited = set(_CITATION.findall(reply))
    known = cited & set(by_ref)
    unknown = cited - set(by_ref)
    checks: list[VerificationCheck] = []

    for item in activity.calls:
        checks.append(VerificationCheck(
            name=f"{item.ref}:{item.tool_id}:execution",
            outcome="pass" if item.status == "succeeded" else "fail",
            detail=f"Runtime status {item.status}" + (f" ({item.error_code})" if item.error_code else ""),
        ))
    for ref in sorted(known - succeeded):
        checks.append(VerificationCheck(name=f"{ref}:citation", outcome="fail",
                                        detail="The answer cites a tool call that did not succeed."))
    if unknown:
        checks.append(VerificationCheck(name="unknown_citations", outcome="fail",
                                        detail=f"The answer cites {len(unknown)} tool reference(s) that do not exist."))
    for ref in sorted(succeeded):
        checks.append(VerificationCheck(
            name=f"{ref}:cited", outcome="pass" if ref in cited else "fail",
            detail="The answer cites this successful result." if ref in cited else "The answer does not cite this result.",
        ))
    # Citation markers such as [T1] contain digits; exclude them from value checks.
    reply_numbers = _numbers(_CITATION.sub(" ", reply))
    for record in evidence:
        if record.tool_id != "calculator" or record.status != "succeeded" or record.ref not in cited:
            continue
        try:
            value = Decimal(str(json.loads(record.output_json)["result"])).normalize()
        except (ValueError, KeyError, TypeError, InvalidOperation):
            checks.append(VerificationCheck(name=f"{record.ref}:value", outcome="not_evaluated",
                                            detail="The calculator result could not be read."))
            continue
        checks.append(VerificationCheck(
            name=f"{record.ref}:value", outcome="pass" if value in reply_numbers else "fail",
            detail=f"The computed value {value} {'appears' if value in reply_numbers else 'does not appear'} in the answer.",
        ))

    failures = sum(item.outcome == "fail" for item in checks if not item.name.endswith(":execution"))
    execution_failures = sum(item.outcome == "fail" for item in checks if item.name.endswith(":execution"))
    value_failures = sum(item.outcome == "fail" for item in checks if item.name.endswith(":value"))
    if not succeeded:
        status, summary = "unverified", "No tool call succeeded, so the answer has no verified tool evidence."
    elif value_failures:
        status, summary = "unverified", "The answer cites a tool result but does not state its value correctly."
    elif failures == 0 and execution_failures == 0:
        status, summary = "verified", "Every successful tool result is cited and consistent with the answer."
    elif known & succeeded and not unknown and not (known - succeeded):
        status, summary = "partially_verified", "Some tool results are cited correctly; others are uncited or failed."
    else:
        status, summary = "unverified", "The answer does not correctly cite the successful tool results."
    return ToolVerification(status=status, checks=tuple(checks)[:40], cited_refs=tuple(sorted(known)),
                            unknown_citation_count=len(unknown), summary=summary)
