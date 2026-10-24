"""Document parsing engine with structural injection fencing and resource bounds (v3.4).

v4.6: DOCX files are parsed with the standard library (zip + a streaming expat
parser) under strict limits: at most 5 MB of uncompressed XML, text capped at
the extraction limit, and no DTD in any encoding (the parser refuses a
DOCTYPE itself, not just its bytes). PDF and other binary files are refused with a clear message
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
from xml.parsers import expat

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_EXTRACTED_CHARS = 500 * 1024        # 500 KB
MAX_PAGES = 100
MAX_DOCX_XML_BYTES = 5 * 1024 * 1024
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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
        f'<<<UNTRUSTED_DOCUMENT_CONTENT filename="{fence_attribute(filename)}" '
        f'content_type="{fence_attribute(content_type)}">>>\n'
        f"{neutralized}\n"
        f"<<<END_UNTRUSTED_DOCUMENT_CONTENT>>>"
    )


def fence_attribute(value: str) -> str:
    """v4.6: a header value that cannot close the fence: no control characters, newlines, quotes or angle brackets."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return re.sub(r'[<>"]', "_", cleaned)[:300]


def _is_pdf(filename: str, content_type: str, raw_bytes: bytes) -> bool:
    return raw_bytes[:5] == b"%PDF-" or content_type == "application/pdf" or filename.endswith(".pdf")


def _is_docx(filename: str, content_type: str) -> bool:
    return filename.endswith(".docx") or "wordprocessingml" in content_type


def extract_docx_text(raw_bytes: bytes) -> str:
    """Paragraph text of a DOCX file, read with a streaming expat parser under strict limits."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocumentParsingError("The file is not a valid DOCX document (word/document.xml is missing).") from exc
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise DocumentTooLargeError("The DOCX text is too large to extract safely.")
            with archive.open(info) as handle:
                xml = handle.read(MAX_DOCX_XML_BYTES + 1)  # bounds the real size, whatever the header declares
    except zipfile.BadZipFile as exc:
        raise DocumentParsingError("The file is not a valid DOCX document.") from exc
    if len(xml) > MAX_DOCX_XML_BYTES:
        raise DocumentTooLargeError("The DOCX text is too large to extract safely.")

    paragraphs: list[str] = []
    current: list[str] = []
    state = {"in_text": 0, "chars": 0}
    parser = expat.ParserCreate(namespace_separator=" ")

    def refuse_dtd(*_args):
        raise DocumentParsingError("The DOCX document declares a DTD or XML entities, which are refused for safety.")

    def start(name, _attributes):
        if name == f"{_WORD_NS} t":
            state["in_text"] += 1

    def end(name):
        if name == f"{_WORD_NS} t":
            state["in_text"] -= 1
        elif name == f"{_WORD_NS} p":
            if current:
                paragraphs.append("".join(current))
                current.clear()

    def text(data):
        if state["in_text"]:
            state["chars"] += len(data)
            if state["chars"] > MAX_EXTRACTED_CHARS:
                raise DocumentTooLargeError("The DOCX text exceeds the extraction limit.")
            current.append(data)

    parser.StartDoctypeDeclHandler = refuse_dtd
    parser.EntityDeclHandler = refuse_dtd
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(xml, True)
    except expat.ExpatError as exc:
        raise DocumentParsingError("The DOCX document XML could not be parsed.") from exc
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
    text, file_size, page_count = extract_document_text(filename, content_type, raw_bytes, text_content)
    return apply_injection_fence(filename, content_type, text), file_size, page_count


def extract_document_text(
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

    # v4.6: binary checks apply to uploaded bytes only; pre-extracted text_content is always accepted as text.
    if raw_bytes is not None and _is_pdf(lower_name, lower_type, raw_bytes):
        raise DocumentParsingError("PDF parsing is not available yet: it needs a PDF library, which has not been "
                                   "approved as a dependency. Nothing was stored.")
    if raw_bytes is not None and _is_docx(lower_name, lower_type):
        extracted_text = extract_docx_text(raw_bytes)
        if len(extracted_text) > MAX_EXTRACTED_CHARS:
            extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[Content truncated at 500KB limit]"
        page_count = min(MAX_PAGES, max(1, len(extracted_text) // 3000 + (1 if len(extracted_text) % 3000 else 0)))
        return extracted_text, file_size, page_count
    if raw_bytes is not None and (b"\x00" in raw_bytes[:8192] or raw_bytes[:4] == b"PK\x03\x04"):
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

    return extracted_text, file_size, page_count
