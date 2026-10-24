"""v5.x hardening: durable memory proposals, graph concurrency, central audit, adapters.

Evidence types (reported separately):
- unit and in-process concurrency tests use real threads against the
  in-memory stores;
- "PostgreSQL" tests here use a fake connection that records SQL; they prove
  statement shape, lock ordering and error mapping, NOT live database
  behaviour. Live PostgreSQL tests are in
  integration_tests/test_intelligence_postgres_integration.py (opt-in).
"""

import hashlib
import json
import math
import threading
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from database import runner
from jarvis.audit.stores import StorageError
from jarvis.contextual.models import ContextRequest, MemoryCandidate
from jarvis.contextual.selection import assemble
from jarvis.contextual.sources import RepositoryCandidateSource, checked_similarity, validate_candidates
from jarvis.contextual.updates import MemoryProposalStore, MemoryUpdateService, PostgresProposalStore, UpdateError
from jarvis.core.config import Settings
from jarvis.knowledge_graph.adapters import ExtractionError, document_record
from jarvis.knowledge_graph.models import EntityInput, Provenance, RelationshipInput
from jarvis.knowledge_graph.service import GraphError, GraphService
from jarvis.knowledge_graph.stores import MemoryGraphStore, PostgresGraphStore, lock_id
from jarvis.main import create_app
from jarvis.orchestration.models import ExecutionPlan, PlanStep
from jarvis.reasoning.adapters import NEUTRAL_FORMAT, PlanAdapterError, adapt_plan, from_neutral_plan
from jarvis.reasoning.evaluator import evaluate_plan
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER = Provenance(source_kind="user_statement", source_ref="chat:1", extractor="user")
MODEL = Provenance(source_kind="document", source_ref="doc:1", extractor="model")
TOKEN = "hardening-test-token-with-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def run_threads(*targets):
    results, errors = [None] * len(targets), [None] * len(targets)

    def wrap(index, target):
        try:
            results[index] = target()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            errors[index] = exc

    threads = [threading.Thread(target=wrap, args=(index, target)) for index, target in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "a worker deadlocked"
    return results, errors


# ================================================= durable memory proposals

class FlakyStore(MemoryProposalStore):
    """Fails the Nth transition to simulate a crash or storage loss."""

    def __init__(self, fail_on: int | None = None):
        super().__init__()
        self.fail_on, self.transitions = fail_on, 0

    def transition(self, *args, **kwargs):
        self.transitions += 1
        if self.transitions == self.fail_on:
            raise StorageError()
        return super().transition(*args, **kwargs)


def test_mark_outdated_decision_is_the_durable_state():
    store = MemoryProposalStore()
    first = MemoryUpdateService(store)
    proposal = first.propose_outdated(OWNER, target_ref="memory:42", source_ref="chat", reason="Upgraded")
    assert first.outdated_refs(OWNER) == frozenset()
    first.decide(OWNER, proposal.proposal_id, accept=True)
    second = MemoryUpdateService(store)  # a new service over the same store (in-process stand-in for a restart)
    assert second.outdated_refs(OWNER) == frozenset({"memory:42"}) and second.outdated_refs(OTHER) == frozenset()
    assert second.list(OWNER)[0].status == "accepted" and second.list(OWNER)[0].decided_at


def test_add_is_claimed_applied_then_finalized_and_failures_are_honest():
    written = []
    service = MemoryUpdateService(MemoryProposalStore())
    proposal = service.propose_add(OWNER, category="long_term", content="Prefers tea", source_ref="chat:1",
                                   reliability="user_stated")

    def failing(item):
        raise RuntimeError("database down")

    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=True, applier=failing)
    assert raised.value.status_code == 503 and "still pending" in raised.value.public_message
    pending = service.list(OWNER)[0]
    assert pending.status == "proposed" and pending.last_error == "apply_failed"

    class Created:
        id = "1234"

    accepted = service.decide(OWNER, proposal.proposal_id, accept=True,
                              applier=lambda item: written.append(item.status) or Created())
    assert accepted.status == "accepted" and accepted.applied_ref == "memory:1234" and written == ["applying"]


