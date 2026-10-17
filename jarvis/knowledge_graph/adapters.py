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

from typing import Iterable, Literal

from pydantic import ValidationError

from jarvis.knowledge_graph.models import KnowledgeRecord, RecordEntity, RecordRelationship


class ExtractionError(ValueError):
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
