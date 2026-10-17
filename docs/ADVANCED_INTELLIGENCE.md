# Advanced intelligence and personalization (v5.1–v5.6)

This workstream adds an intelligence layer to JARVIS. It decides which model fits a request, represents knowledge as a graph, selects and packages relevant memory, validates non-text inputs, checks the quality of plans and answers, and adapts to the owner's explicit preferences.

It was built in parallel with the v3.1–v4.0 workstream, on branch `claude/jarvis-v5.1-to-v5.6`, from the accepted v3.0 baseline (`jarvis/integration-acceptance-v30`, `7bfb919`). **It is not integrated with v3.1–v4.0**, and no such integration has been verified.

## Design rules

- **New packages only.** The work lives in `jarvis/routing`, `jarvis/knowledge_graph`, `jarvis/contextual`, `jarvis/multimodal`, `jarvis/reasoning` and `jarvis/personalization`, plus `jarvis/advanced_intelligence.py` and `jarvis/api/routes/intelligence.py`. Shared files changed minimally:
  - `jarvis/main.py`: 3 lines.
  - `jarvis/security/policy.py`: 4 capabilities.
  - `pyproject.toml`: package data.
  - Migrations `0052` and `0056`.
- **Nothing is replaced.** The chat path, the existing provider adapters, the planner, the tool runtime, the approval gateway and audit, and memory storage are unchanged. The router wraps the existing `IntelligenceProvider` adapters.
- **No second engine.** This layer does not add a workflow or DAG engine, scheduler, memory store, approval gateway, audit service or deployment system. The reasoning package evaluates plans; it never executes them.
- **Mandatory security boundary.** Routing never grants permissions. Model output is never trusted as an instruction. Every new endpoint is authenticated and checks its capability server-side.

## v5.1 — Model registry, routing and evaluation (`jarvis/routing`)

**Registry.** The registry (`config/models.json`) is a validated `ModelDescriptor` for each model. A descriptor records the provider, model id, task types, input modalities, context and output limits, tool-calling and structured-output support, locality, demo flag, configuration requirements, limitations and optional pricing.
- **Nothing is invented.** The two packaged models declare only what their adapters guarantee. Unknown context limits, prices and specializations stay unknown (`null`). An unknown capability never satisfies a requirement.
- **Pricing.** It can be recorded only with a source and a date.
- **Availability.** It comes from the adapters actually built at startup, not from the file.

**Capability matching.** `requirements.infer_requirements` is a transparent keyword classifier. It returns the matched signals so callers can see why a task type was chosen. Callers can also pass `TaskRequirements` directly.

**Policies.** The policies (`config/policies.json`) are `balanced`, `quality`, `fast` and `private`. Each has explicit weights for suitability, quality, latency, cost and locality, plus the switches `allow_demo`, `allow_remote`, `preferred_providers` and `max_fallbacks` (0–3).

**Eligibility rules.** These are hard rules, and each exclusion is explained:
- the model's adapter is configured;
- demo and remote models are used only when both the policy and the privacy requirement allow them;
- the model supports every requested modality;
- its declared context capacity covers the request;
- it supports structured output or tool calling when the request needs it;
- its measured latency is within the request's limit.

**Fallback.** The router tries at most `max_fallbacks` further models from the same eligible list. A fallback therefore can never relax privacy or modality constraints. Provider errors, timeouts and adapter crashes are reported as sanitized codes.

**Evaluation.** `EvaluationHarness` runs a dataset sequentially against chosen models and reports:
- correctness, against deterministic expectations; no model grades another model;
- structured-output validity, completion rate and failure rate;
- p50 and p95 latency;
- estimated cost, only when operator pricing exists.

Runs are reproducible from the dataset id, version and SHA-256. Summaries from demo or scripted adapters are marked `simulated`.

**Evidence.** Evaluation results change routing only through an explicit `accept_evidence` call. Simulated summaries are refused unless the caller allows them, which only tests do. Policies themselves never change.