def test_interrupted_apply_is_visible_never_reapplied_and_owner_resolvable():
    store = FlakyStore(fail_on=2)  # claim succeeds, finalization fails (process "stops")
    service = MemoryUpdateService(store)
    proposal = service.propose_add(OWNER, category="long_term", content="Uses Windows", source_ref="chat:2",
                                   reliability="user_stated")
    calls = []
    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=True, applier=lambda item: calls.append(1))
    assert raised.value.status_code == 503
    stuck = service.list(OWNER)[0]
    assert stuck.status == "applying" and stuck.decided_at is None  # never reported as accepted
    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=True, applier=lambda item: calls.append(1))
    assert raised.value.status_code == 409 and calls == [1]  # not applied twice
    with pytest.raises(UpdateError):
        service.resolve_interrupted(OTHER, proposal.proposal_id, applied=True)
    resolved = service.resolve_interrupted(OWNER, proposal.proposal_id, applied=True, applied_ref="memory:9")
    assert resolved.status == "accepted" and resolved.applied_ref == "memory:9"


def test_storage_unavailable_records_no_decision_and_writes_nothing():
    service = MemoryUpdateService(FlakyStore(fail_on=1))
    proposal = service.propose_add(OWNER, category="long_term", content="x y z", source_ref="c", reliability="inferred")
    calls = []
    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=True, applier=calls.append)
    assert raised.value.status_code == 503 and calls == [] and service.list(OWNER)[0].status == "proposed"


def test_concurrent_decisions_apply_exactly_once():
    service = MemoryUpdateService(MemoryProposalStore())
    proposal = service.propose_add(OWNER, category="long_term", content="Works remotely", source_ref="c",
                                   reliability="user_stated")
    gate, calls = threading.Barrier(2), []

    def apply(item):
        calls.append(item.proposal_id)

    def decide():
        gate.wait(timeout=5)
        return service.decide(OWNER, proposal.proposal_id, accept=True, applier=apply)

    results, errors = run_threads(decide, decide)
    assert len(calls) == 1 and sum(result is not None for result in results) == 1
    assert [error.status_code for error in errors if error is not None] == [409]


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, log, rows=(), fail_sqlstate=None, exits=None):
        self.log, self.rows, self.fail_sqlstate, self.exits = log, list(rows), fail_sqlstate, exits

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if self.exits is not None:
            self.exits.append("rollback" if kind else "commit")
        return False

    def execute(self, query, params=()):
        text = " ".join(query.split())
        self.log.append((text, params))
        if self.fail_sqlstate and text.startswith("INSERT"):
            error = RuntimeError("constraint")
            error.sqlstate = self.fail_sqlstate
            raise error
        if "count(*)" in text and "kg_" in text:
            return FakeCursor([(0, 0)])
        return FakeCursor([] if "set_config" in text or "advisory" in text else self.rows)


def statements(log):
    return [entry for entry in log if "set_config" not in entry[0]]


def test_postgres_proposal_store_sql_is_conditional_and_owner_scoped():
    log = []
    store = PostgresProposalStore("postgresql://x", connect=lambda url, connect_timeout: FakeConnection(log))
    service = MemoryUpdateService(store)
    assert service.durable is True
    with pytest.raises(UpdateError) as raised:  # the fake returns no row: the pending-limit insert "did not happen"
        service.propose_outdated(OWNER, target_ref="memory:1", source_ref="c", reason="r")
    assert raised.value.status_code == 409
    insert = statements(log)[0][0]
    assert "WHERE (SELECT count(*) FROM jarvis_app.memory_update_proposals WHERE owner_id = %s" in insert
    store.transition(OWNER, "MP-" + "a" * 16, expected="proposed", status="accepted", decided_at="2026-01-01")
    update = statements(log)[-1]
    assert "WHERE owner_id = %s AND id = %s AND status = %s" in update[0] and update[1][-3:] == (OWNER, "MP-" + "a" * 16, "proposed")
    store.outdated_refs(OWNER)
    assert "kind = 'mark_outdated' AND status = 'accepted'" in statements(log)[-1][0] and statements(log)[-1][1] == (OWNER,)


