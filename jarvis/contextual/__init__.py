"""Advanced memory selection and context packaging (v5.3).

Builds on ``jarvis.memory`` (records, lexical query terms) without replacing
it or changing its schema. Adds memory categories, multi-factor relevance,
owner/project isolation, contradiction handling, model-aware context budgets
and memory-update proposals. Similarity is lexical by default; an embedding
scorer can be plugged in through ``SimilarityScorer`` when semantic retrieval
exists upstream.
"""