**Observability.** `RoutingRecord`s keep the decision, policy, selected model, attempts, a prompt SHA-256 and the prompt length. They never keep prompt or reply text.

## v5.2 — Knowledge graph (`jarvis/knowledge_graph`)

**Entities.** There are nine entity types. Each entity has a normalized key that is unique per owner and type, aliases, scalar attributes and accumulated provenance.

**Relationships.** There are seven relation types: `belongs_to`, `depends_on`, `references`, `created_by`, `related_to`, `describes` and `supersedes`. A schema lists the allowed subject and object types for each relation.

**Status and confidence.** Relationships carry a status (`verified`, `inferred`, `disputed`, `superseded`, `retracted`), a confidence and provenance:
- a verified relationship must have confidence 1.0;
- model-extracted relationships can only be `inferred`.

**Conflicts.** For functional relations (`belongs_to`, `created_by`):
- an inference conflicting with a verified fact becomes `disputed`, and the fact stays;
- verified evidence supersedes an earlier inference;
- two equally strong claims are both `disputed` until the owner calls `resolve_conflict`.

Re-asserting the same relationship merges its provenance. A verified fact is never downgraded. `depends_on` cycles are rejected.

**Traversal.** Traversal is breadth-first with depth ≤ 3 and at most 100 entities. It reports when it truncates and labels non-verified edges. Retracted and superseded edges are hidden by default.

**Upstream contract.** `KnowledgeRecord` is the normalized record a knowledge or document module hands the graph: source, extractor, entities with local references, and relationships. Each record is validated whole before anything is written. The graph does not ingest documents itself.

**Storage.** In-memory storage is the default. PostgreSQL is used when persistence is enabled; migration `0052_knowledge_graph.sql` adds composite foreign keys on `(owner_id, id)`, so cross-owner edges are impossible in the database itself.

**Limitation.** Multi-step updates are serialized within one process. Several processes writing the same owner's graph concurrently are not coordinated.

## v5.3 — Memory and context management (`jarvis/contextual`)

**Categories.** The six categories are `conversation`, `task`, `long_term`, `project`, `procedural` and `retrieved_knowledge`. Adapters map existing `MemoryRecord` and `HistoryEntry` data onto them unchanged. No memory schema changed.

**Selection pipeline.** Each omission records its reason.
1. **Isolation.** Records belonging to other owners or other projects are removed before scoring.
2. **Filters.** Records outside the requested categories are dropped, and outdated records are dropped unless requested.
3. **Contradictions.** Between structured facts, the more reliable record is kept (verified > user-stated > document > inferred). Ties are flagged and both records are kept.
4. **Scoring.** Each record is scored on similarity, recency (per-category half-life), reliability, scope, owner pins and task match. The component scores are returned.
5. **Packing.** Records are packed into a token budget. The budget comes from the selected model's declared context minus the reserved output tokens, or from a conservative 3,000 tokens when the context is unknown.

**Rendering.** The package is rendered as JSON under an "untrusted" header with references (`M1`, `D1`, …).

**Similarity is lexical** and reuses `jarvis.memory.query`. `SimilarityScorer` is the plug-in point for semantic retrieval once the knowledge workstream provides embeddings. pgvector is still not used (decision D6).

**Update proposals.** Memory updates (`add`, `mark_outdated`) need an owner decision. Accepting an `add` calls the existing repository's `create`. "Outdated" decisions are an in-memory overlay. Making them durable needs a memory-schema change, which belongs to the knowledge workstream.

## v5.4 — Multimodal foundation (`jarvis/multimodal`)

**Content parts.** There are four kinds: text, image (base64 only), speech transcript and browser visual observation. No file paths or URLs are accepted.

**Image validation.**
- The data must be strict base64.
- Its file signature must match the declared type: PNG, JPEG, GIF or WebP. SVG is rejected.
- Dimensions are read from the header without decoding pixels.
- Limits: 5 MiB per image, 12 MiB per request, 8192 px per side and 40 megapixels.

