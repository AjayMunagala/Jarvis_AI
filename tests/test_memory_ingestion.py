import pytest

from jarvis.memory.ingestion import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_FILENAME_LENGTH,
    DocumentValidationError,
    sanitize_document_filename,
    validate_document,
)
from jarvis.memory.models import MAX_MEMORY_CONTENT_LENGTH


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("notes.txt", "notes.txt"),
        ("REPORT.MD", "REPORT.MD"),
        (r"C:\\private\\notes.md", "notes.md"),
        ("/private/notes.txt", "notes.txt"),
        ("r%C3%A9sum%C3%A9.md", "résumé.md"),
    ],
)
def test_sanitize_document_filename_keeps_only_supported_basename(filename, expected):
    assert sanitize_document_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "", "notes.pdf", "notes.txt.exe", "..", "folder/", "bad\nname.md",
        "notes%0Ahidden.md", "bad\u0085name.md", "bad%C2%85name.md", "C:notes.md",
    ],
)
def test_sanitize_document_filename_rejects_invalid_names(filename):
    with pytest.raises(DocumentValidationError):
        sanitize_document_filename(filename)


def test_sanitize_document_filename_enforces_basename_length_boundary():
    maximum_name = f"{'a' * (MAX_DOCUMENT_FILENAME_LENGTH - len('.txt'))}.txt"
    assert len(maximum_name) == MAX_DOCUMENT_FILENAME_LENGTH
    assert sanitize_document_filename(maximum_name) == maximum_name
    with pytest.raises(DocumentValidationError):
        sanitize_document_filename(f"{'a' * (MAX_DOCUMENT_FILENAME_LENGTH - len('.txt') + 1)}.txt")


def test_validates_utf8_content_and_strips_one_leading_bom():
    assert validate_document("notes.txt", b"\xef\xbb\xbfhello").content == "hello"
    # Exactly one BOM is stripped; a second is content and remains visible.
    assert validate_document("notes.txt", b"\xef\xbb\xbf\xef\xbb\xbfhello").content == "\ufeffhello"


@pytest.mark.parametrize(
    "raw",
    [b"\xff", b"\x00text", b" \t\r\n"],
)
def test_rejects_malformed_nul_and_blank_documents(raw):
    with pytest.raises(DocumentValidationError):
        validate_document("notes.md", raw)


def test_accepts_exact_size_limits_and_rejects_each_limit_plus_one():
    assert len(validate_document("notes.txt", b"x" * MAX_MEMORY_CONTENT_LENGTH).content) == MAX_MEMORY_CONTENT_LENGTH
    with pytest.raises(DocumentValidationError, match="4,000"):
        validate_document("notes.txt", b"x" * (MAX_MEMORY_CONTENT_LENGTH + 1))
    # The independent byte cap is checked before decoding; the character cap
    # necessarily rejects a 16 KiB ASCII payload first only after its byte check.
    with pytest.raises(DocumentValidationError, match="4,000"):
        validate_document("notes.txt", b"x" * MAX_DOCUMENT_BYTES)
    with pytest.raises(DocumentValidationError, match="16 KiB"):
        validate_document("notes.txt", b"x" * (MAX_DOCUMENT_BYTES + 1))