# ===================================================== graph concurrency

def ent(service, kind, name, owner=OWNER):
    return service.upsert_entity(owner, EntityInput(entity_type=kind, name=name, provenance=USER))


def rel(subject, relation, target, status="verified", provenance=None):
    return RelationshipInput(subject_id=subject.entity_id, relation=relation, object_id=target.entity_id, status=status,
                             confidence=1.0 if status == "verified" else 0.6, provenance=provenance or (USER if status == "verified" else MODEL))


class PausingStore(MemoryGraphStore):
    """Pauses inside the critical section so a racing writer can try to interleave."""

    def __init__(self):
        super().__init__()
        self.pause = None

    def find_relationships(self, owner_id, subject_id, relation):
        result = super().find_relationships(owner_id, subject_id, relation)
        if self.pause is not None:
            try:
                self.pause.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
        return result


class UnlockedStore(PausingStore):
    """Negative control: identical store without keyed locks."""

    @contextmanager
    def atomic(self, owner_id, keys=()):
        yield self


def test_negative_control_without_locks_the_functional_race_is_real():
    service = GraphService(UnlockedStore())
    task, alpha, beta = ent(service, "task", "t"), ent(service, "project", "alpha"), ent(service, "project", "beta")
    service.store.pause = threading.Barrier(2)
    results, errors = run_threads(lambda: service.add_relationship(OWNER, rel(task, "belongs_to", alpha)),
                                  lambda: service.add_relationship(OWNER, rel(task, "belongs_to", beta)))
    assert errors == [None, None] and [item.status for item in results] == ["verified", "verified"]  # broken invariant


def test_concurrent_conflicting_verified_facts_are_both_disputed():
    service = GraphService(PausingStore())
    task, alpha, beta = ent(service, "task", "t"), ent(service, "project", "alpha"), ent(service, "project", "beta")
    service.store.pause = threading.Barrier(2)
    results, errors = run_threads(lambda: service.add_relationship(OWNER, rel(task, "belongs_to", alpha)),
                                  lambda: service.add_relationship(OWNER, rel(task, "belongs_to", beta)))
    assert errors == [None, None]
    stored = service.store.find_relationships(OWNER, task.entity_id, "belongs_to")
    assert sorted(item.status for item in stored) == ["disputed", "disputed"]
    assert {stored[0].relationship_id} == set(stored[1].conflicts_with)


def test_concurrent_inference_never_downgrades_a_verified_fact():
    for _ in range(5):
        service = GraphService(PausingStore())
        doc, alice, bob = ent(service, "document", "spec"), ent(service, "person", "alice"), ent(service, "person", "bob")
        service.store.pause = threading.Barrier(2)
        run_threads(lambda: service.add_relationship(OWNER, rel(doc, "created_by", alice)),
                    lambda: service.add_relationship(OWNER, rel(doc, "created_by", bob, status="inferred")))
        stored = {item.object_id: item for item in service.store.find_relationships(OWNER, doc.entity_id, "created_by")}
        assert stored[alice.entity_id].status == "verified"
        assert stored[bob.entity_id].status in {"disputed", "superseded"}


def test_concurrent_duplicate_entities_and_dependency_cycles():
    service = GraphService(MemoryGraphStore())
    gate = threading.Barrier(4)

    def create():
        gate.wait(timeout=5)
        return service.upsert_entity(OWNER, EntityInput(entity_type="service", name="API", provenance=USER)).entity_id

    ids, errors = run_threads(create, create, create, create)
    assert errors == [None] * 4 and len(set(ids)) == 1 and service.store.count(OWNER)[0] == 1
    a, b = ent(service, "software_component", "a"), ent(service, "software_component", "b")
    gate = threading.Barrier(2)

    def link(x, y):
        def run():
            gate.wait(timeout=5)
            return service.add_relationship(OWNER, rel(x, "depends_on", y))
        return run

    results, errors = run_threads(link(a, b), link(b, a))
    assert sum(item is not None for item in results) == 1
    assert [error.status_code for error in errors if error is not None] == [422]