Transcripts and observations are secret-redacted.

**Routing.** Capability checks use the v5.1 router.
- Images or transcripts force `local_only` routing unless the request sets `allow_external_disclosure`.
- Only providers with a registered multimodal adapter receive media.

**Results.** Direct observations, measured by JARVIS, are kept separate from model interpretations. Interpretations are labelled untrusted, and labelled `simulated` when they come from a mock adapter.

**Only mock multimodal adapters exist.** The default registry has no image-capable model, so `/intelligence/multimodal/inspect` honestly reports no eligible model. No real image, audio or video understanding is claimed.

## v5.5 — Reasoning and plan evaluation (`jarvis/reasoning`)

**Plan evaluation.** `evaluate_plan` runs seven deterministic checks:
- goal coverage;
- constraints: step limit, forbidden action kinds and tools, required steps;
- dependencies: unknown, self-referential, cycles;
- information: every input must be provided or produced by a prerequisite step;
- redundancy;
- observable completion criteria;
- safety: consequential steps must require approval, and they are only ever proposed.

The verdict is `pass`, `needs_revision` or `reject`. "Pass" means well-formed, never completed.

**Strategies.** Six task classes have strategies: coding analysis, technical comparison, research synthesis, constraint satisfaction, decision analysis and risk analysis. Each strategy has an instruction and an answer-structure check. The check confirms the sections are present; it does not judge correctness.

**Uncertainty and verification.**
- A claim with credible contradicting evidence is `contested`.
- A model verifier is an opinion. Model agreement alone gives `plausible_unverified`, never `verified`.

**Refinement.** Refinement is bounded: at most 3 attempts, a deadline, and a stop when there is no progress. Refiner output is re-validated. Results carry `executed = false`.

**Measured result.** Dataset `plan-quality-v1` has 13 hand-labelled cases. The evaluator reaches verdict accuracy **12/13 (0.923)**. The structural baseline, which mirrors the existing `ExecutionPlan` validation, reaches **5/13 (0.385)**. The single miss is kept visible: a duplicated step only raises a warning. The dataset is small and author-written, so it shows relative improvement and catches regressions; it does not measure general planning quality.

**Integration interface.** `from_execution_plan` adapts the existing orchestration plan. The planner can call `evaluate_plan` once the DAG workstream has stabilized its plan contract.

## v5.6 — Personalization (`jarvis/personalization`)

**Catalog.** There are seven allowlisted preferences:
- `response_detail`
- `output_format`
- `language` (a BCP 47 tag)
- `coding_explanation`
- `preferred_providers`
- `latency_quality`
- `privacy_routing`

Permissions, approvals, tools and security settings cannot be represented at all.

**Scopes and provenance.** A preference is global or scoped to a task type; the task-type scope wins. Its provenance is `explicit` or `inferred_confirmed`.

**Inference.** Inference uses fixed observation kinds only, for example "asked for a shorter answer", and needs at least 3 consistent observations. It never overrides an explicit preference or contrary evidence. It creates **proposals only**, which the owner accepts or rejects.

**Owner controls.** The owner can inspect, set, remove, disable and reset preferences. Disabling takes effect immediately and keeps the data for inspection. Reset deletes everything.

**Safety invariance.**
- Routing application can tighten privacy and reorder eligible models. It never loosens a request's privacy, never changes `allow_remote`, `allow_demo` or fallbacks, and never adds models. An exhaustive combination test checks this.
- Presentation directives are formatting-only text.

**Storage.** In-memory by default, or PostgreSQL with migration `0056_personalization.sql`.

## API (`/intelligence`, owner bearer token required)

