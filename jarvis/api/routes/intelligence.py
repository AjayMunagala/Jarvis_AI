"""Authenticated advanced-intelligence endpoints (v5.1-v5.6).

Every route requires the owner bearer token and server-side capabilities:
``intelligence.read`` for inspection, ``intelligence.evaluate`` to run model
evaluations, ``knowledge_graph.write`` for graph changes,
``personalization.manage`` for preference changes, and the existing
``memory.read`` / ``memory.write`` for memory context and memory proposals.
No route executes tools or actions, and none can change permissions.

Every mutation is recorded in the central v2.9 audit trail (category
"intelligence") with metadata only, as ``succeeded`` when it changed state or
``failed`` when an authorized attempt changed nothing (validation, conflict,
not found or storage failure). Requests rejected by authentication or
capability checks are refused before any audit-worthy work starts.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette.concurrency import run_in_threadpool

from jarvis.advanced_intelligence import IntelligenceLayer
from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.contextual.sources import RepositoryCandidateSource
from jarvis.contextual.models import ContextRequest
from jarvis.contextual.selection import assemble
from jarvis.contextual.updates import UpdateError
from jarvis.knowledge_graph.models import EntityInput, KnowledgeRecord, RelationshipInput
from jarvis.knowledge_graph.service import GraphError
from jarvis.models.schemas import ErrorResponse
from jarvis.multimodal.models import MultimodalRequest
from jarvis.multimodal.normalize import MultimodalInputError
from jarvis.personalization.models import Observation
from jarvis.personalization.service import PersonalizationError
from jarvis.reasoning.evaluator import evaluate_plan
from jarvis.reasoning.models import PlanCandidate
from jarvis.reasoning.strategies import STRATEGIES, check_answer
from jarvis.routing.evaluation import load_dataset
from jarvis.routing.models import TaskRequirements
from jarvis.routing.requirements import infer_requirements
from jarvis.security.policy import Capability

router = APIRouter(prefix="/intelligence", tags=["intelligence"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}
_DATASETS = ("routing_baseline",)


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _owner(request: Request, owner_id: UUID | None, *capabilities: Capability) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    for capability in (Capability.INTELLIGENCE_READ, *capabilities):
        enforce_capability(request, owner_id, capability)
    return owner_id


def _layer(request: Request) -> IntelligenceLayer:
    layer = getattr(request.app.state, "intelligence", None)
    if not isinstance(layer, IntelligenceLayer):
        raise HTTPException(status_code=503, detail="The intelligence layer is unavailable.")
    return layer


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except (GraphError, UpdateError, PersonalizationError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc
    except MultimodalInputError as exc:
        raise HTTPException(status_code=422, detail=exc.public_message) from exc


async def _mutate(request: Request, owner: UUID, event_type: str, subject: str, func, *args,
                  details: dict | None = None, describe=None, **kwargs):
    """Run a mutation and audit its outcome (metadata only)."""
    layer = _layer(request)
    correlation = request.headers.get("x-request-id")
    try:
        result = await _call(func, *args, **kwargs)
    except HTTPException as exc:
        layer.audit(owner, event_type, succeeded=False, subject=subject, correlation_id=correlation,
                    details={**(details or {}), "http_status": exc.status_code})
        raise
    except Exception:
        layer.audit(owner, event_type, succeeded=False, subject=subject, correlation_id=correlation,
                    details={**(details or {}), "http_status": 500})
        raise
    layer.audit(owner, event_type, succeeded=True, subject=subject, correlation_id=correlation,
                details={**(details or {}), **(describe(result) if describe else {})})
    return result


# ---------------------------------------------------------------- v5.1

@router.get("/models", responses=_ERRORS)
async def models(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    layer = _layer(request)
    statuses = layer.registry.status(layer.router.adapters, layer.router.disabled)
    return {"models": [item.model_dump(mode="json") for item in statuses],
            "policies": [item.model_dump(mode="json") for item in layer.router.policies.policies],
            "default_policy": layer.router.policies.default_policy,
            "evidence": sorted(layer.router.evidence()),
            "note": "Capabilities are declared, not measured, unless an accepted evaluation provides evidence."}


class RouteBody(_Body):
    description: StrictStr | None = Field(default=None, max_length=4000)
    requirements: TaskRequirements | None = None
    policy: StrictStr | None = Field(default=None, max_length=64)
    personalize: bool = True


@router.post("/route", responses=_ERRORS)
async def route(body: RouteBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    layer = _layer(request)
    if body.requirements is None and body.description is None:
        raise HTTPException(status_code=422, detail="Provide a description or explicit requirements.")
    signals: tuple[str, ...] = ()
    requirements = body.requirements
    if requirements is None:
        requirements, signals = infer_requirements(body.description)
    try:
        policy = layer.router.policy(body.policy)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Unknown routing policy.") from exc
    applied = False
    if body.personalize:
        prefs = await _call(layer.personalization.resolve, owner, requirements.task_type)
        policy, requirements = layer.personalization.apply_to_routing(prefs, policy, requirements)
        applied = prefs.enabled and bool(prefs.values)
    decision = layer.router.decide(requirements, policy=policy)
    return {"decision": decision.model_dump(mode="json"), "signals": list(signals), "personalized": applied,
            "note": "Routing selects among eligible models only; it never grants permissions or executes anything."}


@router.get("/routing/recent", responses=_ERRORS)
async def routing_recent(request: Request, limit: int = Query(default=50, ge=1, le=200),
                         owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    return {"records": [item.model_dump(mode="json") for item in _layer(request).router.recent(limit)]}


class EvaluationBody(_Body):
    dataset: Literal["routing_baseline"] = "routing_baseline"
    model_keys: tuple[StrictStr, ...] = Field(min_length=1, max_length=3)
    repeats: int = Field(default=1, ge=1, le=5)


@router.post("/evaluations", responses=_ERRORS)
async def run_evaluation(body: EvaluationBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.INTELLIGENCE_EVALUATE)
    layer = _layer(request)
    dataset, digest = load_dataset(body.dataset)
    details = {"dataset": body.dataset, "models": len(body.model_keys), "repeats": body.repeats}
    try:
        report = await layer.harness.run(dataset, digest, body.model_keys, repeats=body.repeats)
    except ValueError as exc:
        layer.audit(owner, "evaluation_run", succeeded=False, subject="evaluation", details={**details, "http_status": 422})
        raise HTTPException(status_code=422, detail="Unknown model keys or invalid repeats.") from exc
    layer.store_report(owner, report)
    layer.audit(owner, "evaluation_run", succeeded=True, subject="evaluation", details={
        **details, "report_id": report.report_id, "simulated": any(item.simulated for item in report.summaries)})
    return report.model_dump(mode="json")


@router.post("/evaluations/{report_id}/accept", responses=_ERRORS)
async def accept_evaluation(report_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.INTELLIGENCE_EVALUATE)
    layer = _layer(request)
    report = layer.report(owner, report_id)
    if report is None:
        layer.audit(owner, "evaluation_evidence_accept", succeeded=False, subject="evaluation",
                    details={"report_id": report_id[:40], "http_status": 404})
        raise HTTPException(status_code=404, detail="The evaluation report was not found.")
    accepted = layer.router.accept_evidence(report)
    layer.audit(owner, "evaluation_evidence_accept", succeeded=bool(accepted), subject="evaluation",
                details={"report_id": report_id, "accepted_models": len(accepted),
                         "refused_simulated": sum(item.simulated for item in report.summaries)})
    return {"accepted_models": list(accepted),
            "note": "Simulated (mock or demo) summaries are never accepted as routing evidence."}


# ---------------------------------------------------------------- v5.2

@router.post("/graph/entities", responses=_ERRORS)
async def upsert_entity(body: EntityInput, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    entity = await _mutate(request, owner, "kg_entity_upsert", f"entity:{body.entity_type}",
                           _layer(request).graph.upsert_entity, owner, body,
                           details={"entity_type": body.entity_type, "extractor": body.provenance.extractor},
                           describe=lambda item: {"entity_id": item.entity_id})
    return entity.model_dump(mode="json")


@router.get("/graph/entities", responses=_ERRORS)
async def search_entities(request: Request, text: str | None = Query(default=None, max_length=200),
                          entity_type: str | None = Query(default=None, max_length=40),
                          limit: int = Query(default=20, ge=1, le=100),
                          owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    items = await _call(_layer(request).graph.search, owner, text=text, entity_type=entity_type, limit=limit)
    return {"entities": [item.model_dump(mode="json") for item in items], "durable": _layer(request).graph.durable}


@router.get("/graph/entities/{entity_id}", responses=_ERRORS)
async def get_entity(entity_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    return (await _call(_layer(request).graph.get_entity, owner, entity_id)).model_dump(mode="json")


@router.delete("/graph/entities/{entity_id}", responses=_ERRORS)
async def delete_entity(entity_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    removed = await _mutate(request, owner, "kg_entity_delete", "entity", _layer(request).graph.delete_entity,
                            owner, entity_id, details={"entity_id": entity_id[:40]},
                            describe=lambda count: {"removed": count})
    return {"removed": removed}


@router.get("/graph/entities/{entity_id}/neighbors", responses=_ERRORS)
async def neighbors(entity_id: str, request: Request, depth: int = Query(default=1, ge=0, le=3),
                    max_entities: int = Query(default=50, ge=1, le=100), include_inferred: bool = True,
                    owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    view = await _call(_layer(request).graph.neighbors, owner, entity_id, depth=depth, max_entities=max_entities,
                       include_inferred=include_inferred)
    return view.model_dump(mode="json")


@router.post("/graph/relationships", responses=_ERRORS)
async def add_relationship(body: RelationshipInput, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    item = await _mutate(request, owner, "kg_relationship_add", f"relationship:{body.relation}",
                         _layer(request).graph.add_relationship, owner, body,
                         details={"relation": body.relation, "requested_status": body.status,
                                  "extractor": body.provenance.extractor},
                         describe=lambda rel: {"relationship_id": rel.relationship_id, "stored_status": rel.status,
                                               "conflicts": len(rel.conflicts_with)})
    return item.model_dump(mode="json")


class NoteBody(_Body):
    note: StrictStr | None = Field(default=None, max_length=500)


@router.post("/graph/relationships/{relationship_id}/resolve", responses=_ERRORS)
async def resolve_relationship(relationship_id: str, body: NoteBody, request: Request,
                               owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    item = await _mutate(request, owner, "kg_conflict_resolve", "relationship", _layer(request).graph.resolve_conflict,
                         owner, relationship_id, note=body.note, details={"relationship_id": relationship_id[:40]},
                         describe=lambda rel: {"stored_status": rel.status})
    return item.model_dump(mode="json")


@router.post("/graph/relationships/{relationship_id}/retract", responses=_ERRORS)
async def retract_relationship(relationship_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    item = await _mutate(request, owner, "kg_relationship_retract", "relationship", _layer(request).graph.retract,
                         owner, relationship_id, details={"relationship_id": relationship_id[:40]},
                         describe=lambda rel: {"stored_status": rel.status})
    return item.model_dump(mode="json")


@router.post("/graph/records", responses=_ERRORS)
async def ingest_record(body: KnowledgeRecord, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.KNOWLEDGE_GRAPH_WRITE)
    result = await _mutate(request, owner, "kg_record_ingest", f"record:{body.source_kind}", _layer(request).graph.ingest,
                           owner, body, details={"source_kind": body.source_kind, "extractor": body.extractor,
                                                 "entities": len(body.entities), "relationships": len(body.relationships)},
                           describe=lambda item: {"stored_relationships": len(item.relationships)})
    return result.model_dump(mode="json")


# ---------------------------------------------------------------- v5.3

class ContextPreviewBody(_Body):
    query: StrictStr = Field(min_length=1, max_length=2000)
    project_id: StrictStr | None = Field(default=None, max_length=80)
    model_key: StrictStr | None = Field(default=None, max_length=64)
    max_items: int = Field(default=12, ge=1, le=30)


@router.post("/context/preview", responses=_ERRORS)
async def context_preview(body: ContextPreviewBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.MEMORY_READ)
    layer = _layer(request)
    repository = getattr(request.app.state, "memory_repository", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="Memory storage requires JARVIS_PERSISTENCE_ENABLED=true.")
    model = layer.registry.get(body.model_key) if body.model_key else None
    if body.model_key and model is None:
        raise HTTPException(status_code=422, detail="Unknown model key.")
    source = RepositoryCandidateSource(repository)
    try:
        candidates = await run_in_threadpool(source.candidates, owner, body.query, limit=50)
    except Exception as exc:  # noqa: BLE001 - sanitized
        raise HTTPException(status_code=503, detail="Memory storage is unavailable.") from exc
    outdated = await _call(layer.memory_updates.outdated_refs, owner)
    package = assemble(ContextRequest(owner_id=owner, query=body.query, project_id=body.project_id,
                                      max_items=body.max_items), candidates, model=model, outdated_refs=outdated)
    return package.model_dump(mode="json")


class OutdatedBody(_Body):
    target_ref: StrictStr = Field(min_length=1, max_length=300)
    reason: StrictStr = Field(min_length=1, max_length=500)


@router.get("/memory/proposals", responses=_ERRORS)
async def memory_proposals(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.MEMORY_READ)
    layer = _layer(request)
    items = await _call(layer.memory_updates.list, owner)
    return {"proposals": [item.model_dump(mode="json") for item in items], "durable": layer.memory_updates.durable}


@router.post("/memory/proposals/outdated", responses=_ERRORS)
async def propose_outdated(body: OutdatedBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.MEMORY_WRITE)
    proposal = await _mutate(request, owner, "memory_proposal_create", "memory_proposal:mark_outdated",
                             _layer(request).memory_updates.propose_outdated, owner, target_ref=body.target_ref,
                             source_ref="owner:api", reason=body.reason, details={"kind": "mark_outdated"},
                             describe=lambda item: {"proposal_id": item.proposal_id})
    return proposal.model_dump(mode="json")


async def _decide_memory(proposal_id: str, request: Request, owner_id, accept: bool):
    owner = _owner(request, owner_id, Capability.MEMORY_WRITE)
    repository = getattr(request.app.state, "memory_repository", None)

    def applier(item):
        if repository is None:
            raise UpdateError(503, "Memory storage requires JARVIS_PERSISTENCE_ENABLED=true.")
        repository.create(owner, item.content, item.category, item.source_ref)

    item = await _mutate(request, owner, "memory_proposal_" + ("accept" if accept else "reject"), "memory_proposal",
                         _layer(request).memory_updates.decide, owner, proposal_id, accept=accept, applier=applier,
                         details={"proposal_id": proposal_id[:40]},
                         describe=lambda result: {"kind": result.kind, "stored_status": result.status,
                                                  "applied": result.applied_ref is not None})
    return item.model_dump(mode="json")


@router.post("/memory/proposals/{proposal_id}/accept", responses=_ERRORS)
async def accept_memory(proposal_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    return await _decide_memory(proposal_id, request, owner_id, True)


@router.post("/memory/proposals/{proposal_id}/reject", responses=_ERRORS)
async def reject_memory(proposal_id: str, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    return await _decide_memory(proposal_id, request, owner_id, False)


class InterruptedBody(_Body):
    applied: bool
    applied_ref: StrictStr | None = Field(default=None, max_length=300)


@router.post("/memory/proposals/{proposal_id}/resolve-interrupted", responses=_ERRORS)
async def resolve_interrupted_memory(proposal_id: str, body: InterruptedBody, request: Request,
                                     owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.MEMORY_WRITE)
    item = await _mutate(request, owner, "memory_proposal_resolve_interrupted", "memory_proposal",
                         _layer(request).memory_updates.resolve_interrupted, owner, proposal_id, applied=body.applied,
                         applied_ref=body.applied_ref, details={"proposal_id": proposal_id[:40], "applied": body.applied},
                         describe=lambda result: {"stored_status": result.status})
    return item.model_dump(mode="json")


# ---------------------------------------------------------------- v5.4

@router.post("/multimodal/inspect", responses=_ERRORS)
async def multimodal_inspect(body: MultimodalRequest, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    return (await _call(_layer(request).multimodal.inspect, body)).model_dump(mode="json")


# ---------------------------------------------------------------- v5.5

@router.post("/plans/evaluate", responses=_ERRORS)
async def plans_evaluate(body: PlanCandidate, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    return evaluate_plan(body).model_dump(mode="json")


@router.get("/reasoning/strategies", responses=_ERRORS)
async def strategies(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    return {"strategies": [item.model_dump(mode="json") for item in STRATEGIES.values()]}


class AnswerCheckBody(_Body):
    task_class: StrictStr = Field(max_length=40)
    answer: StrictStr = Field(min_length=1, max_length=40_000)


@router.post("/answers/check", responses=_ERRORS)
async def answers_check(body: AnswerCheckBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    _owner(request, owner_id)
    if body.task_class not in STRATEGIES:
        raise HTTPException(status_code=422, detail="Unknown task class.")
    return check_answer(body.task_class, body.answer).model_dump(mode="json")


# ---------------------------------------------------------------- v5.6

@router.get("/preferences", responses=_ERRORS)
async def preferences(request: Request, task_type: str = Query(default="general", max_length=40),
                      owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    service = _layer(request).personalization
    items = await _call(service.preferences, owner)
    effective = await _call(service.resolve, owner, task_type)
    return {"enabled": effective.enabled, "durable": service.durable,
            "preferences": [item.model_dump(mode="json") for item in items],
            "effective": effective.model_dump(mode="json"),
            "presentation": service.apply_to_presentation(effective).instruction()}


class PreferenceBody(_Body):
    value: StrictStr | list[StrictStr]
    scope: StrictStr = Field(default="global", max_length=40)


@router.put("/preferences/{key}", responses=_ERRORS)
async def set_preference(key: str, body: PreferenceBody, request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    item = await _mutate(request, owner, "preference_set", f"preference:{key[:40]}",
                         _layer(request).personalization.set_preference, owner, key, body.value, scope=body.scope,
                         details={"key": key[:40], "scope": body.scope})
    return item.model_dump(mode="json")


@router.delete("/preferences/{key}", responses=_ERRORS)
async def remove_preference(key: str, request: Request, scope: str = Query(default="global", max_length=40),
                            owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    await _mutate(request, owner, "preference_remove", f"preference:{key[:40]}",
                  _layer(request).personalization.remove_preference, owner, key, scope=scope,
                  details={"key": key[:40], "scope": scope})
    return {"removed": True}


@router.post("/preferences/disable", responses=_ERRORS)
async def disable_preferences(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    return {"enabled": await _mutate(request, owner, "personalization_disable", "personalization",
                                     _layer(request).personalization.set_enabled, owner, False)}


@router.post("/preferences/enable", responses=_ERRORS)
async def enable_preferences(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    return {"enabled": await _mutate(request, owner, "personalization_enable", "personalization",
                                     _layer(request).personalization.set_enabled, owner, True)}


@router.post("/preferences/reset", responses=_ERRORS)
async def reset_preferences(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    return {"removed": await _mutate(request, owner, "personalization_reset", "personalization",
                                     _layer(request).personalization.reset, owner,
                                     describe=lambda count: {"removed": count})}


class ObservationsBody(_Body):
    observations: tuple[Observation, ...] = Field(min_length=1, max_length=100)


@router.post("/preferences/observations", responses=_ERRORS)
async def preference_observations(body: ObservationsBody, request: Request,
                                  owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    created = await _mutate(request, owner, "preference_observations", "personalization",
                            _layer(request).personalization.propose_from_observations, owner, body.observations,
                            details={"observations": len(body.observations)},
                            describe=lambda items: {"proposals_created": len(items)})
    return {"proposals": [item.model_dump(mode="json") for item in created],
            "note": "Proposals change nothing until you accept them."}


@router.get("/preferences/proposals", responses=_ERRORS)
async def preference_proposals(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id)
    items = await _call(_layer(request).personalization.proposals, owner)
    return {"proposals": [item.model_dump(mode="json") for item in items]}


@router.post("/preferences/proposals/{proposal_id}/{decision}", responses=_ERRORS)
async def decide_preference(proposal_id: str, decision: Literal["accept", "reject"], request: Request,
                            owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(request, owner_id, Capability.PERSONALIZATION_MANAGE)
    item = await _mutate(request, owner, f"preference_proposal_{decision}", "preference_proposal",
                         _layer(request).personalization.decide, owner, proposal_id, accept=decision == "accept",
                         details={"proposal_id": proposal_id[:40]},
                         describe=lambda result: {"key": result.key, "scope": result.scope, "stored_status": result.status})
    return item.model_dump(mode="json")