def test_delete_racing_with_relationship_writes_leaves_no_orphans():
    for _ in range(10):
        service = GraphService(MemoryGraphStore())
        api, db = ent(service, "service", "api"), ent(service, "service", "db")
        gate = threading.Barrier(2)

        def add():
            gate.wait(timeout=5)
            return service.add_relationship(OWNER, rel(api, "depends_on", db))

        def delete():
            gate.wait(timeout=5)
            return service.delete_entity(OWNER, db.entity_id)

        results, errors = run_threads(add, delete)
        for error in errors:
            assert error is None or (isinstance(error, GraphError) and error.status_code in {404, 409})
        for item in service.store._relationships.values():
            assert service.store.get_entity(OWNER, item.object_id) is not None


def test_unrelated_updates_do_not_share_a_lock():
    store = PausingStore()
    service = GraphService(store)
    t1, t2, p = ent(service, "task", "one"), ent(service, "task", "two"), ent(service, "project", "p")
    store.pause = threading.Barrier(2)  # both must be inside their critical sections at once to pass quickly
    results, errors = run_threads(lambda: service.add_relationship(OWNER, rel(t1, "belongs_to", p)),
                                  lambda: service.add_relationship(OWNER, rel(t2, "belongs_to", p)))
    assert errors == [None, None] and not store.pause.broken  # they met inside: no global serialization


def test_cross_owner_conflict_resolution_and_retraction_are_refused():
    service = GraphService(MemoryGraphStore())
    task, alpha, beta = ent(service, "task", "t"), ent(service, "project", "a"), ent(service, "project", "b")
    service.add_relationship(OWNER, rel(task, "belongs_to", alpha))
    disputed = service.add_relationship(OWNER, rel(task, "belongs_to", beta))
    for call in (lambda: service.resolve_conflict(OTHER, disputed.relationship_id),
                 lambda: service.retract(OTHER, disputed.relationship_id)):
        with pytest.raises(GraphError) as raised:
            call()
        assert raised.value.status_code == 404
    assert service.store.get_relationship(OWNER, disputed.relationship_id).status == "disputed"
    assert lock_id(OWNER, "rel:x:belongs_to") != lock_id(OTHER, "rel:x:belongs_to")


def test_postgres_atomic_takes_sorted_advisory_locks_on_one_connection_and_rolls_back():
    log, exits, connections = [], [], []

    def connect(url, connect_timeout):
        connections.append(1)
        return FakeConnection(log, exits=exits)

    store = PostgresGraphStore("postgresql://x", connect=connect)
    with store.atomic(OWNER, ("rel:b:depends_on", "dep", "rel:a:depends_on")) as tx:
        tx.lock(("quota:relationships",))
        tx.find_relationships(OWNER, "KE-" + "a" * 16, "depends_on")
    locks = [params[0] for query, params in statements(log) if "pg_advisory_xact_lock" in query]
    assert locks[:3] == [lock_id(OWNER, key) for key in sorted(("rel:b:depends_on", "dep", "rel:a:depends_on"))]
    assert locks[3] == lock_id(OWNER, "quota:relationships") and len(connections) == 1 and exits == ["commit"]
    with pytest.raises(ValueError):
        with store.atomic(OWNER, ("rel:a:x",)):
            raise ValueError("business rule failed")
    assert exits[-1] == "rollback"


def test_postgres_constraint_races_become_409_conflicts_with_nothing_changed():
    exits = []
    store = PostgresGraphStore("postgresql://x",
                               connect=lambda url, connect_timeout: FakeConnection([], fail_sqlstate="23505", exits=exits))
    service = GraphService(store)
    with pytest.raises(GraphError) as raised:
        service.upsert_entity(OWNER, EntityInput(entity_type="service", name="api", provenance=USER))
    assert raised.value.status_code == 409 and "concurrently" in raised.value.public_message and exits == ["rollback"]
    fk = PostgresGraphStore("postgresql://x", connect=lambda url, connect_timeout: FakeConnection([], fail_sqlstate="23503"))
    memory = GraphService(MemoryGraphStore())
    a, b = ent(memory, "service", "a"), ent(memory, "service", "b")
    edge = memory.add_relationship(OWNER, rel(a, "depends_on", b))
    with pytest.raises(StorageError) as raised:
        fk.save_relationship(edge)
    assert type(raised.value).__name__ == "StorageConflict"


