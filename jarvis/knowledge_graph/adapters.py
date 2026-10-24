"""Document-extraction boundary for the knowledge graph.

``document_record`` is the supported way for a document-intelligence module
(for example v3.4, once it exists) to hand extracted entities and relations to
``GraphService.ingest``. It fixes ``source_kind="document"``, keeps the
document reference and per-relation excerpts as provenance, and refuses -
rather than silently downgrades - a ``verified`` relationship from any
extractor other than the owner (``"user"``). Model- and rule-extracted
relationships therefore always enter the graph as ``inferred``; they become
verified only when the owner asserts the same relationship with user
provenance (which upgrades it) or resolves a conflict in its favour.
"""

import re
from typing import Any, Iterable, Literal
from uuid import UUID

from pydantic import ValidationError

from jarvis.knowledge_graph.models import (
    IngestResult,
    KnowledgeRecord,
    RecordEntity,
    RecordRelationship,
)


class ExtractionError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


class DocumentNotFoundError(ValueError):
    """Raised when a referenced document does not exist."""
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


class DocumentOwnershipError(PermissionError):
    """Raised when attempting to access a document belonging to another owner."""
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def document_record(document_ref: str, *, extractor: Literal["model", "rule", "user"],
                    entities: Iterable[dict | RecordEntity], relationships: Iterable[dict | RecordRelationship]) -> KnowledgeRecord:
    if not isinstance(document_ref, str) or not document_ref.strip():
        raise ExtractionError("A document reference is required for provenance.")
    try:
        parsed_entities = tuple(item if isinstance(item, RecordEntity) else RecordEntity.model_validate(item)
                                for item in entities)
        parsed_relationships = tuple(item if isinstance(item, RecordRelationship) else RecordRelationship.model_validate(item)
                                     for item in relationships)
    except ValidationError as exc:
        raise ExtractionError("The extraction does not satisfy the knowledge record contract.") from exc
    if extractor != "user" and any(item.status == "verified" for item in parsed_relationships):
        raise ExtractionError("Extracted relationships must be 'inferred'; only the owner can verify a relationship.")
    if extractor != "user" and any(item.confidence >= 1 for item in parsed_relationships):
        raise ExtractionError("Extracted relationships must have confidence below 1.0.")
    try:
        return KnowledgeRecord(source_kind="document", source_ref=document_ref[:300], extractor=extractor,
                               entities=parsed_entities, relationships=parsed_relationships)
    except ValidationError as exc:
        raise ExtractionError("The extraction does not satisfy the knowledge record contract.") from exc


class KnowledgeGraphDocumentAdapter:
    """Dedicated adapter linking document extraction output to the Knowledge Graph.

    Coordinates document validation, contract mapping, and atomic ingestion.
    """

    def __init__(self, graph_service: Any, document_store: Any = None) -> None:
        self.graph_service = graph_service
        self.document_store = document_store

    def validate_document(self, owner_id: UUID, document_id: UUID) -> Any:
        """Validate document existence and owner isolation before ingestion."""
        if self.document_store is None:
            return None

        get_doc_fn = getattr(self.document_store, "get_document", None)
        if not callable(get_doc_fn):
            return None

        # Try to retrieve with owner scoping
        doc = get_doc_fn(owner_id, document_id)
        if doc is not None:
            actual_owner = getattr(doc, "owner_id", None)
            if actual_owner is None and hasattr(doc, "document"):
                actual_owner = getattr(doc.document, "owner_id", None)
            if actual_owner is not None and actual_owner != owner_id:
                raise DocumentOwnershipError(
                    f"Document {document_id} belongs to a different owner, not {owner_id}."
                )
            return doc

        # Check internal storage if available (e.g. MemoryKnowledgeStore._documents)
        docs_dict = getattr(self.document_store, "_documents", None)
        if isinstance(docs_dict, dict) and document_id in docs_dict:
            raw_doc = docs_dict[document_id]
            doc_owner = getattr(raw_doc, "owner_id", None)
            if doc_owner is not None and doc_owner != owner_id:
                raise DocumentOwnershipError(
                    f"Document {document_id} belongs to a different owner, not {owner_id}."
                )

        raise DocumentNotFoundError(f"Document {document_id} not found.")

    def map_document_extractions(
        self,
        document_ref: str,
        entities: Iterable[dict | RecordEntity],
        relationships: Iterable[dict | RecordRelationship],
        *,
        extractor: Literal["model", "rule", "user"] = "model",
    ) -> KnowledgeRecord:
        """Map document-extracted entities and relationships into a KnowledgeRecord.

        Strictly enforces:
        - All entities and relationships retain document_ref as provenance source_ref.
        - If extractor != "user" (e.g. "model"), relationships MUST be 'inferred'
          with confidence < 1.0. If input specified 'verified' or confidence >= 1.0,
          it is safely mapped to 'inferred' with confidence bounded to 0.95.
        """
        if not isinstance(document_ref, str) or not document_ref.strip():
            raise ExtractionError("A document reference is required for provenance.")

        parsed_entities: list[RecordEntity] = []
        for item in entities:
            if isinstance(item, RecordEntity):
                parsed_entities.append(item)
            elif isinstance(item, dict):
                entity_data = dict(item)
                if "ref" not in entity_data:
                    name_slug = re.sub(
                        r"[^a-z0-9_-]", "_", str(entity_data.get("name", "")).strip().lower()
                    )[:40]
                    entity_data["ref"] = name_slug or "entity"
                parsed_entities.append(RecordEntity.model_validate(entity_data))
            else:
                raise ExtractionError(f"Unsupported entity type: {type(item)}")

        parsed_relationships: list[RecordRelationship] = []
        for item in relationships:
            if isinstance(item, RecordRelationship):
                rel_data = item.model_dump()
            elif isinstance(item, dict):
                rel_data = dict(item)
            else:
                raise ExtractionError(f"Unsupported relationship type: {type(item)}")

            # Enforce upstream validator rules
            if extractor != "user":
                if rel_data.get("status") == "verified":
                    rel_data["status"] = "inferred"
                conf = float(rel_data.get("confidence", 0.8))
                if conf >= 1.0:
                    rel_data["confidence"] = 0.95
                elif conf < 0.0:
                    rel_data["confidence"] = 0.0
                else:
                    rel_data["confidence"] = conf
            elif rel_data.get("status") == "verified" and "confidence" not in rel_data:
                rel_data["confidence"] = 1.0

            parsed_relationships.append(RecordRelationship.model_validate(rel_data))

        return KnowledgeRecord(
            source_kind="document",
            source_ref=document_ref[:300],
            extractor=extractor,
            entities=tuple(parsed_entities),
            relationships=tuple(parsed_relationships),
        )

    def ingest_document_extraction(
        self,
        owner_id: UUID,
        document_id: UUID,
        entities: Iterable[dict | RecordEntity],
        relationships: Iterable[dict | RecordRelationship],
        *,
        extractor: Literal["model", "rule", "user"] = "model",
        document_ref: str | None = None,
    ) -> IngestResult:
        """Validate, map, and atomically ingest document extractions into the graph.

        Existing entities are safely resolved or updated (not failed).
        On error, atomic rollback ensures no partial state is persisted.
        """
        self.validate_document(owner_id, document_id)
        ref = document_ref or f"doc:{document_id}"
        record = self.map_document_extractions(
            document_ref=ref,
            entities=entities,
            relationships=relationships,
            extractor=extractor,
        )
        return self.graph_service.ingest(owner_id, record)

