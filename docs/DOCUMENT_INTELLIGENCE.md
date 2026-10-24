# Research & Document Intelligence (v3.4)

v3.4 establishes multi-format document ingestion, structural prompt injection fencing, strict resource bounds, and cited research synthesis across documents and external resources.

## Architecture & Security Controls

### 1. Multi-Format Extraction Engine
- Ingests text, Markdown, CSV, and JSON data formats.
- Supports both raw string payloads (`text_content`) and base64-encoded binary/document bytes (`data_base64`).
- Strips non-printable ASCII control characters (`[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]`) while preserving valid structural whitespaces (tabs, newlines).

### 2. Structural Prompt Injection Fencing
Extracted document text is strictly isolated before downstream LLM synthesis:
- Untrusted content is framed within structural tags:
  ```
  <<<UNTRUSTED_DOCUMENT_CONTENT filename="..." content_type="...">>>
  ...neutralized document text...
  <<<END_UNTRUSTED_DOCUMENT_CONTENT>>>
  ```
- Neutralizes any adversarial attempts to close or spoof fences (`<<<UNTRUSTED_DOCUMENT_CONTENT` -> `[[[UNTRUSTED_DOCUMENT_CONTENT`).

### 3. Resource & DoS Bounds
- Hard file size limit: 10 MB (`HTTP 413 Request Entity Too Large` if violated).
- Maximum extracted text ceiling: 500 KB (512,000 characters). Content exceeding the limit is cleanly truncated with an explanatory notice.
- Maximum page count ceiling: 100 pages (heuristically computed at 3,000 characters per page).

### 4. Cited Research Synthesis
- Compiles extracted evidence from parsed documents and verified external web resources.
- Produces verifiable structured citations with source name, entity reference, and verbatim quote snippets.
- Audits all parse, synthesis, and deletion operations via `jarvis_app.audit_events`.

---

## Database Migration (`0010_document_ingestion.sql`)

- `jarvis_app.parsed_documents`: `id` (UUID PK), `owner_id` (UUID), `filename` (VARCHAR 300), `content_type` (VARCHAR 100), `file_size_bytes` (BIGINT), `page_count` (INT), `extracted_text` (TEXT), `metadata` (JSONB), `created_at` (TIMESTAMPTZ).
- `jarvis_app.research_syntheses`: `id` (UUID PK), `owner_id` (UUID), `topic` (VARCHAR 300), `source_urls` (JSONB), `source_document_ids` (JSONB), `synthesis` (TEXT), `citations` (JSONB), `created_at` (TIMESTAMPTZ).

---

## API Endpoints

All endpoints are mounted under `/research`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/research/documents/parse` | `POST` | `RESEARCH_WRITE` | Parse raw/base64 document with injection fencing |
| `/research/documents` | `GET` | `RESEARCH_READ` | List parsed documents with pagination |
| `/research/documents/{id}` | `GET` | `RESEARCH_READ` | Retrieve specific parsed document |
| `/research/documents/{id}` | `DELETE` | `RESEARCH_WRITE` | Delete parsed document |
| `/research/synthesize` | `POST` | `RESEARCH_WRITE` | Generate cited multi-source synthesis |
| `/research/syntheses` | `GET` | `RESEARCH_READ` | List research syntheses with pagination |
| `/research/syntheses/{id}` | `GET` | `RESEARCH_READ` | Retrieve specific research synthesis |

---

## Verification & Test Coverage

- `tests/test_research.py`: Unit tests for control character sanitization, prompt injection fencing, format parsing (CSV, JSON, Markdown), size bounds, truncation, and service lifecycle.
- `tests/test_research_api.py`: FastAPI TestClient integration tests verifying authentication, capability checks (401/403), document parse CRUD, synthesis generation, and 404/413/422 status handling.
