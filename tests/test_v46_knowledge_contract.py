"""v4.6 one knowledge and context contract: stable embeddings, shared budgeting, research indexing, DOCX parsing."""

import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.knowledge.chunking import EMBEDDING_MODEL, compute_deterministic_embedding, estimate_tokens
from jarvis.knowledge.models import ContextBudgetRequest, KnowledgeDocumentCreateRequest
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.stores import MemoryKnowledgeStore
from jarvis.main import create_app
from jarvis.research.parsers import DocumentParsingError, DocumentTooLargeError, parse_document_content
from jarvis.routing.requirements import estimate_tokens as shared_estimate

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v46-knowledge-contract-token-with-32-chars"


def docx_bytes(paragraphs, *, xml_prefix=b"", oversized=False):
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (xml_prefix + b'<?xml version="1.0"?>' if not xml_prefix else xml_prefix) + (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>").encode()
    if oversized:
        xml += b" " * (21 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


# ------------------------------------------------------------- embeddings and tokens

def test_embeddings_are_identical_across_processes():
    script = ("import json; from jarvis.knowledge.chunking import compute_deterministic_embedding as e; "
              "print(json.dumps(e('restart the ingestion worker after rotating credentials')))")
    vectors = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        vectors.append(json.loads(out.stdout))
    assert vectors[0] == vectors[1] == compute_deterministic_embedding(
        "restart the ingestion worker after rotating credentials")
    assert EMBEDDING_MODEL == "stable-hash-64-v2"


def test_one_token_estimator_is_shared():
    assert estimate_tokens is shared_estimate


# ------------------------------------------------------------- knowledge service

def make_knowledge():
    service = KnowledgeService(MemoryKnowledgeStore())
    return service


def test_search_audit_never_stores_the_query_text():
    recorded = []

    class Audit:
        def record(self, **kwargs):
            recorded.append(kwargs)

    service = KnowledgeService(MemoryKnowledgeStore(), audit_service=Audit())
    service.search(OWNER, __import__("jarvis.knowledge.models", fromlist=["KnowledgeSearchRequest"])
                   .KnowledgeSearchRequest(query="my secret diagnosis", top_k=3))
    event = next(item for item in recorded if item["event_type"] == "knowledge.search")
    assert "secret" not in json.dumps(event, default=str) and event["details"]["query_chars"] == 19


def test_context_budget_uses_the_shared_packer_and_respects_the_budget():
    service = make_knowledge()
    long_doc = service.ingest_document(OWNER, KnowledgeDocumentCreateRequest(
        title="Runbook", content=" ".join(f"Step {n}: rotate credentials and restart the worker." for n in range(200))))
    other = service.ingest_document(OWNER, KnowledgeDocumentCreateRequest(
        title="Recipes", content="Bake the bread at two hundred degrees for forty minutes."))
    allocation = service.allocate_context_budget(OWNER, ContextBudgetRequest(
        prompt="How do I bake bread?", max_context_tokens=500,
        document_ids=[long_doc.document.id, other.document.id]))
    assert allocation.sections["knowledge_context"] <= 500 - 300 - estimate_tokens("How do I bake bread?")
    assert allocation.used_tokens <= allocation.max_tokens
    assert allocation.omitted_count > 0  # the budget could not hold every chunk
    assert all(chunk.token_count == estimate_tokens(chunk.content) for chunk in allocation.selected_chunks)
    # explicitly chosen documents count even without a word in common with the prompt
    assert any(chunk.document_id == long_doc.document.id for chunk in allocation.selected_chunks)


def test_context_budget_from_search_results():
    service = make_knowledge()
    service.ingest_document(OWNER, KnowledgeDocumentCreateRequest(
        title="Runbook", content="Restart the ingestion worker after rotating credentials."))
    allocation = service.allocate_context_budget(OWNER, ContextBudgetRequest(
        prompt="restart the ingestion worker", max_context_tokens=4000))
    assert len(allocation.selected_chunks) == 1 and allocation.truncated_count == 0


# ------------------------------------------------------------- parsing

def test_docx_text_is_extracted_and_fenced():
    text, size, pages = parse_document_content("plan.docx", "application/octet-stream",
                                               raw_bytes=docx_bytes(["First paragraph.", "Second one."]))
    assert "First paragraph.\nSecond one." in text and text.startswith("<<<UNTRUSTED_DOCUMENT_CONTENT")


@pytest.mark.parametrize("raw, filename, error, message", [
    (b"%PDF-1.7 ...", "report.pdf", DocumentParsingError, "PDF parsing is not available"),
    (b"%PDF-1.7 ...", "report.txt", DocumentParsingError, "PDF parsing is not available"),
    (b"\x00\x01\x02binary", "blob.bin", DocumentParsingError, "binary file type is not supported"),
    (b"PK\x03\x04not-a-real-zip", "archive.zip", DocumentParsingError, "binary file type is not supported"),
    (b"PK\x03\x04broken", "broken.docx", DocumentParsingError, "not a valid DOCX"),
])
def test_unsupported_or_broken_files_are_refused(raw, filename, error, message):
    with pytest.raises(error, match=message):
        parse_document_content(filename, "application/octet-stream", raw_bytes=raw)


def test_docx_entities_and_oversized_xml_are_refused():
    with pytest.raises(DocumentParsingError, match="entities"):
        parse_document_content("x.docx", "", raw_bytes=docx_bytes(
            ["a"], xml_prefix=b'<?xml version="1.0"?><!DOCTYPE w [<!ENTITY e "boom">]>'))
    with pytest.raises(DocumentTooLargeError):
        parse_document_content("x.docx", "", raw_bytes=docx_bytes(["a"], oversized=True))


# ------------------------------------------------------------- research -> knowledge and graph (API)

def make_client():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    return app, TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def test_research_documents_can_be_indexed_in_knowledge_and_the_graph():
    app, client = make_client()
    with client:
        parsed = client.post("/research/documents/parse", json={
            "filename": "rotation.docx", "content_type": "application/octet-stream", "index_in_knowledge": True,
            "data_base64": base64.b64encode(docx_bytes(["Rotate the signing keys every ninety days."])).decode()})
        assert parsed.status_code == 201
        metadata = parsed.json()["metadata"]
        assert metadata["knowledge_index"] == "indexed" and metadata["graph"] == "recorded"
        hits = client.post("/knowledge/search", json={"query": "signing keys", "top_k": 3}).json()["results"]
        assert hits and "UNTRUSTED_DOCUMENT_CONTENT" in hits[0]["chunk"]["content"]  # still fenced as untrusted
        plain = client.post("/research/documents/parse", json={"filename": "n.txt", "text_content": "plain"}).json()
        assert "knowledge_index" not in plain["metadata"]  # indexing is opt-in
        assert client.delete(f"/research/documents/{parsed.json()['id']}").status_code == 204
        knowledge_id = metadata["knowledge_document_id"]
        assert client.get(f"/knowledge/documents/{knowledge_id}").status_code == 404  # removed with it


def test_research_refusals_are_422_not_500():
    app, client = make_client()
    with client:
        bad_base64 = client.post("/research/documents/parse", json={"filename": "a.txt", "data_base64": "@@@"})
        pdf = client.post("/research/documents/parse", json={
            "filename": "a.pdf", "data_base64": base64.b64encode(b"%PDF-1.4 x").decode()})
    assert bad_base64.status_code == 422 and pdf.status_code == 422
    assert "approved as a dependency" in pdf.json()["detail"]