def test_ingest_is_all_or_nothing():
    service = GraphService(MemoryGraphStore())
    a, b = ent(service, "software_component", "a"), ent(service, "software_component", "b")
    service.add_relationship(OWNER, rel(a, "depends_on", b))
    before = service.store.count(OWNER)
    record = document_record("doc:design.md", extractor="model", entities=(
        {"ref": "a", "entity_type": "software_component", "name": "a"},
        {"ref": "b", "entity_type": "software_component", "name": "b"},
        {"ref": "c", "entity_type": "software_component", "name": "c"},
    ), relationships=(
        {"subject_ref": "c", "relation": "depends_on", "object_ref": "a", "status": "inferred", "confidence": 0.5},
        {"subject_ref": "b", "relation": "depends_on", "object_ref": "a", "status": "inferred", "confidence": 0.5},
    ))
    with pytest.raises(GraphError) as raised:
        service.ingest(OWNER, record)  # second relationship would close a cycle
    assert raised.value.status_code == 422
    assert service.store.count(OWNER) == before  # entity "c" and the first relationship were rolled back
    assert service.search(OWNER, text="c", entity_type="software_component") == ()


# ================================================================ audit

def make_client():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    return app, TestClient(app, headers=HEADERS)


def intelligence_events(client):
    return client.get("/activity", params={"category": "intelligence", "limit": 200}).json()["events"]


def test_successful_and_failed_mutations_are_audited_without_private_content():
    app, client = make_client()
    with client:
        secret_name = "Project Zeus Secret Codename"
        entity = client.post("/intelligence/graph/entities", json={
            "entity_type": "project", "name": secret_name, "provenance": USER.model_dump()}).json()
        other = client.post("/intelligence/graph/entities", json={
            "entity_type": "person", "name": "Alice Example", "provenance": USER.model_dump()}).json()
        bad = client.post("/intelligence/graph/relationships", json={
            "subject_id": entity["entity_id"], "relation": "describes", "object_id": other["entity_id"],
            "status": "verified", "confidence": 1.0, "provenance": USER.model_dump()})
        assert bad.status_code == 422
        client.put("/intelligence/preferences/language", json={"value": "te"})
        client.post("/intelligence/preferences/reset")
        proposal = client.post("/intelligence/memory/proposals/outdated", json={
            "target_ref": "memory:1", "reason": "My salary changed to 99999"}).json()
        client.post(f"/intelligence/memory/proposals/{proposal['proposal_id']}/accept")
        report = client.post("/intelligence/evaluations", json={"model_keys": ["mock-deterministic"]}).json()
        client.post(f"/intelligence/evaluations/{report['report_id']}/accept")
        events = intelligence_events(client)
    by_type = {}
    for item in events:
        by_type.setdefault(item["event_type"], []).append(item)
    assert {"kg_entity_upsert", "kg_relationship_add", "preference_set", "personalization_reset",
            "memory_proposal_create", "memory_proposal_accept", "evaluation_run", "evaluation_evidence_accept"} <= set(by_type)
    failed = by_type["kg_relationship_add"][0]
    assert failed["status"] == "failed" and failed["details"]["mutated"] is False and failed["details"]["http_status"] == 422
    assert all(item["status"] == "succeeded" for item in by_type["kg_entity_upsert"])
    evidence = by_type["evaluation_evidence_accept"][0]
    assert evidence["status"] == "failed" and evidence["details"]["refused_simulated"] == 1  # simulated never accepted
    dumped = json.dumps(events)
    for private in (secret_name, "Alice Example", "99999", '"te"', TOKEN):
        assert private not in dumped
    assert all(item["category"] == "intelligence" and item["owner_id"] == str(OWNER) for item in events)


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def test_unauthorized_attempts_are_refused_before_any_mutation_or_audit():
    app, client = make_client()
    with client:
        app.state.authorization_policy = DenyPolicy({Capability.KNOWLEDGE_GRAPH_WRITE})
        response = client.post("/intelligence/graph/entities", json={
            "entity_type": "service", "name": "api", "provenance": USER.model_dump()})
        assert response.status_code == 403
        app.state.authorization_policy = CapabilityPolicy(OWNER)
        assert intelligence_events(client) == []
        assert client.get("/intelligence/graph/entities").json()["entities"] == []
    with TestClient(app) as anonymous:
        assert anonymous.post("/intelligence/preferences/reset").status_code == 401
        assert anonymous.post("/intelligence/memory/proposals/MP-0000000000000000/resolve-interrupted",
                              json={"applied": True}).status_code == 401


