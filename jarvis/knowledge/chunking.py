"""Text chunking, deterministic embeddings, and Reciprocal Rank Fusion (RRF) (v3.3)."""

import math
import re
from typing import Any
from uuid import UUID

from jarvis.knowledge.models import KnowledgeChunk, ScoredChunk


def estimate_tokens(text: str) -> int:
    """Heuristic token count based on standard ~4 characters per token."""
    return max(1, math.ceil(len(text) / 4))


def chunk_text(
    text: str,
    *,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[str]:
    """Splits text into overlapping bounded chunks honoring line/sentence boundaries when possible."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            # Try to break at newline or space
            last_break = text.rfind("\n", start, end)
            if last_break == -1 or last_break <= start:
                last_break = text.rfind(" ", start, end)
            if last_break > start:
                end = last_break

        chunk_str = text[start:end].strip()
        if chunk_str:
            chunks.append(chunk_str)

        start = max(start + 1, end - chunk_overlap)
        if start >= text_len:
            break

    return chunks


def compute_deterministic_embedding(text: str, dim: int = 64) -> list[float]:
    """Deterministic, normalized pseudo-semantic vector representation for offline environments."""
    words = re.findall(r"\w+", text.lower())
    vector = [0.0] * dim
    for word in words:
        h = hash(word)
        idx = abs(h) % dim
        vector[idx] += 1.0

    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude > 0.0:
        return [round(v / magnitude, 4) for v in vector]
    return vector


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculates cosine similarity between two float vectors."""
    if len(vec1) != len(vec2) or not vec1:
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot_product / (norm_a * norm_b)))


def reciprocal_rank_fusion(
    lexical_ranked: list[tuple[KnowledgeChunk, str]],
    semantic_ranked: list[tuple[KnowledgeChunk, str]],
    *,
    k: int = 60,
    top_k: int = 5,
) -> list[ScoredChunk]:
    """Reciprocal Rank Fusion (RRF) combining keyword and semantic rankings."""
    rrf_scores: dict[UUID, float] = {}
    chunk_map: dict[UUID, KnowledgeChunk] = {}
    doc_titles: dict[UUID, str] = {}
    match_sources: dict[UUID, set[str]] = {}

    for rank, (chunk, title) in enumerate(lexical_ranked, start=1):
        cid = chunk.id
        chunk_map[cid] = chunk
        doc_titles[cid] = title
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
        match_sources.setdefault(cid, set()).add("lexical")

    for rank, (chunk, title) in enumerate(semantic_ranked, start=1):
        cid = chunk.id
        chunk_map[cid] = chunk
        doc_titles[cid] = title
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
        match_sources.setdefault(cid, set()).add("semantic")

    sorted_chunks = sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)

    results: list[ScoredChunk] = []
    for cid, score in sorted_chunks[:top_k]:
        sources = match_sources[cid]
        match_type = "hybrid_rrf" if len(sources) > 1 else list(sources)[0]
        results.append(
            ScoredChunk(
                chunk=chunk_map[cid],
                document_title=doc_titles[cid],
                score=round(score, 4),
                match_type=match_type,
            )
        )

    return results