| Area | Endpoints | Capability (always plus `intelligence.read`) |
|---|---|---|
| Models and routing | `GET /models`, `POST /route`, `GET /routing/recent` | none extra |
| Evaluation | `POST /evaluations`, `POST /evaluations/{id}/accept` | `intelligence.evaluate` |
| Graph | `GET /graph/entities[/{id}]`, `GET /graph/entities/{id}/neighbors` | none extra |
| Graph writes | `POST /graph/entities`, `DELETE /graph/entities/{id}`, `POST /graph/relationships`, `POST /graph/relationships/{id}/resolve` and `/retract`, `POST /graph/records` | `knowledge_graph.write` |
| Context | `POST /context/preview` (needs persistence), `GET /memory/proposals` | `memory.read` |
| Memory proposals | `POST /memory/proposals/outdated`, `POST /memory/proposals/{id}/accept` and `/reject` | `memory.write` |
| Multimodal | `POST /multimodal/inspect` (validates and checks capabilities; makes no model call) | none extra |
| Reasoning | `POST /plans/evaluate`, `GET /reasoning/strategies`, `POST /answers/check` | none extra |
| Preferences | `GET /preferences`, `GET /preferences/proposals` | none extra |
| Preference changes | `PUT` and `DELETE /preferences/{key}`, `POST /preferences/disable`, `/enable`, `/reset`, `/observations`, `POST /preferences/proposals/{id}/accept` and `/reject` | `personalization.manage` |

`POST /evaluations` calls the configured provider, the same one chat uses. It is bounded to 5 cases × at most 5 repeats × at most 3 models, and it runs only when the owner asks.

## Tests and evidence

| File | Result | Evidence type |
|---|---|---|
| `tests/test_model_routing.py` | 29 passed | scripted adapters |
| `tests/test_knowledge_graph.py` | 20 passed | in-memory store; PostgreSQL through a fake connection |
| `tests/test_context_management.py` | 13 passed | fixtures |
| `tests/test_multimodal.py` | 15 passed | generated images and mock adapters |
| `tests/test_reasoning.py` | 23 passed | hand-labelled dataset |
| `tests/test_personalization.py` | 11 passed | in-memory store; PostgreSQL through a fake connection |
| `tests/test_intelligence_api.py` | 30 passed | FastAPI TestClient, mock provider |

**Not tested (unavailable):**
- live model providers, so there are no real quality or latency numbers;
- live PostgreSQL for migrations 0052 and 0056;
- real multimodal models.

No model performance or pricing data is claimed.

## Integration dependencies on the v3.1–v4.0 workstream

These are documented contracts. None has been verified against that branch yet.

| Upstream (Antigravity) | Contract here | Status |
|---|---|---|
| v3.2 DAG planner / v3.8 cross-module workflows | `reasoning.evaluate_plan(PlanCandidate)`; write an adapter like `from_execution_plan` for the new DAG plan type | Adapter pending the DAG plan contract |
| v3.3 knowledge intelligence | `contextual.SimilarityScorer` for semantic similarity; `MemoryCandidate` adapters; durable "outdated" state needs a memory-schema decision | Lexical similarity until then |
| v3.4 document intelligence | Produce `knowledge_graph.KnowledgeRecord`s from extracted entities and relations (model-extracted relations stay `inferred`) | Contract ready; producer pending |
| v3.5 proactive / v3.9 controlled improvement | `EvaluationReport` and `compare_to_baseline` outputs can serve as evidence; this workstream implements no self-improvement or deployment engine | Evidence only |
| Chat path | Use `ModelRouter.decide/execute` for model choice, `contextual.assemble` for context, and `PresentationDirectives.instruction()` for formatting, behind a flag | Not wired into chat yet, on purpose |
| Central audit (v2.9) | Graph, preference and evaluation changes are not yet in the central audit; this needs new audit categories (a change to the audit model and migration 0006) | Deferred; coordinate first |

**Possible merge conflicts** are small and textual:
- `jarvis/main.py` (router include and state);
- `jarvis/security/policy.py` (new capabilities);
- `pyproject.toml` (package data);
- the documentation sections.

Migrations are numbered `0052` and `0056` so they cannot collide with the other workstream's `0007`+.
