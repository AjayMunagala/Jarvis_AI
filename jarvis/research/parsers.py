"""Document parsing engine with structural injection fencing and resource bounds (v3.4)."""

import base64
import csv
import io
import json
import re
from typing import Any

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_EXTRACTED_CHARS = 500 * 1024        # 500 KB
MAX_PAGES = 100


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
