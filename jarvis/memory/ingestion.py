"""Bounded validation for explicit plain-text memory imports."""

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from unicodedata import category
from urllib.parse import unquote

from jarvis.memory.models import MAX_MEMORY_CONTENT_LENGTH

MAX_DOCUMENT_BYTES = 16 * 1024
MAX_DOCUMENT_FILENAME_LENGTH = 255
MAX_ENCODED_FILENAME_LENGTH = 4096
SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".txt", ".md"})


class DocumentValidationError(ValueError):
    """Input rejection with a safe, non-content-bearing public message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


@dataclass(frozen=True)
class ValidatedDocument:
    filename: str
    content: str


def sanitize_document_filename(filename: str) -> str:
    """Return only a validated basename; never retain client-supplied paths."""
    if not filename or len(filename) > MAX_ENCODED_FILENAME_LENGTH:
        raise DocumentValidationError("A valid .txt or .md filename is required.")
    try:
        filename = unquote(filename, errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("A valid .txt or .md filename is required.") from exc
    if any(category(char) == "Cc" for char in filename):
        raise DocumentValidationError("A valid .txt or .md filename is required.")

    windows_path = PureWindowsPath(filename)
    if windows_path.drive and not windows_path.root:
        raise DocumentValidationError("A valid .txt or .md filename is required.")

    basename = PurePosixPath(filename.replace("\\", "/")).name
    windows_basename = PureWindowsPath(basename)
    if (
        not basename
        or basename in {".", ".."}
        or (windows_basename.drive and not windows_basename.root)
        or len(basename) > MAX_DOCUMENT_FILENAME_LENGTH
        or PurePosixPath(basename).suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES
    ):
        raise DocumentValidationError("Only .txt and .md documents are supported.")
    return basename


def validate_document(filename: str, raw: bytes) -> ValidatedDocument:
    """Validate bounded bytes and strict UTF-8 before persistence."""
    safe_filename = sanitize_document_filename(filename)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError("Document exceeds the 16 KiB upload limit.")
    try:
        # utf-8-sig removes one leading UTF-8 BOM and rejects malformed bytes.
        content = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("Document must contain valid UTF-8 text.") from exc
    if len(content) > MAX_MEMORY_CONTENT_LENGTH:
        raise DocumentValidationError("Document exceeds the 4,000 character content limit.")
    if "\x00" in content:
        raise DocumentValidationError("Document must not contain NUL characters.")
    if not content.strip():
        raise DocumentValidationError("Document must not be blank.")
    return ValidatedDocument(filename=safe_filename, content=content)
