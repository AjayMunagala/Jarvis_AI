"""Owner-scoped knowledge graph foundation (v5.2).

Entities and typed relationships with provenance, confidence and explicit
status. Inferred relationships are never treated as verified facts, and a
verified fact is never silently replaced by an inference. The graph consumes
normalized ``KnowledgeRecord`` inputs; it does not ingest or retrieve documents
itself (that belongs to the knowledge/document intelligence modules).
"""
