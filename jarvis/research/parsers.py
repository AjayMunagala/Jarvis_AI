"""Document parsing engine with structural injection fencing and resource bounds (v3.4).

v4.6: DOCX files are parsed with the standard library (zip + XML) under strict
limits: at most 20 MB of uncompressed XML, and no DTDs or entity
declarations. PDF and other binary files are refused with a clear message
instead of being decoded as text and stored as garbage. PDF support needs a
PDF library, which requires the owner's approval as a new dependency.
"""

import base64
import csv
import io
import json
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_EXTRACTED_CHARS = 500 * 1024        # 500 KB
MAX_PAGES = 100
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentParsingError(Exception):
    """Sanitized failure during document extraction or parsing."""


class DocumentTooLargeError(DocumentParsingError):
    """Uploaded document exceeds resource bounds."""


def sanitize_control_characters(text: str) -> str:
    """Removes non-printable control characters while preserving newlines and tabs."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def apply_injection_fence(filename: str, content_type: str, text: str) -> str:
    """Encloses untrusted extracted text within structural boundary delimiters."""
    sanitized_text = sanitize_control_characters(text)
    # Neutralize any attempted fence spoofing inside the document
    neutralized = (
        sanitized_text.replace("<<<UNTRUSTED_DOCUMENT_CONTENT", "[[[UNTRUSTED_DOCUMENT_CONTENT")
        .replace("<<<END_UNTRUSTED_DOCUMENT_CONTENT>>>", "[[[END_UNTRUSTED_DOCUMENT_CONTENT]]]")
    )
    return (
        f'<<<UNTRUSTED_DOCUMENT_CONTENT filename="{filename}" content_type="{content_type}">>>\n'
        f"{neutralized}\n"
        f"<<<END_UNTRUSTED_DOCUMENT_CONTENT>>>"
    )


def _is_pdf(filename: str, content_type: str, raw_bytes: bytes | None) -> bool:
    return (filename.endswith(".pdf") or "pdf" in content_type
            or (raw_bytes is not None and raw_bytes[:5] == b"%PDF-"))


def _is_docx(filename: str, content_type: str) -> bool:
    return filename.endswith(".docx") or "wordprocessingml" in content_type


def extract_docx_text(raw_bytes: bytes) -> str:
    """Paragraph text of a DOCX file, read with the standard library under strict limits."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocumentParsingError("The file is not a valid DOCX document (word/document.xml is missing).") from exc
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise DocumentTooLargeError("The DOCX text is too large to extract safely.")
            with archive.open(info) as handle:
                xml = handle.read(MAX_DOCX_XML_BYTES + 1)
    except zipfile.BadZipFile as exc:
        raise DocumentParsingError("The file is not a valid DOCX document.") from exc
    if len(xml) > MAX_DOCX_XML_BYTES:
        raise DocumentTooLargeError("The DOCX text is too large to extract safely.")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise DocumentParsingError("The DOCX document declares XML entities, which are refused for safety.")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DocumentParsingError("The DOCX document XML could not be parsed.") from exc
    paragraphs = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t"))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def parse_document_content(
    filename: str,
    content_type: str,
    raw_bytes: bytes | None = None,
    text_content: str | None = None,
) -> tuple[str, int, int]:
    """Parses raw document content into bounded, structurally fenced text.

    Returns:
        (fenced_text, file_size_bytes, page_count)
    """
    if raw_bytes is not None and len(raw_bytes) > MAX_FILE_SIZE_BYTES:
        raise DocumentTooLargeError(
            f"Document exceeds maximum supported size of {MAX_FILE_SIZE_BYTES // (1024*1024)}MB."
        )

    file_size = len(raw_bytes) if raw_bytes is not None else len(text_content.encode("utf-8") if text_content else b"")
    if file_size > MAX_FILE_SIZE_BYTES:
        raise DocumentTooLargeError(
            f"Document exceeds maximum supported size of {MAX_FILE_SIZE_BYTES // (1024*1024)}MB."
        )

    lower_name = filename.lower()
    lower_type = content_type.lower()
    extracted_text = ""
    page_count = 1

    if _is_pdf(lower_name, lower_type, raw_bytes):
        raise DocumentParsingError("PDF parsing is not available yet: it needs a PDF library, which has not been "
                                   "approved as a dependency. Nothing was stored.")
    if raw_bytes is not None and _is_docx(lower_name, lower_type):
        extracted_text = extract_docx_text(raw_bytes)
        if len(extracted_text) > MAX_EXTRACTED_CHARS:
            extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[Content truncated at 500KB limit]"
        page_count = min(MAX_PAGES, max(1, len(extracted_text) // 3000 + (1 if len(extracted_text) % 3000 else 0)))
        return apply_injection_fence(filename, content_type, extracted_text), file_size, page_count
    if raw_bytes is not None and (b"\x00" in raw_bytes[:8192] or raw_bytes[:2] == b"PK"):
        raise DocumentParsingError("This binary file type is not supported; only text, Markdown, JSON, CSV and DOCX "
                                   "documents can be parsed. Nothing was stored.")

    try:
        if "json" in lower_type or lower_name.endswith(".json"):
            raw_str = text_content if text_content is not None else (raw_bytes.decode("utf-8", errors="replace") if raw_bytes is not None else "")
            parsed_json = json.loads(raw_str)
            extracted_text = json.dumps(parsed_json, indent=2)
        elif "csv" in lower_type or lower_name.endswith(".csv"):
            raw_str = text_content if text_content is not None else (raw_bytes.decode("utf-8", errors="replace") if raw_bytes is not None else "")
            stream = io.StringIO(raw_str)
            reader = csv.reader(stream)
            rows = [", ".join(row) for row in reader]
            extracted_text = "\n".join(rows)
        elif text_content is not None:
            extracted_text = text_content
        elif raw_bytes is not None:
            # Text / Markdown / Fallback
            extracted_text = raw_bytes.decode("utf-8", errors="replace")
        else:
            extracted_text = ""
    except Exception as exc:
        raise DocumentParsingError(f"Failed to parse {filename}: {exc}") from exc

    # Enforce text character ceiling
    if len(extracted_text) > MAX_EXTRACTED_CHARS:
        extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[Content truncated at 500KB limit]"

    # Heuristic page count: 3,000 characters per page
    page_count = min(MAX_PAGES, max(1, len(extracted_text) // 3000 + (1 if len(extracted_text) % 3000 else 0)))

    fenced = apply_injection_fence(filename, content_type, extracted_text)
    return fenced, file_size, page_count
