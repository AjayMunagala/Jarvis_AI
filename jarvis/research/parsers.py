"""Document parsing engine with structural injection fencing and resource bounds (v3.4).

v4.6: DOCX files are parsed with the standard library (zip + a streaming expat
parser) under strict limits: at most 5 MB of uncompressed XML, text capped at
the extraction limit, and no DTD in any encoding (the parser refuses a
DOCTYPE itself, not just its bytes). Other binary files are refused with a clear message instead of being
decoded as text and stored as garbage.

v6.2: PDF text is extracted with ``pypdf`` (the optional ``documents`` extra;
without it, PDFs are refused with a message saying how to enable them).
Encrypted PDFs are refused, at most ``MAX_PAGES`` pages are read (the rest is
cut off with a visible note), the text is capped like DOCX, and extraction
stops after ``PDF_TIME_BUDGET_SECONDS`` so a hostile file cannot hang a
request. A PDF that contains only images yields no text and is reported as
such (no OCR).
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
PDF_TIME_BUDGET_SECONDS = 20.0
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


def extract_pdf_text(raw_bytes: bytes) -> tuple[str, int]:
    """Text and page count of a PDF, read with pypdf under strict limits (v6.2)."""
    import time

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise DocumentParsingError("PDF parsing needs the optional 'documents' extra (pip install -e \".[documents]\"). "
                                   "Nothing was stored.") from exc
    try:
        reader = PdfReader(io.BytesIO(raw_bytes), strict=False)
        if reader.is_encrypted:
            raise DocumentParsingError("The PDF is encrypted or password-protected; it was not opened. Nothing was stored.")
        total_pages = len(reader.pages)
        started = time.monotonic()
        parts: list[str] = []
        chars = 0
        stopped = None
        for index, page in enumerate(reader.pages):
            if index >= MAX_PAGES:
                stopped = f"[Only the first {MAX_PAGES} of {total_pages} pages were read]"
                break
            if time.monotonic() - started > PDF_TIME_BUDGET_SECONDS:
                stopped = f"[Extraction stopped after {index} of {total_pages} pages: time limit reached]"
                break
            text = page.extract_text() or ""
            parts.append(text.strip())
            chars += len(text)
            if chars > MAX_EXTRACTED_CHARS:
                stopped = f"[Extraction stopped after {index + 1} of {total_pages} pages: text limit reached]"
                break
    except DocumentParsingError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError,
            OSError, NotImplementedError) as exc:
        raise DocumentParsingError("The PDF could not be read (it may be damaged or use unsupported features). "
                                   "Nothing was stored.") from exc
    text = "\n\n".join(part for part in parts if part)
    if not text.strip():
        raise DocumentParsingError("The PDF contains no extractable text (it may be scanned images; OCR is not "
                                   "supported). Nothing was stored.")
    if stopped:
        text += "\n" + stopped
    return text, max(1, min(total_pages, MAX_PAGES))


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
        extracted_text, page_count = extract_pdf_text(raw_bytes)
        if len(extracted_text) > MAX_EXTRACTED_CHARS:
            extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[Content truncated at 500KB limit]"
        return extracted_text, file_size, page_count
    if raw_bytes is not None and _is_docx(lower_name, lower_type):
        extracted_text = extract_docx_text(raw_bytes)
        if len(extracted_text) > MAX_EXTRACTED_CHARS:
            extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[Content truncated at 500KB limit]"
        page_count = min(MAX_PAGES, max(1, len(extracted_text) // 3000 + (1 if len(extracted_text) % 3000 else 0)))
        return extracted_text, file_size, page_count
    if raw_bytes is not None and (b"\x00" in raw_bytes[:8192] or raw_bytes[:4] == b"PK\x03\x04"):
        raise DocumentParsingError("This binary file type is not supported; only text, Markdown, JSON, CSV, DOCX "
                                   "and PDF documents can be parsed. Nothing was stored.")

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
