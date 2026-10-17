"""Planner adapter boundary for the v5.5 plan evaluator.

Implemented:

- ``from_execution_plan`` (in ``evaluator``) for the existing orchestration
  ``ExecutionPlan``.
- ``from_neutral_plan`` for the *neutral plan document* defined below. This is
  a contract proposed by this workstream for upstream planners to target; it
  is not, and does not assume, the v3.2 DAG planner's own format.
- ``adapt_plan`` dispatches on registered adapters and refuses unknown types.

Neutral plan document (version 1)::

    {"format": "jarvis.neutral_plan", "version": 1, "goal": str,
     "nodes": [{"id": str, "description": str, "action_kind": str,
                "tool_id"?: str, "inputs"?: [str], "outputs"?: [str],
                "completion_criteria"?: str, "requires_approval"?: bool}],
     "edges": [{"from": node id, "to": node id}],   # "to" depends on "from"
     "constraints"?: PlanConstraints}

Adapters never execute, schedule or reorder anything; invalid input is
rejected (``PlanAdapterError``), never silently repaired.
"""

from typing import Any, Callable

from pydantic import ValidationError

from jarvis.reasoning.evaluator import from_execution_plan
from jarvis.reasoning.models import PlanCandidate

NEUTRAL_FORMAT = "jarvis.neutral_plan"
MAX_NODES = 40
MAX_EDGES = 400


class PlanAdapterError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def from_neutral_plan(document: dict[str, Any]) -> PlanCandidate:
    if not isinstance(document, dict) or document.get("format") != NEUTRAL_FORMAT or document.get("version") != 1:
        raise PlanAdapterError(f"Expected a {NEUTRAL_FORMAT} document, version 1.")
    nodes, edges = document.get("nodes"), document.get("edges", [])
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_NODES:
        raise PlanAdapterError(f"A plan needs 1-{MAX_NODES} nodes.")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        raise PlanAdapterError(f"A plan may have at most {MAX_EDGES} edges.")
    ids = [node.get("id") if isinstance(node, dict) else None for node in nodes]
    known = set(ids)
    dependencies: dict[str, list[str]] = {node_id: [] for node_id in ids if isinstance(node_id, str)}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") not in known or edge.get("to") not in known:
            raise PlanAdapterError("Every edge must connect two declared node ids.")
        dependencies[edge["to"]].append(edge["from"])
    try:
        steps = [{
            "step_id": node["id"], "description": node.get("description", ""),
            "action_kind": node.get("action_kind", "analysis"), "tool_id": node.get("tool_id"),
            "depends_on": tuple(dict.fromkeys(dependencies.get(node["id"], ()))),
            "inputs": tuple(node.get("inputs", ())), "outputs": tuple(node.get("outputs", ())),
            "completion_criteria": node.get("completion_criteria", ""),
            "requires_approval": node.get("requires_approval", False),
        } for node in nodes]
        return PlanCandidate.model_validate({"goal": document.get("goal"), "steps": steps,
                                             "constraints": document.get("constraints") or {}})
    except (KeyError, TypeError, ValidationError) as exc:
        raise PlanAdapterError("The plan document does not satisfy the plan contract.") from exc


_ADAPTERS: list[tuple[Callable[[Any], bool], Callable[[Any], PlanCandidate]]] = []


def register_adapter(matches: Callable[[Any], bool], adapter: Callable[[Any], PlanCandidate]) -> None:
    """Register an adapter for an upstream plan type (for example a future DAG plan)."""
    _ADAPTERS.append((matches, adapter))


def adapt_plan(plan: Any) -> PlanCandidate:
    if isinstance(plan, PlanCandidate):
        return plan
    for matches, adapter in _ADAPTERS:
        if matches(plan):
            return adapter(plan)
    raise PlanAdapterError(f"No plan adapter is registered for {type(plan).__name__}.")


def _is_execution_plan(plan: Any) -> bool:
    from jarvis.orchestration.models import ExecutionPlan  # noqa: PLC0415

    return isinstance(plan, ExecutionPlan)


register_adapter(_is_execution_plan, from_execution_plan)
register_adapter(lambda plan: isinstance(plan, dict) and plan.get("format") == NEUTRAL_FORMAT, from_neutral_plan)