def test_resolve_interrupted_endpoint_and_audit():
    app, client = make_client()
    with client:
        service = app.state.intelligence.memory_updates
        proposal = service.propose_add(OWNER, category="long_term", content="likes jazz", source_ref="c",
                                       reliability="user_stated")
        service.store.transition(OWNER, proposal.proposal_id, expected="proposed", status="applying", decided_at=None)
        response = client.post(f"/intelligence/memory/proposals/{proposal.proposal_id}/resolve-interrupted",
                               json={"applied": False})
        assert response.status_code == 200 and response.json()["status"] == "proposed"
        again = client.post(f"/intelligence/memory/proposals/{proposal.proposal_id}/resolve-interrupted",
                            json={"applied": False})
        assert again.status_code == 409
        types = [(item["event_type"], item["status"]) for item in intelligence_events(client)]
    assert ("memory_proposal_resolve_interrupted", "succeeded") in types
    assert ("memory_proposal_resolve_interrupted", "failed") in types


# ============================================================== adapters

def test_neutral_plan_adapter_validates_and_feeds_the_evaluator():
    document = {"format": NEUTRAL_FORMAT, "version": 1, "goal": "Rotate the API key safely",
                "constraints": {"available_inputs": ["key_id"]},
                "nodes": [{"id": "check", "description": "Check which services use the API key", "inputs": ["key_id"],
                           "outputs": ["usage"], "completion_criteria": "every consumer of the key is listed"},
                          {"id": "rotate", "description": "Request approval to rotate the API key", "inputs": ["usage"],
                           "outputs": ["approval"], "action_kind": "consequential", "requires_approval": True,
                           "completion_criteria": "approval request recorded with its identifier"}],
                "edges": [{"from": "check", "to": "rotate"}]}
    plan = adapt_plan(document)
    assert plan.steps[1].depends_on == ("check",) and evaluate_plan(plan).verdict == "pass"
    for broken in ({**document, "version": 2}, {**document, "edges": [{"from": "check", "to": "nowhere"}]},
                   {**document, "nodes": []}, {**document, "nodes": [{"id": "Bad Id", "description": "x"}]}):
        with pytest.raises(PlanAdapterError):
            from_neutral_plan(broken)
    with pytest.raises(PlanAdapterError):
        adapt_plan(object())
    existing = ExecutionPlan(execution_id="e", goal="g", intent="analysis", steps=(
        PlanStep(step_id="s1", expert_id="analysis", round_number=1, expected_evidence="analysis"),), stop_condition="done")
    assert adapt_plan(existing).steps[0].step_id == "s1"


