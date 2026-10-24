"""Research and document intelligence subsystem (v3.4)."""

from jarvis.research.models import (
    Citation,
    DocumentParseRequest,
    ParsedDocument,
    ParsedDocumentPage,
    ResearchSynthesis,
    ResearchSynthesisPage,
    ResearchSynthesizeRequest,
)
from jarvis.research.parsers import (
    DocumentParsingError,
    DocumentTooLargeError,
    apply_injection_fence,
    parse_document_content,
)
from jarvis.research.service import ResearchService
from jarvis.research.stores import MemoryResearchStore, PostgresResearchStore, ResearchStore

__all__ = [
    "Citation",
    "DocumentParseRequest",
    "DocumentParsingError",
    "DocumentTooLargeError",
    "MemoryResearchStore",
    "ParsedDocument",
    "ParsedDocumentPage",
    "PostgresResearchStore",
    "ResearchService",
    "ResearchStore",
    "ResearchSynthesis",
    "ResearchSynthesisPage",
    "ResearchSynthesizeRequest",
    "apply_injection_fence",
    "parse_document_content",
]
