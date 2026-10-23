"""Opt-in live PostgreSQL tests for the v5.x intelligence layer (migrations 0052-0058).

Runs only with JARVIS_RUN_POSTGRES_INTEGRATION=1 and the same password-free
loopback admin URL and PGPASSFILE prerequisites as test_postgres_integration.py,
whose isolated-database fixture is reused (a generated database is created,
migrated with the real runner, and dropped afterwards).
"""

import threading
from uuid import UUID

import pytest



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


# ------------------------------------------------- added for live validation

def test_upgrade_from_v30_migration_state_applies_0052_to_0058(isolated_postgres):
    """A database migrated to the v3.0 set (0001-0006) upgrades cleanly and keeps its audit rows."""
    import shutil
    import tempfile
    from pathlib import Path
    from uuid import uuid4

    import psycopg
    from psycopg import sql

    from database import runner

    name = f"jarvis_it_{uuid4().hex}"
    config = runner.DatabaseConfig(
        host=isolated_postgres.migration_config.host, port=isolated_postgres.migration_config.port, database=name,
        username=isolated_postgres.migration_config.username, maintenance_database="postgres",
        pg_bin_dir=isolated_postgres.migration_config.pg_bin_dir,
        pgpass_file=isolated_postgres.migration_config.pgpass_file,
        backup_dir=isolated_postgres.migration_config.backup_dir)
    url = isolated_postgres.database_url.rsplit("/", 1)[0] + "/" + name
    created = False
    try:
        with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="jarvis-v30-migrations-") as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            for migration in sorted(runner.MIGRATIONS_DIR.glob("000[1-6]_*.sql")):
                shutil.copy2(migration, scripts / migration.name)
            assert runner.install(config, database_dir=Path(temporary)) == 0
        with psycopg.connect(url) as connection:
            before = [row[0] for row in connection.execute(
                "SELECT migration_id FROM jarvis_meta.schema_migrations ORDER BY migration_id")]
            connection.execute("""INSERT INTO jarvis_app.audit_events
                (id, owner_id, occurred_at, category, event_type, decision, status, subject, details)
                VALUES (%s, %s, now(), 'tool', 'tool_call', 'allowed', 'succeeded', 'calculator', '{}'::jsonb)""",
                               ("AE-" + "2" * 16, OWNER))
        assert before == ["0001_migration_metadata", "0002_conversation_persistence", "0003_long_term_memories",
                          "0004_memory_ownership", "0005_memory_source_kind", "0006_audit_and_approvals"]
        assert runner.install(config) == 0  # applies only the pending v5 migrations
        assert runner.verify(config) == 0
        with psycopg.connect(url) as connection:
            after = [row[0] for row in connection.execute(
                "SELECT migration_id FROM jarvis_meta.schema_migrations ORDER BY migration_id")]
            # The runner applies every pending local migration in order (on the combined code base this also
            # includes the v3.x migrations 0007-0013 and 0059), and nothing already applied is re-applied.
            local = sorted(migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR))
            assert after == local and after[:len(before)] == before
            assert {"0052_knowledge_graph", "0056_personalization", "0057_memory_update_proposals",
                    "0058_audit_intelligence_category"} <= set(after)
            assert connection.execute("SELECT category FROM jarvis_app.audit_events WHERE id = %s",
                                      ("AE-" + "2" * 16,)).fetchone() == ("tool",)
            connection.execute("""INSERT INTO jarvis_app.audit_events
                (id, owner_id, occurred_at, category, event_type, decision, status, subject, details)
                VALUES (%s, %s, now(), 'intelligence', 'kg_entity_upsert', 'allowed', 'succeeded', 'entity', '{}'::jsonb)""",
                               ("AE-" + "3" * 16, OWNER))
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(url) as connection:
                connection.execute("""INSERT INTO jarvis_app.audit_events
                    (id, owner_id, occurred_at, category, event_type, decision, status, subject, details)
                    VALUES (%s, %s, now(), 'unknown', 'x', 'allowed', 'succeeded', 'x', '{}'::jsonb)""",
                                   ("AE-" + "4" * 16, OWNER))
    finally:
        if created:
            with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
                              "AND pid <> pg_backend_pid()", (name,))
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_interrupted_add_is_recovered_by_a_new_process(isolated_postgres):
    url = isolated_postgres.database_url
    writes = []

    class Crash(Exception):
        pass

    class CrashingStore(PostgresProposalStore):
        """Stops after the memory write, before finalization (simulated process death)."""

        def transition(self, owner_id, proposal_id, *, expected, status, **kwargs):
            if expected == "applying" and status == "accepted":
                raise Crash()
            return super().transition(owner_id, proposal_id, expected=expected, status=status, **kwargs)

    crashing = MemoryUpdateService(CrashingStore(url))
    proposal = crashing.propose_add(OWNER, category="long_term", content="live interrupted add", source_ref="it",
                                    reliability="user_stated")
    with pytest.raises(Crash):
        crashing.decide(OWNER, proposal.proposal_id, accept=True, applier=lambda item: writes.append(item.proposal_id))
    assert writes == [proposal.proposal_id]
    restarted = MemoryUpdateService(PostgresProposalStore(url))
    stuck = next(item for item in restarted.list(OWNER) if item.proposal_id == proposal.proposal_id)
    assert stuck.status == "applying" and stuck.decided_at is None
    with pytest.raises(Exception) as raised:
        restarted.decide(OWNER, proposal.proposal_id, accept=True, applier=lambda item: writes.append("again"))
    assert getattr(raised.value, "status_code", None) == 409 and writes == [proposal.proposal_id]
    with pytest.raises(Exception):
        restarted.resolve_interrupted(OTHER, proposal.proposal_id, applied=True)
    resolved = restarted.resolve_interrupted(OWNER, proposal.proposal_id, applied=True, applied_ref="memory:live")
    assert resolved.status == "accepted" and resolved.decided_at is not None
    assert MemoryUpdateService(PostgresProposalStore(url)).list(OWNER, status="accepted")


