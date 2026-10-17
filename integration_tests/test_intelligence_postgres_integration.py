"""Opt-in live PostgreSQL tests for the v5.x intelligence layer (migrations 0052-0058).

Runs only with JARVIS_RUN_POSTGRES_INTEGRATION=1 and the same password-free
loopback admin URL and PGPASSFILE prerequisites as test_postgres_integration.py,
whose isolated-database fixture is reused (a generated database is created,
migrated with the real runner, and dropped afterwards).
"""

import threading
from uuid import UUID

import pytest

from test_postgres_integration import isolated_postgres  # noqa: F401 - shared session fixture

from jarvis.audit.models import AuditRecord
from jarvis.audit.stores import PostgresAuditStore
from jarvis.contextual.updates import MemoryUpdateService, PostgresProposalStore
from jarvis.knowledge_graph.models import EntityInput, KnowledgeRecord, Provenance, RelationshipInput
from jarvis.knowledge_graph.service import GraphError, GraphService
from jarvis.knowledge_graph.stores import PostgresGraphStore

OWNER = UUID("c1a4d5e6-0000-4000-8000-000000000001")
OTHER = UUID("c1a4d5e6-0000-4000-8000-000000000002")
USER = Provenance(source_kind="user_statement", source_ref="it", extractor="user")


def graph(url):
    return GraphService(PostgresGraphStore(url, operation_timeout_seconds=10))


def threads(*targets):
    results, errors = [None] * len(targets), [None] * len(targets)
    gate = threading.Barrier(len(targets))

    def run(index, target):
        gate.wait(timeout=10)
        try:
            results[index] = target()
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    workers = [threading.Thread(target=run, args=(i, t)) for i, t in enumerate(targets)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    return results, errors


def entity(service, kind, name, owner=OWNER):
    return service.upsert_entity(owner, EntityInput(entity_type=kind, name=name, provenance=USER))


def test_outdated_decisions_survive_a_new_store_and_connection(isolated_postgres):
    url = isolated_postgres.database_url
    first = MemoryUpdateService(PostgresProposalStore(url))
    proposal = first.propose_outdated(OWNER, target_ref="memory:live-1", source_ref="it", reason="Upgraded")
    first.decide(OWNER, proposal.proposal_id, accept=True)
    restarted = MemoryUpdateService(PostgresProposalStore(url))
    assert restarted.outdated_refs(OWNER) == frozenset({"memory:live-1"})
    assert restarted.outdated_refs(OTHER) == frozenset()
    results, errors = threads(*(lambda: restarted.decide(OWNER, proposal.proposal_id, accept=False) for _ in range(3)))
    assert all(error is not None and error.status_code == 409 for error in errors)


def test_concurrent_conflicting_verified_facts_are_disputed_across_services(isolated_postgres):
    url = isolated_postgres.database_url
    setup = graph(url)
    task, alpha, beta = entity(setup, "task", "live-task"), entity(setup, "project", "live-a"), entity(setup, "project", "live-b")
    one, two = graph(url), graph(url)  # separate services and connections, like two server processes
    results, errors = threads(
        lambda: one.add_relationship(OWNER, RelationshipInput(subject_id=task.entity_id, relation="belongs_to",
                                                              object_id=alpha.entity_id, status="verified",
                                                              confidence=1.0, provenance=USER)),
        lambda: two.add_relationship(OWNER, RelationshipInput(subject_id=task.entity_id, relation="belongs_to",
                                                              object_id=beta.entity_id, status="verified",
                                                              confidence=1.0, provenance=USER)))
    assert errors == [None, None]
    stored = setup.store.find_relationships(OWNER, task.entity_id, "belongs_to")
    assert sorted(item.status for item in stored) == ["disputed", "disputed"]


def test_concurrent_duplicate_entities_and_cycles(isolated_postgres):
    url = isolated_postgres.database_url
    services = [graph(url) for _ in range(4)]
    ids, errors = threads(*(lambda s=s: entity(s, "service", "live-dup").entity_id for s in services))
    assert errors == [None] * 4 and len(set(ids)) == 1
    a, b = entity(services[0], "software_component", "live-a"), entity(services[0], "software_component", "live-b")
    results, errors = threads(
        lambda: services[1].add_relationship(OWNER, RelationshipInput(subject_id=a.entity_id, relation="depends_on",
                                                                      object_id=b.entity_id, status="verified",
                                                                      confidence=1.0, provenance=USER)),
        lambda: services[2].add_relationship(OWNER, RelationshipInput(subject_id=b.entity_id, relation="depends_on",
                                                                      object_id=a.entity_id, status="verified",
                                                                      confidence=1.0, provenance=USER)))
    assert sum(item is not None for item in results) == 1
    assert [error.status_code for error in errors if error is not None] == [422]


def test_ingest_rolls_back_entirely_and_cross_owner_edges_are_impossible(isolated_postgres):
    url = isolated_postgres.database_url
    service = graph(url)
    a, b = entity(service, "software_component", "live-x"), entity(service, "software_component", "live-y")
    service.add_relationship(OWNER, RelationshipInput(subject_id=a.entity_id, relation="depends_on", object_id=b.entity_id,
                                                      status="verified", confidence=1.0, provenance=USER))
    before = service.store.count(OWNER)
    record = KnowledgeRecord(source_kind="document", source_ref="doc:live", extractor="model", entities=(
        {"ref": "x", "entity_type": "software_component", "name": "live-x"},
        {"ref": "y", "entity_type": "software_component", "name": "live-y"},
        {"ref": "z", "entity_type": "software_component", "name": "live-z"},
    ), relationships=(
        {"subject_ref": "z", "relation": "depends_on", "object_ref": "x", "status": "inferred", "confidence": 0.5},
        {"subject_ref": "y", "relation": "depends_on", "object_ref": "x", "status": "inferred", "confidence": 0.5},
    ))
    with pytest.raises(GraphError):
        service.ingest(OWNER, record)
    assert service.store.count(OWNER) == before
    foreign = entity(service, "service", "live-foreign", owner=OTHER)
    edge = service.add_relationship(OWNER, RelationshipInput(subject_id=a.entity_id, relation="related_to",
                                                             object_id=b.entity_id, status="verified", confidence=1.0,
                                                             provenance=USER))
    with pytest.raises(Exception):
        service.store.save_relationship(edge.model_copy(update={"relationship_id": "KR-" + "f" * 16,
                                                                "object_id": foreign.entity_id}))


def test_intelligence_audit_category_is_accepted_by_migration_0058(isolated_postgres):
    store = PostgresAuditStore(isolated_postgres.database_url)
    store.append(AuditRecord(event_id="AE-" + "1" * 16, occurred_at="2026-10-17T00:00:00.000+00:00", owner_id=OWNER,
                             category="intelligence", event_type="kg_entity_upsert", decision="allowed",
                             status="succeeded", subject="entity:service", details={"mutated": True}, durable=True))
    assert store.recent(OWNER, limit=5, category="intelligence")[0].event_type == "kg_entity_upsert"
