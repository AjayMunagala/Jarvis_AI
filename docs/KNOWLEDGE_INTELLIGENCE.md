# Knowledge Intelligence & Context Continuity (v3.3)

v3.3 introduces long-form document chunking, deterministic semantic embeddings, hybrid retrieval using Reciprocal Rank Fusion (RRF), and dynamic context budget allocation.

## Architecture & Mathematical Foundations

### 1. Document Partitioning & Token Estimation
- Ingested documents are split into overlapping text chunks (`chunk_size=500` characters, `chunk_overlap=50` characters) honoring whitespace and sentence boundaries.
- Token counts are calculated using standard heuristic bounding (`estimate_tokens(text)`), ensuring predictable token consumption.

### 2. Hybrid Retrieval with Reciprocal Rank Fusion (RRF)
Search candidates are scored using both lexical keyword indexing (Full-Text Search / PostgreSQL GIN indexes) and semantic vector similarity (cosine angle between normalized vector projections).

The independent rank lists are fused using Reciprocal Rank Fusion:
$$\text{RRF Score}(c) = \frac{1}{k + r_{\text{lexical}}(c)} + \frac{1}{k + r_{\text{semantic}}(c)}$$
where $k = 60$. Candidates matching both dimensions receive high combined priority without requiring manual calibration between keyword frequencies and cosine scores.

### 3. Context Budget Allocator
Guarantees LLM context constraints:
$$\text{Total Tokens} = T_{\text{system}} + T_{\text{prompt}} + \sum_{i} T_{\text{chunk}_i} \le T_{\text{max}}$$
The allocator dynamically selects top-scoring chunks that fit within the remaining token ceiling after accounting for system reserves and prompt tokens.

---

## Database Migration (`0009_knowledge_store.sql`)

- `jarvis_app.knowledge_documents`: ID, owner, title, source type (`manual`, `note`, `file`, `web`, `memory`), source URI, content, metadata JSONB, timestamps. GIN index on text search vector.
- `jarvis_app.knowledge_chunks`: ID, document_id, owner, chunk index, content, token count. Cascade delete on document removal.
- `jarvis_app.knowledge_embeddings`: chunk_id, owner, model name, embedding JSONB representation.

---

## API Endpoints

All endpoints are mounted under `/knowledge`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/knowledge/documents` | `POST` | `KNOWLEDGE_WRITE` | Ingest document and auto-partition into chunks |
| `/knowledge/documents` | `GET` | `KNOWLEDGE_READ` | List documents with pagination |
| `/knowledge/documents/{id}` | `GET` | `KNOWLEDGE_READ` | Retrieve document and chunks |
| `/knowledge/documents/{id}` | `DELETE` | `KNOWLEDGE_WRITE` | Delete document and cascade chunks |
| `/knowledge/search` | `POST` | `KNOWLEDGE_READ` | Hybrid search using RRF ranking |
| `/knowledge/context-budget` | `POST` | `KNOWLEDGE_READ` | Compute context budget allocation |

---

## Verification & Test Coverage

- `tests/test_knowledge.py`: Unit tests for chunking boundaries, token estimation, cosine vector similarity, RRF fusion, `MemoryKnowledgeStore`, `KnowledgeService` ingestion and context budget allocation.
- `tests/test_knowledge_api.py`: Integration tests for `/knowledge/*` endpoints, authentication, capability enforcement (401/403), and CRUD workflows.
