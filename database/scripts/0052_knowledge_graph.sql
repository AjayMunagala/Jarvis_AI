-- Purpose: owner-scoped knowledge graph entities and relationships (v5.2).
-- Numbered 0052 (v5.2) so it cannot collide with migrations added by the parallel v3.1-v4.0 workstream.
-- Composite foreign keys make cross-owner relationships impossible; inferred facts are never stored as verified.
CREATE TABLE jarvis_app.kg_entities (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    entity_type text NOT NULL,
    key text NOT NULL,
    name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT kg_entities_id_format CHECK (id ~ '^KE-[a-f0-9]{16}$'),
    CONSTRAINT kg_entities_type_allowed CHECK (entity_type IN ('person', 'project', 'document', 'task',
        'software_component', 'service', 'concept', 'event', 'organization')),
    CONSTRAINT kg_entities_key_length CHECK (length(key) BETWEEN 1 AND 200),
    CONSTRAINT kg_entities_name_length CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT kg_entities_json_shapes CHECK (jsonb_typeof(aliases) = 'array' AND jsonb_typeof(attributes) = 'object'
        AND jsonb_typeof(provenance) = 'array'),
    CONSTRAINT kg_entities_owner_key_unique UNIQUE (owner_id, entity_type, key),
    CONSTRAINT kg_entities_owner_id_unique UNIQUE (owner_id, id)
);

CREATE TABLE jarvis_app.kg_relationships (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    subject_id text NOT NULL,
    relation text NOT NULL,
    object_id text NOT NULL,
    status text NOT NULL,
    confidence double precision NOT NULL,
    provenance jsonb NOT NULL,
    conflicts_with jsonb NOT NULL DEFAULT '[]'::jsonb,
    superseded_by text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT kg_relationships_id_format CHECK (id ~ '^KR-[a-f0-9]{16}$'),
    CONSTRAINT kg_relationships_relation_allowed CHECK (relation IN ('belongs_to', 'depends_on', 'references',
        'created_by', 'related_to', 'describes', 'supersedes')),
    CONSTRAINT kg_relationships_status_allowed CHECK (status IN ('verified', 'inferred', 'disputed', 'superseded', 'retracted')),
    CONSTRAINT kg_relationships_confidence_range CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT kg_relationships_verified_confidence CHECK (status <> 'verified' OR confidence = 1),
    CONSTRAINT kg_relationships_no_self_loop CHECK (subject_id <> object_id),
    CONSTRAINT kg_relationships_provenance_present CHECK (jsonb_typeof(provenance) = 'array'
        AND jsonb_array_length(provenance) >= 1),
    CONSTRAINT kg_relationships_subject_owner FOREIGN KEY (owner_id, subject_id)
        REFERENCES jarvis_app.kg_entities (owner_id, id),
    CONSTRAINT kg_relationships_object_owner FOREIGN KEY (owner_id, object_id)
        REFERENCES jarvis_app.kg_entities (owner_id, id)
);

CREATE INDEX kg_relationships_owner_subject_idx ON jarvis_app.kg_relationships (owner_id, subject_id, relation);
CREATE INDEX kg_relationships_owner_object_idx ON jarvis_app.kg_relationships (owner_id, object_id);
