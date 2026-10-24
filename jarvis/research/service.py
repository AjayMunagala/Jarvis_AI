"""Research service coordinating multi-format parsing, structural injection fencing, and synthesis (v3.4)."""

import base64
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.research.models import (
    Citation,
    DocumentParseRequest,
    ParsedDocument,
    ParsedDocumentPage,
    ResearchSynthesis,
    ResearchSynthesisPage,
    ResearchSynthesizeRequest,
)
from jarvis.research.parsers import parse_document_content
from jarvis.research.stores import ResearchStore

logger = logging.getLogger(__name__)


class ResearchService:
    def __init__(self, store: ResearchStore, audit_service: Any = None) -> None:
        self.store = store
        self.audit_service = audit_service

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="research",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record research audit event: %s", exc)

    def parse_and_store_document(
        self, owner_id: UUID, req: DocumentParseRequest
    ) -> ParsedDocument:
        raw_bytes = None
        if req.data_base64:
            raw_bytes = base64.b64decode(req.data_base64)

        fenced_text, file_size, page_count = parse_document_content(
            filename=req.filename,
            content_type=req.content_type,
            raw_bytes=raw_bytes,
            text_content=req.text_content,
        )

        now = datetime.now(timezone.utc)
        doc = ParsedDocument(
            id=uuid4(),
            owner_id=owner_id,
            filename=req.filename,
            content_type=req.content_type,
            file_size_bytes=file_size,
            page_count=page_count,
            extracted_text=fenced_text,
            metadata={"fenced": True, "encoding": "utf-8"},
            created_at=now,
        )

        self.store.save_document(doc)
        self._audit(
            owner_id,
            "research.document.parse",
            f"research:doc:{doc.id}",
            {"filename": doc.filename, "size": file_size, "pages": page_count},
        )
        return doc

    def get_document(self, owner_id: UUID, doc_id: UUID) -> ParsedDocument | None:
        return self.store.get_document(owner_id, doc_id)

    def list_documents(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> ParsedDocumentPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_documents(owner_id, limit=limit, offset=offset)
        return ParsedDocumentPage(items=items, total=total)

    def delete_document(self, owner_id: UUID, doc_id: UUID) -> bool:
        deleted = self.store.delete_document(owner_id, doc_id)
        if deleted:
            self._audit(
                owner_id,
                "research.document.delete",
                f"research:doc:{doc_id}",
                {"deleted": True},
            )
        return deleted

    def synthesize_research(
        self, owner_id: UUID, req: ResearchSynthesizeRequest
    ) -> ResearchSynthesis:
        now = datetime.now(timezone.utc)
        citations: list[Citation] = []
        findings: list[str] = []

        # Gather document evidence
        for doc_id in req.document_ids:
            doc = self.store.get_document(owner_id, doc_id)
            if doc:
                snippet = doc.extracted_text[:200].replace("\n", " ").strip()
                citations.append(
                    Citation(
                        source=doc.filename,
                        reference=f"doc:{doc.id}",
                        quote=snippet[:100],
                    )
                )
                findings.append(f"From {doc.filename}: {snippet}")

        # Web URLs are recorded as references only. v4.1: nothing fetches them, so the
        # citation must not claim the content was read or verified.
        for url in req.web_urls:
            citations.append(
                Citation(
                    source=url,
                    reference=url,
                    quote="Not fetched: URL recorded as a reference only; its content was not read or verified.",
                )
            )
            findings.append(f"{url}: listed as a reference; content not retrieved.")

        summary_text = (
            f"Synthesis on '{req.topic}':\n"
            + ("\n".join(f"- {f}" for f in findings) if findings else "No external findings supplied.")
        )

        synthesis = ResearchSynthesis(
            id=uuid4(),
            owner_id=owner_id,
            topic=req.topic,
            source_urls=req.web_urls,
            source_document_ids=req.document_ids,
            synthesis=summary_text,
            citations=citations,
            created_at=now,
        )

        self.store.save_synthesis(synthesis)
        self._audit(
            owner_id,
            "research.synthesize",
            f"research:synthesis:{synthesis.id}",
            {"topic": synthesis.topic, "sources_count": len(citations)},
        )
        return synthesis

    def get_synthesis(
        self, owner_id: UUID, synthesis_id: UUID
    ) -> ResearchSynthesis | None:
        return self.store.get_synthesis(owner_id, synthesis_id)

    def list_syntheses(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> ResearchSynthesisPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_syntheses(owner_id, limit=limit, offset=offset)
        return ResearchSynthesisPage(items=items, total=total)
