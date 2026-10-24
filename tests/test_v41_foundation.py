"""v4.1 foundation and safety repair: regression tests.

1. Migrations 0008-0013 changed only by removing the foreign-key clauses to a
   table that never existed (proven byte-for-byte against the originals).
2. Quarantine is confined to the repository root.
3. Self-improvement can change only allowlisted targets.
4. Knowledge chunk reads are owner-scoped.
5. Workflow steps never report a placeholder success; knowledge.search works.
6. Research citations do not claim unfetched URLs were verified.
"""

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from database import runner
from jarvis.core.config import Settings
from jarvis.health.quarantine import QuarantineManager
from jarvis.improvement.immutable import ImmutableSecurityFileError, resolve_improvement_target
from jarvis.improvement.models import ProposalCreateRequest, ProposalDecisionRequest
from jarvis.improvement.service import ImprovementService
from jarvis.improvement.stores import MemoryImprovementStore
from jarvis.knowledge.models import ContextBudgetRequest, KnowledgeDocumentCreateRequest
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.stores import MemoryKnowledgeStore
from jarvis.main import create_app
from jarvis.repo.path_security import PathSecurityError
from jarvis.workflow.models import DAGSpec, WorkflowDefinitionCreateRequest
from jarvis.workflow.service import WorkflowService
from jarvis.workflow.stores import MemoryWorkflowStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOKEN = "v41-foundation-token-with-at-least-32-characters"
FIXTURES = Path(__file__).parent / "fixtures" / "migrations_before_v41"
CLAUSES = (" REFERENCES jarvis_app.users(id) ON DELETE CASCADE", " REFERENCES jarvis_app.users(id)")


