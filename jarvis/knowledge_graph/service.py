"""Knowledge graph service: validation, conflicts, provenance, bounded traversal.

Conflict rules for *functional* relations (``belongs_to``, ``created_by``),
where a subject may have only one active object:

- existing **verified**, new **inferred** -> the new one is stored as
  ``disputed``; the verified fact is unchanged (an inference never replaces it);
- existing **inferred**, new **verified** -> the inference becomes
  ``superseded`` by the verified fact;
- both verified, or both inferred -> both become ``disputed`` and must be
  resolved by the owner (``resolve_conflict``).

Re-asserting the same triple merges provenance; an inference can be upgraded
to verified by verified evidence, a verified fact is never downgraded.
``depends_on`` cycles are rejected. Traversal is breadth-first with hard
depth and size limits.
"""

import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from uuid import UUID

from jarvis.knowledge_graph.models import (
    ACTIVE_STATUSES, MAX_PROVENANCE, RELATION_SCHEMA, Entity, EntityInput, IngestResult, KnowledgeRecord,
    Neighborhood, Provenance, Relationship, RelationshipInput,
)
from jarvis.knowledge_graph.stores import StorageError

MAX_DEPTH = 3
MAX_TRAVERSAL_ENTITIES = 100
MAX_EDGES_PER_ENTITY = 200
_CYCLE_SEARCH_LIMIT = 1000


class GraphError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.public_message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _merge_provenance(existing: tuple[Provenance, ...], new: Provenance) -> tuple[Provenance, ...]:
    if any(item.source_ref == new.source_ref and item.extractor == new.extractor for item in existing):
        return existing
    return (*existing, new)[-MAX_PROVENANCE:]


