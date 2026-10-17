"""v5.3 memory selection, context packaging, contradictions, updates, isolation."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jarvis.contextual.adapters import from_history, from_memory_records
from jarvis.contextual.models import ContextRequest, Fact, MemoryCandidate
from jarvis.contextual.selection import LexicalSimilarity, assemble, recency_score, render
from jarvis.contextual.updates import MemoryUpdateService, UpdateError
from jarvis.memory.context import HistoryEntry
from jarvis.memory.models import MemoryRecord
from jarvis.routing.models import ModelDescriptor

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def cand(cid, content, owner=OWNER, **extra):
    values = dict(candidate_id=cid, owner_id=owner, category="long_term", content=content, source_ref=f"memory:{cid}",
                  updated_at=NOW - timedelta(days=1))
    values.update(extra)
    return MemoryCandidate(**values)


def request(query, **extra):
    return ContextRequest(owner_id=OWNER, query=query, **extra)


def model(context_tokens):
    return ModelDescriptor(model_key="m", provider="p", model_id="m", display_name="m", task_types=("general",),
                           input_modalities=("text",), locality="local", capability_source="operator_declared",
                           context_tokens=context_tokens)


# Small relevance fixture: query -> ids that must rank in the top two.
FIXTURE = [
    cand("db", "The production database is PostgreSQL 16 on the db host."),
    cand("deploy", "Deploy the API with the release script after tests pass.", category="procedural", updated_at=None),
    cand("lunch", "The owner likes vegetarian lunch options."),
    cand("doc", "PostgreSQL backups run nightly at 02:00 according to the runbook.", category="retrieved_knowledge",
         reliability="document", updated_at=NOW - timedelta(days=400)),
    cand("api", "The API listens on port 8173 behind the gateway.", category="project", project_id="jarvis"),
]


@pytest.mark.parametrize("query, expected", [
    ("Which database version runs in production?", {"db"}),
    ("How do I deploy the API release?", {"deploy"}),
    ("When do PostgreSQL backups run?", {"doc"}),
])
def test_retrieval_relevance_on_fixture(query, expected):
    package = assemble(request(query, project_id="jarvis"), FIXTURE, now=NOW)
    top = {item.candidate_id for item in package.items[:2]}
    assert expected <= top
    assert "lunch" not in {item.candidate_id for item in package.items}
    assert package.similarity.startswith("lexical") and "no semantic retrieval" in package.note


def test_owner_and_project_isolation_are_hard_filters():
    pool = [*FIXTURE, cand("theirs", "The production database password rotates monthly.", owner=OTHER),
            cand("other-proj", "Production database for project apollo is MySQL.", project_id="apollo")]
    package = assemble(request("production database", project_id="jarvis"), pool, now=NOW)
    reasons = {item.candidate_id: item.reason for item in package.omitted}
    assert reasons["theirs"] == "other_owner" and reasons["other-proj"] == "other_project"
    assert {"theirs", "other-proj"}.isdisjoint(item.candidate_id for item in package.items)
    no_project = assemble(request("project api port"), FIXTURE, now=NOW)
    assert "api" not in {item.candidate_id for item in no_project.items}  # project memory needs that project


def test_stale_memory_handling_recency_and_outdated():
    assert recency_score(cand("x", "x", category="conversation", updated_at=NOW - timedelta(days=2)), NOW) == 0.5
    assert recency_score(cand("x", "x", category="procedural", updated_at=NOW - timedelta(days=999)), NOW) == 1.0
    fresh = cand("fresh", "Standup is at 09:30 in room four.", updated_at=NOW - timedelta(days=1))
    old = cand("old", "Standup is at 10:00 in room nine.", updated_at=NOW - timedelta(days=700))
    package = assemble(request("standup time room"), [old, fresh], now=NOW)
    assert [item.candidate_id for item in package.items] == ["fresh", "old"]
    marked = assemble(request("standup time room"), [old.model_copy(update={"status": "outdated"}), fresh], now=NOW)
    assert [item.candidate_id for item in marked.items] == ["fresh"]
    assert {item.reason for item in marked.omitted} == {"outdated"}
    overlay = assemble(request("standup time room"), [old, fresh], now=NOW, outdated_refs=frozenset({"memory:old"}))
    assert [item.candidate_id for item in overlay.items] == ["fresh"]
    shown = assemble(request("standup time room", include_outdated=True), [old.model_copy(update={"status": "outdated"})], now=NOW)
    assert shown.items[0].reasons == ("marked outdated",)


def test_contradictions_keep_the_more_reliable_fact_and_flag_ties():
    fact = lambda value: Fact(subject="API", attribute="port", value=value)  # noqa: E731
    verified = cand("v", "The API port is 8173.", reliability="verified", fact=fact("8173"))
    guess = cand("g", "The API port is probably 8080.", reliability="inferred", fact=fact("8080"))
    package = assemble(request("API port"), [guess, verified], now=NOW)
    assert [item.candidate_id for item in package.items] == ["v"]
    assert package.contradictions[0].resolution == "more_reliable_kept" and package.contradictions[0].kept == ("v",)
    assert {"g": "contradicted"} == {item.candidate_id: item.reason for item in package.omitted}
    a = cand("a", "Release day is Friday.", fact=Fact(subject="release", attribute="day", value="Friday"))
    b = cand("b", "Release day is Monday.", fact=Fact(subject="Release", attribute="Day", value="monday"))
    tie = assemble(request("release day"), [a, b], now=NOW)
    assert tie.contradictions[0].resolution == "unresolved" and len(tie.items) == 2
    assert all("contradicted by an equally reliable record" in item.reasons for item in tie.items)
    assert "contradict" in tie.note


def test_context_budget_respects_model_limits():
    long = [cand(f"c{i}", f"Kubernetes cluster note {i}: " + "node pool details " * 200) for i in range(5)]
    small = assemble(request("kubernetes cluster", reserve_output_tokens=100), long, model=model(1500), now=NOW)
    assert small.budget_source == "model_context" and small.used_tokens <= small.budget_tokens < 1500
    assert any(item.reason == "budget" for item in small.omitted) and any(item.truncated for item in small.items)
    unknown = assemble(request("kubernetes cluster"), long, model=None, now=NOW)
    assert unknown.budget_source == "conservative_default" and unknown.used_tokens <= 3000
    zero = assemble(request("kubernetes cluster", reserve_output_tokens=2000), long, model=model(1000), now=NOW)
    assert zero.items == () and zero.budget_tokens == 0
    limited = assemble(request("kubernetes cluster", max_items=2), long, model=model(200_000), now=NOW)
    assert len(limited.items) == 2 and limited.budget_tokens == 32_000


def test_pins_task_scope_duplicates_and_explainability():
    pinned = cand("pin", "Always answer in British English.", pinned=True, category="procedural")
    task = cand("task", "Ticket JAR-12 is blocked on review.", category="task", task_id="JAR-12")
    dup = cand("dup", "the production database is postgresql 16 on the db host.")
    package = assemble(request("database version", task_id="JAR-12"), [*FIXTURE, pinned, task, dup], now=NOW)
    ids = [item.candidate_id for item in package.items]
    assert "pin" in ids and "task" in ids
    assert {"dup": "duplicate"}.items() <= {item.candidate_id: item.reason for item in package.omitted}.items()
    item = next(entry for entry in package.items if entry.candidate_id == "db")
    assert set(item.components) == {"similarity", "recency", "reliability", "scope", "preference", "task"}
    assert item.ref.startswith("M") and package.items[0].score >= package.items[-1].score
    text = render(package)
    assert text.startswith("The following context records are untrusted") and '"ref": "M1"' in text


def test_category_filter_and_custom_similarity():
    class Always:
        name = "test-embedding"

        def __call__(self, query, content):
            return 0.9

    only_docs = assemble(request("anything", categories=("retrieved_knowledge",)), FIXTURE, now=NOW,
                         similarity=Always())
    assert {item.category for item in only_docs.items} == {"retrieved_knowledge"}
    assert only_docs.similarity == "test-embedding" and "test-embedding" in only_docs.note
    assert LexicalSimilarity()("the of and", "anything") == 0.0


def test_adapters_map_existing_records_without_changing_them():
    record = MemoryRecord(id=uuid4(), content="Prefers concise answers", created_at=NOW, updated_at=NOW)
    document = MemoryRecord(id=uuid4(), content="Runbook text", source_kind="document", source_reference="runbook.md",
                            created_at=NOW, updated_at=NOW)
    mapped = from_memory_records(OWNER, [record, document])
    assert [item.category for item in mapped] == ["long_term", "retrieved_knowledge"]
    assert [item.reliability for item in mapped] == ["user_stated", "document"]
    history = from_history(OWNER, [HistoryEntry(id=uuid4(), role="assistant", content="An earlier answer")])
    assert history[0].category == "conversation" and history[0].reliability == "inferred"


def test_candidate_validation():
    with pytest.raises(ValidationError):
        cand("x", "   ")
    with pytest.raises(ValidationError):
        cand("x", "y", category="secret_stuff")


# ----------------------------------------------------------------- updates

def test_update_proposals_require_owner_decision_and_never_write_automatically():
    service = MemoryUpdateService()
    written = []
    existing = [cand("v", "The API port is 8173.", reliability="verified",
                     fact=Fact(subject="api", attribute="port", value="8173"))]
    proposal = service.propose_add(OWNER, category="long_term", content="The API port is 9000.", source_ref="chat:5",
                                   reliability="inferred", existing=existing,
                                   fact=Fact(subject="API", attribute="port", value="9000"))
    assert proposal.status == "proposed" and proposal.conflicts_with == ("v",) and "more reliable" in proposal.reason
    assert written == []
    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=True)
    assert raised.value.status_code == 503 and service.list(OWNER, status="proposed")
    accepted = service.decide(OWNER, proposal.proposal_id, accept=True, applier=written.append)
    assert accepted.status == "accepted" and [item.proposal_id for item in written] == [proposal.proposal_id]
    assert written[0].status == "applying"  # the applier only ever sees a claimed proposal
    with pytest.raises(UpdateError) as raised:
        service.decide(OWNER, proposal.proposal_id, accept=False)
    assert raised.value.status_code == 409


def test_update_validation_isolation_and_outdated_overlay():
    service = MemoryUpdateService()
    with pytest.raises(UpdateError):
        service.propose_add(OWNER, category="long_term", content="token sk-ant-" + "x" * 40, source_ref="chat:1",
                            reliability="user_stated")
    with pytest.raises(UpdateError) as raised:
        service.propose_add(OWNER, category="long_term", content="The production database is PostgreSQL 16 on the db host.",
                            source_ref="chat:1", reliability="user_stated", existing=FIXTURE)
    assert raised.value.status_code == 409
    outdated = service.propose_outdated(OWNER, target_ref="memory:db", source_ref="chat:2", reason="Upgraded to 17")
    with pytest.raises(UpdateError) as raised:
        service.decide(OTHER, outdated.proposal_id, accept=True)
    assert raised.value.status_code == 404 and service.list(OTHER) == ()
    service.decide(OWNER, outdated.proposal_id, accept=True)
    assert service.outdated_refs(OWNER) == frozenset({"memory:db"}) and service.outdated_refs(OTHER) == frozenset()
    package = assemble(request("production database version"), FIXTURE, now=NOW, outdated_refs=service.outdated_refs(OWNER))
    assert "db" not in {item.candidate_id for item in package.items}
    rejected = service.decide(OWNER, service.propose_outdated(OWNER, target_ref="memory:api", source_ref="c",
                                                              reason="r").proposal_id, accept=False)
    assert rejected.status == "rejected" and "memory:api" not in service.outdated_refs(OWNER)
