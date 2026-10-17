"""v5.2 knowledge graph: validation, provenance, conflicts, traversal, isolation."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from database import runner
from jarvis.knowledge_graph.models import EntityInput, KnowledgeRecord, Provenance, RelationshipInput
from jarvis.knowledge_graph.service import GraphError, GraphService
from jarvis.knowledge_graph.stores import MemoryGraphStore, PostgresGraphStore, StorageError

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER = Provenance(source_kind="user_statement", source_ref="chat:1", extractor="user")
MODEL = Provenance(source_kind="document", source_ref="doc:readme", extractor="model", excerpt="api uses db")


def graph(**kwargs):
    return GraphService(MemoryGraphStore(), **kwargs)


def entity(service, kind, name, owner=OWNER, provenance=USER, **extra):
    return service.upsert_entity(owner, EntityInput(entity_type=kind, name=name, provenance=provenance, **extra))


def relate(service, subject, relation, target, status="verified", confidence=None, provenance=None, owner=OWNER):
    confidence = confidence if confidence is not None else (1.0 if status == "verified" else 0.6)
    provenance = provenance or (USER if status == "verified" else MODEL)
    return service.add_relationship(owner, RelationshipInput(
        subject_id=subject.entity_id, relation=relation, object_id=target.entity_id, status=status,
        confidence=confidence, provenance=provenance))


# ------------------------------------------------------------- validation

def test_entities_are_deduplicated_by_normalized_key_and_keep_provenance():
    service = graph()
    first = entity(service, "service", "API Gateway", aliases=("gateway",))
    second = entity(service, "service", "  api   gateway ", provenance=MODEL, attributes={"port": 8173})
    assert first.entity_id == second.entity_id and second.key == "api gateway"
    assert [item.source_ref for item in second.provenance] == ["chat:1", "doc:readme"]
    assert second.attributes == {"port": 8173} and second.aliases == ("gateway",)
    assert all(item.recorded_at for item in second.provenance)
    assert entity(service, "project", "API Gateway").entity_id != first.entity_id  # type is part of identity


@pytest.mark.parametrize("change", [
    {"entity_type": "planet"}, {"name": "  "}, {"attributes": {"Bad Key": 1}}, {"attributes": {"x": "y" * 501}},
    {"aliases": tuple(str(i) for i in range(11))},
])
def test_entity_validation(change):
    values = dict(entity_type="service", name="db", provenance=USER)
    values.update(change)
    with pytest.raises(ValidationError):
        EntityInput(**values)


def test_relationship_contract_separates_verified_from_inferred():
    base = dict(subject_id="KE-" + "a" * 16, relation="depends_on", object_id="KE-" + "b" * 16)
    with pytest.raises(ValidationError):
        RelationshipInput(**base, status="verified", confidence=1.0, provenance=MODEL)  # model output is never verified
    with pytest.raises(ValidationError):
        RelationshipInput(**base, status="verified", confidence=0.9, provenance=USER)
    with pytest.raises(ValidationError):
        RelationshipInput(**base, status="inferred", confidence=1.0, provenance=MODEL)
    with pytest.raises(ValidationError):
        RelationshipInput(**{**base, "object_id": base["subject_id"]}, status="inferred", confidence=0.5, provenance=MODEL)


def test_relationship_endpoints_and_types_are_validated():
    service = graph()
    api, db = entity(service, "service", "api"), entity(service, "service", "db")
    doc, alice = entity(service, "document", "readme"), entity(service, "person", "alice")
    assert relate(service, api, "depends_on", db).status == "verified"
    with pytest.raises(GraphError) as raised:
        relate(service, alice, "depends_on", api)
    assert raised.value.status_code == 422
    with pytest.raises(GraphError):
        relate(service, api, "describes", doc)  # only documents describe
    with pytest.raises(GraphError):
        relate(service, doc, "supersedes", api)  # supersedes needs the same type
    assert relate(service, doc, "describes", api).relation == "describes"
    foreign = entity(service, "service", "other", owner=OTHER)
    with pytest.raises(GraphError) as raised:
        relate(service, api, "depends_on", foreign)
    assert raised.value.status_code == 404


def test_dependency_cycles_are_rejected():
    service = graph()
    a, b, c = (entity(service, "software_component", name) for name in "abc")
    relate(service, a, "depends_on", b)
    relate(service, b, "depends_on", c)
    with pytest.raises(GraphError) as raised:
        relate(service, c, "depends_on", a)
    assert "cycle" in raised.value.public_message


# --------------------------------------------------------------- conflicts

def test_inference_never_overrides_a_verified_fact():
    service = graph()
    task, alpha, beta = entity(service, "task", "migrate"), entity(service, "project", "alpha"), entity(service, "project", "beta")
    verified = relate(service, task, "belongs_to", alpha)
    inferred = relate(service, task, "belongs_to", beta, status="inferred", confidence=0.9)
    assert inferred.status == "disputed" and inferred.conflicts_with == (verified.relationship_id,)
    assert service.store.get_relationship(OWNER, verified.relationship_id).status == "verified"


def test_verified_evidence_supersedes_an_inference_and_preserves_it():
    service = graph()
    doc, alice, bob = entity(service, "document", "spec"), entity(service, "person", "alice"), entity(service, "person", "bob")
    guess = relate(service, doc, "created_by", alice, status="inferred", confidence=0.4)
    fact = relate(service, doc, "created_by", bob)
    stored = service.store.get_relationship(OWNER, guess.relationship_id)
    assert fact.status == "verified" and stored.status == "superseded" and stored.superseded_by == fact.relationship_id
    assert stored.provenance == guess.provenance  # history kept


def test_conflicting_verified_facts_are_disputed_until_the_owner_resolves():
    service = graph()
    task, alpha, beta = entity(service, "task", "t"), entity(service, "project", "alpha"), entity(service, "project", "beta")
    first = relate(service, task, "belongs_to", alpha)
    second = relate(service, task, "belongs_to", beta, provenance=Provenance(
        source_kind="memory", source_ref="memory:2", extractor="user"))
    assert second.status == "disputed"
    assert service.store.get_relationship(OWNER, first.relationship_id).status == "disputed"
    resolved = service.resolve_conflict(OWNER, second.relationship_id, note="Moved to beta in October")
    assert resolved.status == "verified" and resolved.provenance[-1].extractor == "user"
    loser = service.store.get_relationship(OWNER, first.relationship_id)
    assert loser.status == "superseded" and loser.superseded_by == second.relationship_id
    with pytest.raises(GraphError) as raised:
        service.resolve_conflict(OWNER, second.relationship_id)
    assert raised.value.status_code == 409


def test_non_functional_relations_allow_many_and_reassertion_merges():
    service = graph()
    api, db, cache = (entity(service, "service", name) for name in ("api", "db", "cache"))
    one = relate(service, api, "depends_on", db, status="inferred", confidence=0.5)
    relate(service, api, "depends_on", cache, status="inferred", confidence=0.5)
    again = relate(service, api, "depends_on", db, status="inferred", confidence=0.7, provenance=Provenance(
        source_kind="repository", source_ref="repo:docker-compose.yml", extractor="rule"))
    assert again.relationship_id == one.relationship_id and again.confidence == 0.7 and len(again.provenance) == 2
    upgraded = relate(service, api, "depends_on", db)
    assert upgraded.status == "verified" and upgraded.confidence == 1.0
    assert relate(service, api, "depends_on", db, status="inferred", confidence=0.2).status == "verified"  # never downgraded
    retracted = service.retract(OWNER, one.relationship_id)
    assert retracted.status == "retracted"


# --------------------------------------------------------------- traversal

def test_traversal_is_bounded_owner_scoped_and_labels_inferences():
    service = graph()
    hub = entity(service, "project", "hub")
    spokes = [entity(service, "task", f"task {i}") for i in range(12)]
    for spoke in spokes:
        relate(service, spoke, "belongs_to", hub, status="inferred", confidence=0.5)
    deep = entity(service, "person", "author")
    relate(service, spokes[0], "created_by", deep)
    view = service.neighbors(OWNER, hub.entity_id, depth=1, max_entities=5)
    assert view.truncated is True and len(view.entities) == 5 and "not verified" in view.note
    assert service.neighbors(OWNER, hub.entity_id, depth=1, include_inferred=False).relationships == ()
    full = service.neighbors(OWNER, hub.entity_id, depth=2, max_entities=100)
    assert deep.entity_id in {item.entity_id for item in full.entities}
    assert service.neighbors(OWNER, hub.entity_id, depth=99).depth == 3
    with pytest.raises(GraphError) as raised:
        service.neighbors(OTHER, hub.entity_id)
    assert raised.value.status_code == 404
    assert service.search(OTHER, text="task") == ()
    assert len(service.search(OWNER, text="task", entity_type="task", limit=3)) == 3


def test_retracted_and_superseded_edges_are_hidden_by_default():
    service = graph()
    a, b = entity(service, "service", "a"), entity(service, "service", "b")
    edge = relate(service, a, "depends_on", b)
    service.retract(OWNER, edge.relationship_id)
    assert service.neighbors(OWNER, a.entity_id).relationships == ()
    assert len(service.neighbors(OWNER, a.entity_id, include_inactive=True).relationships) == 1


def test_delete_entity_removes_its_edges_and_limits_apply():
    service = graph(max_entities=2)
    a, b = entity(service, "service", "a"), entity(service, "service", "b")
    relate(service, a, "depends_on", b)
    with pytest.raises(GraphError) as raised:
        entity(service, "service", "c")
    assert raised.value.status_code == 409
    assert service.delete_entity(OWNER, a.entity_id) == 2
    assert service.neighbors(OWNER, b.entity_id).relationships == ()
    with pytest.raises(GraphError):
        service.delete_entity(OTHER, b.entity_id)


# --------------------------------------------------------------- ingestion

def test_ingest_normalized_record_with_record_provenance():
    service = graph()
    record = KnowledgeRecord(source_kind="document", source_ref="doc:architecture.md", extractor="model", entities=(
        {"ref": "api", "entity_type": "service", "name": "API"},
        {"ref": "db", "entity_type": "service", "name": "PostgreSQL"},
        {"ref": "doc", "entity_type": "document", "name": "Architecture"},
    ), relationships=(
        {"subject_ref": "api", "relation": "depends_on", "object_ref": "db", "status": "inferred", "confidence": 0.8},
        {"subject_ref": "doc", "relation": "describes", "object_ref": "api", "status": "inferred", "confidence": 0.9},
    ))
    result = service.ingest(OWNER, record)
    assert set(result.entity_ids) == {"api", "db", "doc"}
    assert all(item.status == "inferred" and item.provenance[0].source_ref == "doc:architecture.md"
               for item in result.relationships)
    bad = record.model_copy(update={"relationships": (
        {"subject_ref": "db", "relation": "describes", "object_ref": "api", "status": "inferred", "confidence": 0.5},)})
    before = service.store.count(OWNER)
    with pytest.raises(GraphError):
        service.ingest(OWNER, KnowledgeRecord.model_validate(bad.model_dump()))
    assert service.store.count(OWNER) == before  # rejected whole, nothing written
    with pytest.raises(ValidationError):
        KnowledgeRecord(source_kind="document", source_ref="x", extractor="model", entities=(),
                        relationships=({"subject_ref": "a", "relation": "related_to", "object_ref": "b",
                                        "status": "inferred", "confidence": 0.5},))
    with pytest.raises(GraphError):
        service.ingest(OWNER, record.model_copy(update={"relationships": (
            record.relationships[0].model_copy(update={"status": "verified", "confidence": 1.0}),)}))


# ---------------------------------------------------------------- postgres

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.log.append((" ".join(query.split()), params))
        return FakeCursor([] if "set_config" in query else self.rows)


def fake_connect(log, rows=(), fail=False):
    def connect(url, connect_timeout):
        if fail:
            raise OSError("postgresql://user:secret@host failed")
        return FakeConnection(log, list(rows))
    return connect


def statements(log):
    return [entry for entry in log if "set_config" not in entry[0]]


def test_postgres_store_is_parameterized_and_owner_scoped():
    log = []
    store = PostgresGraphStore("postgresql://x", connect=fake_connect(log))
    assert store.get_entity(OWNER, "KE-'; DROP TABLE x; --") is None
    store.search_entities(OWNER, text="50%_off", entity_type="task", limit=5)
    store.edges(OWNER, "KE-" + "a" * 16, limit=10)
    store.delete_entity(OWNER, "KE-" + "a" * 16)
    for query, params in statements(log):
        assert "owner_id = %s" in query and OWNER in params and "DROP" not in query
    search = statements(log)[1]
    assert search[1][2] == "%50\\%\\_off%" and search[1][-1] == 5


def test_postgres_upserts_guard_owner_and_failures_are_sanitized():
    log = []
    memory = graph()
    created = entity(memory, "service", "api")
    edge = relate(memory, created, "depends_on", entity(memory, "service", "db"))
    store = PostgresGraphStore("postgresql://x", connect=fake_connect(log))
    store.save_entity(created)
    store.save_relationship(edge)
    inserts = [entry for entry in statements(log) if entry[0].startswith("INSERT")]
    assert "WHERE jarvis_app.kg_entities.owner_id = EXCLUDED.owner_id" in inserts[0][0]
    assert "WHERE jarvis_app.kg_relationships.owner_id = EXCLUDED.owner_id" in inserts[1][0]
    assert inserts[0][1][:2] == (created.entity_id, OWNER) and inserts[1][1][1] == OWNER
    assert store.durable is True
    failing = GraphService(PostgresGraphStore("postgresql://x", connect=fake_connect([], fail=True)))
    with pytest.raises(GraphError) as raised:
        failing.search(OWNER, text="x")
    assert raised.value.status_code == 503 and "secret" not in raised.value.public_message
    with pytest.raises(StorageError):
        PostgresGraphStore("postgresql://x", connect=fake_connect([], fail=True)).count(OWNER)


def test_migration_0052_is_discovered_and_enforces_isolation_in_sql():
    found = {migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR)}
    assert "0052_knowledge_graph" in found
    sql = (runner.MIGRATIONS_DIR / "0052_knowledge_graph.sql").read_text(encoding="utf-8")
    assert "FOREIGN KEY (owner_id, subject_id)" in sql and "FOREIGN KEY (owner_id, object_id)" in sql
    assert "status <> 'verified' OR confidence = 1" in sql