def test_document_record_keeps_provenance_and_refuses_verified_extraction():
    entities = ({"ref": "api", "entity_type": "service", "name": "API"},
                {"ref": "db", "entity_type": "service", "name": "DB"})
    good = document_record("doc:runbook.md", extractor="model", entities=entities, relationships=(
        {"subject_ref": "api", "relation": "depends_on", "object_ref": "db", "status": "inferred", "confidence": 0.7,
         "excerpt": "the API reads from the DB"},))
    service = GraphService(MemoryGraphStore())
    stored = service.ingest(OWNER, good).relationships[0]
    assert stored.status == "inferred" and stored.provenance[0].source_ref == "doc:runbook.md"
    assert stored.provenance[0].source_kind == "document" and stored.provenance[0].excerpt == "the API reads from the DB"
    for extractor in ("model", "rule"):
        with pytest.raises(ExtractionError):
            document_record("doc:x", extractor=extractor, entities=entities, relationships=(
                {"subject_ref": "api", "relation": "depends_on", "object_ref": "db", "status": "verified", "confidence": 1.0},))
    with pytest.raises(ExtractionError):
        document_record("  ", extractor="model", entities=entities, relationships=())
    upgraded = service.add_relationship(OWNER, RelationshipInput(
        subject_id=stored.subject_id, relation="depends_on", object_id=stored.object_id, status="verified",
        confidence=1.0, provenance=USER))
    assert upgraded.relationship_id == stored.relationship_id and upgraded.status == "verified"  # owner promotion


def test_semantic_adapter_boundary_validates_scores_and_candidates():
    bad_values = iter([math.nan, 2.0, -1, "0.5", True])

    def scorer(query, content):
        value = next(bad_values, None)
        if value is None:
            raise RuntimeError("embedding service down")
        return value

    scorer.name = "test-embedding"
    checked = checked_similarity(scorer)
    assert [checked("q", "c") for _ in range(6)] == [0.0] * 6 and checked.invalid == 6
    candidates = [MemoryCandidate(candidate_id=str(i), owner_id=OWNER, category="long_term",
                                  content=f"database note {i}", source_ref=f"memory:{i}") for i in range(3)]
    package = assemble(ContextRequest(owner_id=OWNER, query="database note"), candidates,
                       similarity=checked_similarity(lambda q, c: math.inf))
    assert package.items == () and "invalid similarity" in package.note
    kept, rejected = validate_candidates(OWNER, [*candidates, {"candidate_id": "x"}, candidates[0].model_copy(
        update={"owner_id": OTHER})])
    assert len(kept) == 3 and rejected == 2

    class Repo:
        def search(self, owner_id, query, *, limit, offset):
            from jarvis.memory.models import MemoryPage
            return MemoryPage(items=(), total=0, limit=limit, offset=offset, has_more=False)

    assert RepositoryCandidateSource(Repo()).candidates(OWNER, "q", limit=999) == []


# ============================================================ migrations

HISTORICAL = {
    "0006_audit_and_approvals": "468bc7e8d0f62d12bdc0251ea4f65a53715a5fc62534d00bd8413fc65cf984ae",
    "0052_knowledge_graph": "93504325f6ca6496acd73ed93791627e49c58caedcb10bf45463e4d492085fec",
    "0056_personalization": "9d696bcfd0dc98a38716813993f5843176871f14b7af740d67b3fe663fdfe08b",
}


def test_forward_migrations_are_added_and_historical_files_are_unchanged():
    found = {migration_id: checksum for migration_id, _, _, checksum in runner._migration_files(runner.MIGRATIONS_DIR)}
    for migration_id, checksum in HISTORICAL.items():
        assert found[migration_id] == checksum
    assert {"0057_memory_update_proposals", "0058_audit_intelligence_category"} <= set(found)
    ordered = sorted(found)
    assert ordered.index("0056_personalization") < ordered.index("0057_memory_update_proposals") \
        < ordered.index("0058_audit_intelligence_category")
    # Later migrations may widen the audit category check again (another workstream's category), but every
    # migration that replaces the check must keep all categories allowed before it, including 'intelligence'.
    required = ["'tool'", "'browser'", "'approval'", "'intelligence'"]
    for migration_id in ordered[ordered.index("0058_audit_intelligence_category"):]:
        sql = (runner.MIGRATIONS_DIR / f"{migration_id}.sql").read_text(encoding="utf-8")
        if "audit_events_category_allowed" in sql and "ADD CONSTRAINT" in sql:
            for category in required:
                assert category in sql, f"{migration_id} drops audit category {category}"
    proposals_sql = (runner.MIGRATIONS_DIR / "0057_memory_update_proposals.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE" not in proposals_sql and "REFERENCES" not in proposals_sql.split("--")[-1]
    assert hashlib.sha256(b"").hexdigest() not in found.values()
