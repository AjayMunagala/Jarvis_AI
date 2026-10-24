"""Research service coordinating multi-format parsing, structural injection fencing, and synthesis (v3.4)."""

import base64
import binascii
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
from jarvis.research.parsers import (DocumentParsingError, apply_injection_fence, extract_document_text,
                                    fence_attribute)
from jarvis.research.stores import ResearchStore

logger = logging.getLogger(__name__)


class ResearchService:
    def __init__(self, store: ResearchStore, audit_service: Any = None) -> None:
        self.store = store
        self.audit_service = audit_service
        self.knowledge_service: Any = None  # v4.6: set by the application for index_in_knowledge
        self.graph_service: Any = None

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
            try:
                raw_bytes = base64.b64decode(req.data_base64, validate=True)
            except (binascii.Error, ValueError) as exc:  # v4.6: was an unhandled 500
                raise DocumentParsingError("data_base64 is not valid base64.") from exc

        plain_text, file_size, page_count = extract_document_text(
            filename=req.filename,
            content_type=req.content_type,
            raw_bytes=raw_bytes,
            text_content=req.text_content,
        )
        fenced_text = apply_injection_fence(req.filename, req.content_type, plain_text)

        now = datetime.now(timezone.utc)
        doc_id = uuid4()
        metadata: dict[str, Any] = {"fenced": True, "encoding": "utf-8"}
        doc = ParsedDocument(
            id=doc_id,
            owner_id=owner_id,
            filename=req.filename,
            content_type=req.content_type,
            file_size_bytes=file_size,
            page_count=page_count,
            extracted_text=fenced_text,
            metadata=metadata,
            created_at=now,
        )

        self.store.save_document(doc)
        if req.index_in_knowledge:
            # v4.6: indexed only after the research document is stored, so a failed save leaves no orphans.
            metadata.update(self._index(owner_id, doc_id, req, plain_text))
            self.store.update_metadata(owner_id, doc_id, metadata)
            doc = doc.model_copy(update={"metadata": metadata})
        self._audit(
            owner_id,
            "research.document.parse",
            f"research:doc:{doc.id}",
            {"filename": doc.filename, "size": file_size, "pages": page_count},
        )
        return doc

    def _index(self, owner_id: UUID, doc_id: UUID, req: DocumentParseRequest, plain_text: str) -> dict[str, Any]:
        """v4.6: index the parsed document in the knowledge store and the knowledge graph; failures are reported."""
        result: dict[str, Any] = {}
        if self.knowledge_service is None:
            return {"knowledge_index": "unavailable"}
        try:
            from jarvis.knowledge.models import KnowledgeDocumentCreateRequest, SourceType

            if not plain_text.strip():
                return {"knowledge_index": "skipped (no text)"}
            # The plain text is indexed with every chunk marked untrusted: a fence around the whole document
            # would not survive chunking, so the marking travels with each chunk instead.
            detail = self.knowledge_service.ingest_document(owner_id, KnowledgeDocumentCreateRequest(
                title=fence_attribute(req.filename)[:200], content=plain_text, source_type=SourceType.FILE,
                source_uri=f"research:{doc_id}", metadata={"research_document_id": str(doc_id), "untrusted": True},
                untrusted=True))
            result.update(knowledge_index="indexed", knowledge_document_id=str(detail.document.id),
                          knowledge_chunks=len(detail.chunks))
        except Exception as exc:  # noqa: BLE001 - the parsed document is still stored; the failure is visible
            logger.warning("Research document %s could not be indexed: %s", doc_id, type(exc).__name__)
            return {"knowledge_index": f"failed ({type(exc).__name__})"}
        if self.graph_service is None:
            result["graph"] = "unavailable"
            return result
        try:
            from jarvis.knowledge_graph.models import EntityInput, Provenance

            entity = self.graph_service.upsert_entity(owner_id, EntityInput(
                entity_type="document", name=fence_attribute(req.filename)[:200], key=f"research:{doc_id}",
                attributes={"knowledge_document_id": result["knowledge_document_id"],
                            "content_type": req.content_type[:100]},
                provenance=Provenance(source_kind="document", source_ref=f"research:{doc_id}", extractor="rule")))
            result.update(graph="recorded", graph_entity_id=str(getattr(entity, "entity_id", getattr(entity, "id", ""))))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Research document %s could not be added to the graph: %s", doc_id, type(exc).__name__)
            result["graph"] = f"failed ({type(exc).__name__})"
        return result

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
        existing = self.store.get_document(owner_id, doc_id)
        metadata = (existing.metadata or {}) if existing else {}
        # v4.6: linked copies go first, best effort, so deleting (or compensating) a document leaves nothing behind
        # and a failure there never hides the delete.
        if metadata.get("knowledge_document_id") and self.knowledge_service is not None:
            try:
                self.knowledge_service.delete_document(owner_id, UUID(metadata["knowledge_document_id"]))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Linked knowledge copy of %s could not be deleted: %s", doc_id, type(exc).__name__)
        if metadata.get("graph_entity_id") and self.graph_service is not None:
            try:
                self.graph_service.delete_entity(owner_id, metadata["graph_entity_id"])
            except Exception as exc:  # noqa: BLE001 - for example already deleted
                logger.warning("Linked graph entity of %s could not be deleted: %s", doc_id, type(exc).__name__)
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