def test_per_owner_quota_is_exact_under_concurrent_creation(isolated_postgres):
    url = isolated_postgres.database_url
    quota_owner = UUID("c1a4d5e6-0000-4000-8000-0000000000aa")
    services = [GraphService(PostgresGraphStore(url, operation_timeout_seconds=10), max_entities=3) for _ in range(6)]
    results, errors = threads(*(lambda s=s, i=i: entity(s, "concept", f"quota-{i}", owner=quota_owner).entity_id
                                for i, s in enumerate(services)))
    assert sum(item is not None for item in results) == 3
    assert sorted(error.status_code for error in errors if error is not None) == [409, 409, 409]
    assert services[0].store.count(quota_owner)[0] == 3
    assert entity(services[0], "concept", "quota-other-owner", owner=OTHER).entity_id  # other owners unaffected


def test_constraint_violations_map_to_conflicts_and_api_audit_is_durable(isolated_postgres):
    from jarvis.knowledge_graph.stores import StorageConflict

    url = isolated_postgres.database_url
    service = graph(url)
    first = entity(service, "service", "live-conflict")
    duplicate = first.model_copy(update={"entity_id": "KE-" + "e" * 16})
    with pytest.raises(StorageConflict):
        service.store.save_entity(duplicate)  # unique (owner, type, key) -> 23505
    with isolated_postgres.client() as client:
        made = client.post("/intelligence/graph/entities", json={
            "entity_type": "project", "name": "Live Private Name", "provenance": USER.model_dump()})
        assert made.status_code == 200
        bad = client.post("/intelligence/graph/relationships", json={
            "subject_id": made.json()["entity_id"], "relation": "depends_on", "object_id": "KE-" + "0" * 16,
            "status": "verified", "confidence": 1.0, "provenance": USER.model_dump()})
        assert bad.status_code == 404
        activity = client.get("/activity", params={"category": "intelligence", "limit": 50}).json()
    assert activity["durable_storage"] is True
    events = {(item["event_type"], item["status"]) for item in activity["events"]}
    assert ("kg_entity_upsert", "succeeded") in events and ("kg_relationship_add", "failed") in events
    assert all(item["durable"] for item in activity["events"]) and "Live Private Name" not in str(activity)


_WORKER = r'''
import os, sys, time
from pathlib import Path
from uuid import UUID
from jarvis.knowledge_graph.models import Provenance, RelationshipInput
from jarvis.knowledge_graph.service import GraphError, GraphService
from jarvis.knowledge_graph.stores import PostgresGraphStore
go = Path(os.environ["JARVIS_IT_GO_FILE"])
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
service = GraphService(PostgresGraphStore(os.environ["JARVIS_IT_DATABASE_URL"], operation_timeout_seconds=15))
try:
    item = service.add_relationship(UUID(os.environ["JARVIS_IT_OWNER"]), RelationshipInput(
        subject_id=sys.argv[1], relation=sys.argv[2], object_id=sys.argv[3], status="verified", confidence=1.0,
        provenance=Provenance(source_kind="user_statement", source_ref="process", extractor="user")))
    print("OK", item.status)
except GraphError as exc:
    print("ERR", exc.status_code)
'''


def test_separate_os_processes_serialize_on_the_same_keys(isolated_postgres, tmp_path):
    """Two real OS processes, each with its own connection, race on one functional relation and one cycle."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    url = isolated_postgres.database_url
    setup = graph(url)
    task = entity(setup, "task", "proc-task")
    alpha, beta = entity(setup, "project", "proc-a"), entity(setup, "project", "proc-b")
    left, right = entity(setup, "software_component", "proc-x"), entity(setup, "software_component", "proc-y")
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    repo_root = str(Path(__file__).resolve().parents[1])

    def race(*pairs):
        go = tmp_path / f"go-{len(list(tmp_path.iterdir()))}"
        env = dict(os.environ, JARVIS_IT_DATABASE_URL=url, JARVIS_IT_OWNER=str(OWNER), JARVIS_IT_GO_FILE=str(go),
                   PYTHONPATH=repo_root)
        procs = [subprocess.Popen([sys.executable, str(worker), *pair], cwd=repo_root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for pair in pairs]
        go.write_text("go", encoding="utf-8")
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err[-500:]
            outputs.append(out.strip())
        return sorted(outputs)

    assert race((task.entity_id, "belongs_to", alpha.entity_id),
                (task.entity_id, "belongs_to", beta.entity_id)) == ["OK disputed", "OK verified"]
    stored = setup.store.find_relationships(OWNER, task.entity_id, "belongs_to")
    assert sorted(item.status for item in stored) == ["disputed", "disputed"]  # the first was linked by the second
    assert race((left.entity_id, "depends_on", right.entity_id),
                (right.entity_id, "depends_on", left.entity_id)) == ["ERR 422", "OK verified"]