class GraphService:
    def __init__(self, store, *, max_entities: int = 5000, max_relationships: int = 20_000):
        self.store = store
        self.max_entities = max_entities
        self.max_relationships = max_relationships
        self._lock = threading.RLock()  # serializes multi-step updates within this process

    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except StorageError as exc:
            raise GraphError(503, "Knowledge graph storage is unavailable.") from exc

    # ------------------------------------------------------------ entities

    def upsert_entity(self, owner_id: UUID, data: EntityInput) -> Entity:
        stamped = data.provenance.model_copy(update={"recorded_at": data.provenance.recorded_at or _now()})
        with self._lock:
            current = self._call(self.store.find_entity, owner_id, data.entity_type, data.normalized_key)
            now = _now()
            if current is None:
                if self._call(self.store.count, owner_id)[0] >= self.max_entities:
                    raise GraphError(409, "The knowledge graph entity limit has been reached.")
                entity = Entity(entity_id="KE-" + secrets.token_hex(8), owner_id=owner_id, entity_type=data.entity_type,
                                key=data.normalized_key, name=data.name, aliases=data.aliases,
                                attributes=data.attributes, provenance=(stamped,), created_at=now, updated_at=now)
            else:
                aliases = tuple(dict.fromkeys((*current.aliases, *data.aliases)))[:10]
                entity = current.model_copy(update={
                    "aliases": aliases, "attributes": {**current.attributes, **data.attributes},
                    "provenance": _merge_provenance(current.provenance, stamped), "updated_at": now,
                })
            self._call(self.store.save_entity, entity)
            return entity

    def get_entity(self, owner_id: UUID, entity_id: str) -> Entity:
        entity = self._call(self.store.get_entity, owner_id, entity_id)
        if entity is None:
            raise GraphError(404, "The entity was not found.")
        return entity

    def search(self, owner_id: UUID, *, text: str | None = None, entity_type: str | None = None,
               limit: int = 20) -> tuple[Entity, ...]:
        return self._call(self.store.search_entities, owner_id, text=text, entity_type=entity_type,
                          limit=max(1, min(limit, 100)))

    def delete_entity(self, owner_id: UUID, entity_id: str) -> int:
        with self._lock:
            removed = self._call(self.store.delete_entity, owner_id, entity_id)
        if not removed:
            raise GraphError(404, "The entity was not found.")
        return removed

    # ------------------------------------------------------- relationships

    def add_relationship(self, owner_id: UUID, data: RelationshipInput) -> Relationship:
        provenance = data.provenance.model_copy(update={"recorded_at": data.provenance.recorded_at or _now()})
        with self._lock:
            subject = self._call(self.store.get_entity, owner_id, data.subject_id)
            target = self._call(self.store.get_entity, owner_id, data.object_id)
            if subject is None or target is None:
                raise GraphError(404, "Both relationship endpoints must be existing entities of this owner.")
            subjects, objects, functional = RELATION_SCHEMA[data.relation]
            if subject.entity_type not in subjects or target.entity_type not in objects:
                raise GraphError(422, f"{data.relation} cannot connect a {subject.entity_type} to a {target.entity_type}.")
            if data.relation == "supersedes" and subject.entity_type != target.entity_type:
                raise GraphError(422, "supersedes must connect two entities of the same type.")
            if data.relation == "depends_on" and self._reaches(owner_id, data.object_id, data.subject_id):
                raise GraphError(422, "This dependency would create a cycle.")

            existing = self._call(self.store.find_relationships, owner_id, data.subject_id, data.relation)
            now = _now()
            same = next((item for item in existing if item.object_id == data.object_id
                         and item.status not in {"retracted"}), None)
            if same is not None:
                return self._reassert(same, data, provenance, now)
            if self._call(self.store.count, owner_id)[1] >= self.max_relationships:
                raise GraphError(409, "The knowledge graph relationship limit has been reached.")

            relationship = Relationship(
                relationship_id="KR-" + secrets.token_hex(8), owner_id=owner_id, subject_id=data.subject_id,
                relation=data.relation, object_id=data.object_id, status=data.status, confidence=data.confidence,
                provenance=(provenance,), created_at=now, updated_at=now,
            )
            rivals = [item for item in existing if functional and item.status in ACTIVE_STATUSES
                      and item.object_id != data.object_id]
            updates: list[Relationship] = []
            for rival in rivals:
                if rival.status == "verified" and data.status == "inferred":
                    relationship = self._link(relationship, rival, status="disputed")
                elif rival.status == "inferred" and data.status == "verified":
                    updates.append(rival.model_copy(update={"status": "superseded",
                                                            "superseded_by": relationship.relationship_id,
                                                            "updated_at": now}))
                else:
                    relationship = self._link(relationship, rival, status="disputed")
                    updates.append(self._link(rival, relationship, status="disputed"))
            self._call(self.store.save_relationship, relationship)
            for item in updates:
                self._call(self.store.save_relationship, item)
            return relationship

    @staticmethod
    def _link(item: Relationship, other: Relationship, *, status: str) -> Relationship:
        conflicts = tuple(dict.fromkeys((*item.conflicts_with, other.relationship_id)))[:10]
        return item.model_copy(update={"status": status, "conflicts_with": conflicts, "updated_at": _now()})

    def _reassert(self, current: Relationship, data: RelationshipInput, provenance: Provenance, now: str) -> Relationship:
        update = {"provenance": _merge_provenance(current.provenance, provenance), "updated_at": now}
        if data.status == "verified" and current.status in {"inferred", "superseded"} and not current.conflicts_with:
            update.update(status="verified", confidence=1.0, superseded_by=None)
        elif current.status == "inferred":
            update["confidence"] = max(current.confidence, data.confidence)
        updated = current.model_copy(update=update)
        self._call(self.store.save_relationship, updated)
        return updated

    def _reaches(self, owner_id: UUID, start: str, goal: str) -> bool:
        seen, queue = {start}, deque([start])
        while queue and len(seen) < _CYCLE_SEARCH_LIMIT:
            node = queue.popleft()
            if node == goal:
                return True
            for edge in self._call(self.store.find_relationships, owner_id, node, "depends_on"):
                if edge.status in ACTIVE_STATUSES and edge.object_id not in seen:
                    seen.add(edge.object_id)
                    queue.append(edge.object_id)
        return goal in seen

    def _relationship(self, owner_id: UUID, relationship_id: str) -> Relationship:
        item = self._call(self.store.get_relationship, owner_id, relationship_id)
        if item is None:
            raise GraphError(404, "The relationship was not found.")
        return item

    def resolve_conflict(self, owner_id: UUID, keep_id: str, *, note: str | None = None) -> Relationship:
        """The owner confirms one disputed assertion; its rivals are superseded."""
        with self._lock:
            keep = self._relationship(owner_id, keep_id)
            if keep.status != "disputed":
                raise GraphError(409, "Only disputed relationships can be resolved.")
            now = _now()
            proof = Provenance(source_kind="user_statement", source_ref=f"resolution:{keep_id}", extractor="user",
                               excerpt=(note or "Owner resolved the conflict.")[:500], recorded_at=now)
            resolved = keep.model_copy(update={"status": "verified", "confidence": 1.0, "conflicts_with": (),
                                               "provenance": _merge_provenance(keep.provenance, proof), "updated_at": now})
            for rival_id in keep.conflicts_with:
                rival = self._call(self.store.get_relationship, owner_id, rival_id)
                if rival is not None and rival.status in ACTIVE_STATUSES:
                    self._call(self.store.save_relationship, rival.model_copy(update={
                        "status": "superseded", "superseded_by": keep_id, "conflicts_with": (), "updated_at": now}))
            self._call(self.store.save_relationship, resolved)
            return resolved

    def retract(self, owner_id: UUID, relationship_id: str) -> Relationship:
        with self._lock:
            item = self._relationship(owner_id, relationship_id)
            retracted = item.model_copy(update={"status": "retracted", "updated_at": _now()})
            self._call(self.store.save_relationship, retracted)
            return retracted

    # ----------------------------------------------------------- traversal

    def neighbors(self, owner_id: UUID, entity_id: str, *, depth: int = 1, max_entities: int = 50,
                  relations: tuple[str, ...] | None = None, include_inferred: bool = True,
                  include_inactive: bool = False) -> Neighborhood:
        depth = max(0, min(depth, MAX_DEPTH))
        max_entities = max(1, min(max_entities, MAX_TRAVERSAL_ENTITIES))
        root = self.get_entity(owner_id, entity_id)
        allowed = {"verified", "disputed"} | ({"inferred"} if include_inferred else set())
        if include_inactive:
            allowed |= {"superseded", "retracted"}
        entities = {root.entity_id: root}
        edges: dict[str, Relationship] = {}
        frontier, truncated = [root.entity_id], False
        for _level in range(depth):
            next_frontier = []
            for node in frontier:
                for edge in self._call(self.store.edges, owner_id, node, limit=MAX_EDGES_PER_ENTITY + 1)[:MAX_EDGES_PER_ENTITY]:
                    if edge.status not in allowed or (relations and edge.relation not in relations):
                        continue
                    other = edge.object_id if edge.subject_id == node else edge.subject_id
                    if other not in entities:
                        if len(entities) >= max_entities:
                            truncated = True
                            continue
                        found = self._call(self.store.get_entity, owner_id, other)
                        if found is None:
                            continue
                        entities[other] = found
                        next_frontier.append(other)
                    edges[edge.relationship_id] = edge
            frontier = next_frontier
            if not frontier:
                break
        note = ("Inferred and disputed relationships are not verified facts; check their status and provenance."
                if any(item.status != "verified" for item in edges.values()) else "All returned relationships are verified.")
        return Neighborhood(root_id=root.entity_id, depth=depth, entities=tuple(entities.values()),
                            relationships=tuple(sorted(edges.values(), key=lambda item: item.relationship_id)),
                            truncated=truncated, note=note)

    # ----------------------------------------------------------- ingestion

    def ingest(self, owner_id: UUID, record: KnowledgeRecord) -> IngestResult:
        """Store a normalized record; each item gets the record's source as provenance.

        All entities and relationship endpoints are validated before anything is
        written, so a record with an invalid relationship type is rejected whole.
        """
        types = {item.ref: item.entity_type for item in record.entities}
        for rel in record.relationships:
            subjects, objects, _ = RELATION_SCHEMA[rel.relation]
            if types[rel.subject_ref] not in subjects or types[rel.object_ref] not in objects:
                raise GraphError(422, f"{rel.relation} cannot connect a {types[rel.subject_ref]} to a {types[rel.object_ref]}.")
            if rel.status == "verified" and record.extractor == "model":
                raise GraphError(422, "model-extracted relationships can only be inferred")
        with self._lock:
            ids = {}
            for item in record.entities:
                entity = self.upsert_entity(owner_id, EntityInput(
                    entity_type=item.entity_type, name=item.name, key=item.key, aliases=item.aliases,
                    attributes=item.attributes, provenance=Provenance(source_kind=record.source_kind,
                                                                      source_ref=record.source_ref,
                                                                      extractor=record.extractor)))
                ids[item.ref] = entity.entity_id
            stored = []
            for rel in record.relationships:
                stored.append(self.add_relationship(owner_id, RelationshipInput(
                    subject_id=ids[rel.subject_ref], relation=rel.relation, object_id=ids[rel.object_ref],
                    status=rel.status, confidence=rel.confidence, provenance=Provenance(
                        source_kind=record.source_kind, source_ref=record.source_ref, extractor=record.extractor,
                        excerpt=rel.excerpt))))
        return IngestResult(entity_ids=ids, relationships=tuple(stored))