def normalized(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


# ------------------------------------------------------------- migrations

@pytest.mark.parametrize("name", sorted(path.name for path in FIXTURES.glob("*.sql")))
def test_migration_repair_removed_only_the_users_foreign_keys(name):
    original = normalized(FIXTURES / name)
    expected = original
    for clause in CLAUSES:
        expected = expected.replace(clause, "")
    assert original != expected  # each file really contained the broken reference
    assert normalized(runner.MIGRATIONS_DIR / name) == expected


def test_six_files_were_repaired_and_no_migration_references_a_users_table():
    assert sorted(path.name[:4] for path in FIXTURES.glob("*.sql")) == ["0008", "0009", "0010", "0011", "0012", "0013"]
    for path in runner.MIGRATIONS_DIR.glob("*.sql"):
        assert "jarvis_app.users" not in normalized(path), path.name


UNCHANGED = {  # checksums from the v3-to-v5 candidate (3a35dda); these files must not change in v4.1
    "0001_migration_metadata": "075c0d965fed19f3", "0002_conversation_persistence": "8e65869fd11e7b08",
    "0003_long_term_memories": "a37b45e058fc6549", "0004_memory_ownership": "ae3dcc70b033d436",
    "0005_memory_source_kind": "a1e4663fa44a34af", "0006_audit_and_approvals": "468bc7e8d0f62d12",
    "0007_personal_assistant": "f205bfb54cd5740a", "0052_knowledge_graph": "93504325f6ca6496",
    "0056_personalization": "9d696bcfd0dc98a3", "0057_memory_update_proposals": "c71f0618b2d8e0ef",
    "0058_audit_intelligence_category": "7476a061a0a471c0", "0059_audit_orchestration_category": "ea841221bc49e417",
}


def test_every_other_migration_is_byte_identical():
    found = {migration_id: checksum for migration_id, _, _, checksum in runner._migration_files(runner.MIGRATIONS_DIR)}
    for migration_id, prefix in UNCHANGED.items():
        assert found[migration_id].startswith(prefix), migration_id
    # Later versions may only add new migrations numbered 0060 or above.
    added = sorted(set(found) - set(UNCHANGED) - {path.stem for path in FIXTURES.glob("*.sql")})
    assert all(migration_id >= "0060" for migration_id in added), added


# -------------------------------------------------------------- quarantine

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "src" / "suspect.py").write_text("import socket", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must never move", encoding="utf-8")
    return root, outside


def test_quarantine_is_confined_to_the_root(repo):
    root, outside = repo
    manager = QuarantineManager(root / ".quarantine", allowed_root=root)
    for bad in (outside, Path("../outside.txt"), Path(".git/config"), Path("src"), Path("C:/Windows/win.ini"),
                Path("src/suspect.py:stream")):
        with pytest.raises((PathSecurityError, ValueError)):
            manager.quarantine_file(OWNER, bad, reason="x")
    assert outside.read_text(encoding="utf-8") == "must never move"
    with pytest.raises(FileNotFoundError):
        manager.quarantine_file(OWNER, Path("src/missing.py"), reason="x")
    record = manager.quarantine_file(OWNER, Path("src/suspect.py"), reason="beaconing")
    with pytest.raises(PathSecurityError):
        manager.quarantine_file(OWNER, Path(record.quarantine_path), reason="again")
    (root / "src" / "suspect.py").write_text("new file at the same place", encoding="utf-8")
    with pytest.raises(ValueError):
        manager.restore_file(record)  # never overwrites an existing file
    assert (root / "src" / "suspect.py").read_text(encoding="utf-8") == "new file at the same place"
    os.remove(root / "src" / "suspect.py")
    assert manager.restore_file(record).restored_at is not None


def test_quarantine_refuses_symlink_components(repo):
    root, outside = repo
    link = root / "linked"
    try:
        link.symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        try:  # Windows without symlink privilege: a directory junction is also a reparse point
            import _winapi

            _winapi.CreateJunction(str(outside.parent), str(link))
        except (ImportError, OSError):
            pytest.skip("Neither symbolic links nor junctions can be created in this environment.")
    manager = QuarantineManager(root / ".quarantine", allowed_root=root)
    with pytest.raises(PathSecurityError):
        manager.quarantine_file(OWNER, Path("linked/outside.txt"), reason="x")
    assert outside.exists()


def make_client(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    app = create_app(settings=Settings(**values))
    return app, TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def test_quarantine_api_refuses_paths_outside_the_repository_without_leaking_server_paths(tmp_path):
    outside = tmp_path / "victim.txt"
    outside.write_text("data", encoding="utf-8")
    app, client = make_client()
    with client:
        response = client.post("/health/security/quarantine", json={"file_path": str(outside), "reason": "test"})
        assert response.status_code == 422 and outside.exists()
        missing = client.post("/health/security/quarantine", json={"file_path": "no/such/file.py", "reason": "x"})
        assert missing.status_code == 404
        repo_root = str(app.state.repo_service.repo_root)
        assert repo_root not in response.text and repo_root not in missing.text


# ---------------------------------------------------------- improvement

@pytest.mark.parametrize("target", [
    "jarvis/main.py", "jarvis/api/dependencies.py", "jarvis/improvement/immutable.py", ".git/config", "tests/test_x.py",
    "C:/Windows/win.ini", "/etc/passwd", "prompts/../jarvis/main.py", "prompts/evil.py", "prompts", "prompts/x.md:ads",
    "jarvis/security/policy.py",
])
def test_improvement_refuses_non_allowlisted_targets(tmp_path, target):
    with pytest.raises(ImmutableSecurityFileError):
        resolve_improvement_target(tmp_path, target, {"prompts/assistant_persona.md": ""})


def test_improvement_writes_only_existing_allowlisted_prompt_files(tmp_path):
    (tmp_path / "prompts").mkdir()
    prompt = tmp_path / "prompts" / "greeting.md"
    prompt.write_text("Hello.", encoding="utf-8")
    victim = tmp_path / "app.py"
    victim.write_text("print('original')", encoding="utf-8")
    service = ImprovementService(MemoryImprovementStore(), repo_root=tmp_path)
    proposal = service.propose_mutation(OWNER, ProposalCreateRequest(
        target_name="prompts/greeting.md", proposed_content="Hello, owner.", rationale="Friendlier greeting"))
    service.decide_proposal(OWNER, proposal.id, ProposalDecisionRequest(decision="approved", comment="ok"))
    assert prompt.read_text(encoding="utf-8") == "Hello, owner."
    with pytest.raises(ImmutableSecurityFileError):
        service.propose_mutation(OWNER, ProposalCreateRequest(
            target_name=str(victim), proposed_content="pwned", rationale="absolute path escape"))
    assert victim.read_text(encoding="utf-8") == "print('original')"
    service.set_target_content("prompts/new.md", "not created")
    assert not (tmp_path / "prompts" / "new.md").exists()


def test_improvement_api_refuses_overwriting_application_code(tmp_path):
    app, client = make_client()
    main_py = Path(app.state.improvement_service.repo_root) / "jarvis" / "main.py"
    before = hashlib.sha256(main_py.read_bytes()).hexdigest()
    with client:
        for target in ("jarvis/main.py", str(main_py)):
            response = client.post("/improvement/proposals", json={
                "target_name": target, "proposed_content": "x = 1", "rationale": "should be refused"})
            assert response.status_code == 422, response.text
    assert hashlib.sha256(main_py.read_bytes()).hexdigest() == before


# ------------------------------------------------------------- knowledge

def test_knowledge_chunks_are_owner_scoped():
    service = KnowledgeService(MemoryKnowledgeStore())
    doc = service.ingest_document(OWNER, KnowledgeDocumentCreateRequest(
        title="Private plan", content="Owner A private launch plan for the satellite project."))
    doc_id = doc.document.id
    assert service.store.get_document_chunks(OTHER, doc_id) == []
    assert service.get_document(OTHER, doc_id) is None
    leak = service.allocate_context_budget(OTHER, ContextBudgetRequest(
        prompt="satellite launch plan", max_context_tokens=2000, document_ids=[doc_id]))
    assert leak.selected_chunks == []
    own = service.allocate_context_budget(OWNER, ContextBudgetRequest(
        prompt="satellite launch plan", max_context_tokens=2000, document_ids=[doc_id]))
    assert own.selected_chunks


# --------------------------------------------------------------- workflow

def run_single_step(service, step):
    definition = service.create_definition(OWNER, WorkflowDefinitionCreateRequest(
        name="v41", dag_spec=DAGSpec(steps=[{"step_id": "s1", "name": "S1", **step}])))
    return service.trigger_run(OWNER, definition.id, {})


@pytest.mark.parametrize("step, reason", [
    ({"step_type": "tool", "action": "shell.exec"}, "No handler is registered"),
    ({"step_type": "tool", "action": "finance.accounts.create"}, "finance service is not configured"),
    ({"step_type": "tool", "action": "knowledge.search"}, "knowledge service is not configured"),
    ({"step_type": "assistant", "action": "teleport.now"}, "not configured"),
])
def test_unavailable_steps_fail_honestly_without_retries(step, reason):
    service = WorkflowService(MemoryWorkflowStore())
    detail = run_single_step(service, step)
    assert detail.run.status != "completed"
    failed = detail.steps[0]
    assert failed.status == "failed" and reason in failed.error_message
    assert failed.recovery_metadata["attempt_count"] == 1  # permanent: no blocking retries


def test_workflow_knowledge_search_step_calls_the_real_service():
    knowledge = KnowledgeService(MemoryKnowledgeStore())
    knowledge.ingest_document(OWNER, KnowledgeDocumentCreateRequest(
        title="Runbook", content="Restart the ingestion worker after rotating credentials."))
    service = WorkflowService(MemoryWorkflowStore(), knowledge_service=knowledge)
    detail = run_single_step(service, {"step_type": "tool", "action": "knowledge.search",
                                       "parameters": {"query": "ingestion worker", "top_k": 3}})
    assert detail.steps[0].status == "completed" and detail.steps[0].outputs["results_count"] >= 1


# --------------------------------------------------------------- research

def test_research_citations_do_not_claim_unfetched_urls_were_verified():
    app, client = make_client()
    with client:
        response = client.post("/research/synthesize", json={"topic": "spec", "web_urls": ["https://example.com/spec"]})
        assert response.status_code == 201, response.text
        quote = response.json()["citations"][0]["quote"]
    assert quote.startswith("Not fetched") and "verified by" not in quote
