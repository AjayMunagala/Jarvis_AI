"""v6.2 PDF text extraction with pypdf, under strict limits."""

import base64
import builtins
import io
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.research.parsers import MAX_PAGES, DocumentParsingError, extract_document_text, parse_document_content

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v62-pdf-support-token-with-32-characters"


def text_pdf(pages: list[str]) -> bytes:
    """A small, valid PDF with one line of Helvetica text per page (built by hand, no extra library)."""
    objects: list[bytes] = []
    kids = []
    font_id = 3 + 2 * len(pages)
    for index, text in enumerate(pages):
        page_id, content_id = 3 + 2 * index, 4 + 2 * index
        kids.append(f"{page_id} 0 R")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >> endobj".encode())
        objects.append(f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode() + stream + b"\nendstream endobj")
    head = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
            f"2 0 obj << /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >> endobj".encode()]
    tail = [f"{font_id} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj".encode()]
    body = b"%PDF-1.4\n"
    offsets = []
    for item in head + objects + tail:
        offsets.append(len(body))
        body += item + b"\n"
    xref = len(body)
    body += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer << /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return body


def test_text_is_extracted_from_every_page_and_fenced():
    fenced, size, pages = parse_document_content("report.pdf", "application/pdf",
                                                 raw_bytes=text_pdf(["Quarterly revenue grew", "Costs were flat"]))
    assert "Quarterly revenue grew" in fenced and "Costs were flat" in fenced
    assert fenced.startswith("<<<UNTRUSTED_DOCUMENT_CONTENT") and pages == 2 and size > 0


def test_long_pdfs_are_cut_off_with_a_visible_note():
    text, _, pages = extract_document_text("long.pdf", "application/pdf",
                                           raw_bytes=text_pdf([f"Page {n}" for n in range(MAX_PAGES + 20)]))
    assert pages == MAX_PAGES and f"Only the first {MAX_PAGES} of {MAX_PAGES + 20} pages" in text
    assert f"Page {MAX_PAGES - 1}" in text and f"Page {MAX_PAGES + 5}" not in text


def test_encrypted_image_only_and_damaged_pdfs_are_refused_cleanly():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    blank = io.BytesIO()
    writer.write(blank)
    writer.encrypt("secret-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(DocumentParsingError, match="encrypted"):
        extract_document_text("locked.pdf", "application/pdf", raw_bytes=encrypted.getvalue())
    with pytest.raises(DocumentParsingError, match="no extractable text"):
        extract_document_text("scan.pdf", "application/pdf", raw_bytes=blank.getvalue())
    with pytest.raises(DocumentParsingError, match="could not be read"):
        extract_document_text("broken.pdf", "application/pdf", raw_bytes=b"%PDF-1.7 not really a pdf")


def test_without_the_library_pdfs_are_refused_with_how_to_enable_them(monkeypatch):
    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name.startswith("pypdf"):
            raise ImportError("pypdf is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    with pytest.raises(DocumentParsingError, match="documents' extra"):
        extract_document_text("report.pdf", "application/pdf", raw_bytes=text_pdf(["x"]))


def test_pdf_through_the_api_and_into_knowledge():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        parsed = client.post("/research/documents/parse", json={
            "filename": "manual.pdf", "content_type": "application/pdf", "index_in_knowledge": True,
            "data_base64": base64.b64encode(text_pdf(["Reset the router by holding the button"])).decode()})
        assert parsed.status_code == 201 and parsed.json()["page_count"] == 1
        hits = client.post("/knowledge/search", json={"query": "reset router button", "top_k": 3}).json()["results"]
        status = client.get("/status").json()
    assert hits and hits[0]["chunk"]["untrusted"] is True
    research = next(item for item in status["subsystems"] if item["key"] == "research")
    assert research["checks"] == ["PDF extraction available"]
