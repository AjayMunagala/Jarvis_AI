# Controlled Self-Improvement & Hardening (v3.9)

v3.9 introduces a strictly governed prompt optimization and self-improvement architecture with automated unified diffs, immutable security boundary enforcement, offline benchmark suites, owner approval gating, and 1-click snapshot rollback.

---

## Architectural Principles & Invariants

### 1. Proposal Gating & Review Workflow
Self-improvement processes (optimizers, automated prompt tuners) are strictly segregated from mutating production prompts or code directly:
1. **Proposal Creation**: Proposed mutations are submitted as `PromptMutationProposal` objects via `/improvement/proposals`.
2. **Automated Unified Diff Generation**: Standard `difflib.unified_diff` is generated comparing the target prompt's baseline with the proposed changes.
3. **Owner Review & Approval**: Only owners with `IMPROVEMENT_APPROVE` capability can decide on proposals (`approved` or `rejected`).
4. **Snapshot Capture**: Upon approval, a full pre-mutation snapshot is created and associated with `snapshot_id`.
5. **1-Click Rollback**: Any approved proposal can be rolled back to its baseline at any time with a single API call (`/improvement/proposals/{id}/restore`).

### 2. Immutable Security Boundary Enforcement
To protect the core security perimeter against self-modification, the system enforces hard-coded immutable security paths (`jarvis/improvement/immutable.py`):
- Any attempt to target paths starting with or containing:
  - `jarvis/security` (Authorization policies, token validation, capability checks)
  - `jarvis/verification` (Evidence verification, patch validators, audit engines)
  - `database/scripts` (SQL migrations)
  - `pyproject.toml` (Dependency manifests)
  - `.env` / configuration files
  - `jarvis/core/config.py` (Core system settings)
  - Path traversal sequences (`..`)
- Is immediately rejected with `ImmutableSecurityFileError` and HTTP 422 Unprocessable Content.

### 3. Offline Benchmark Sandboxing
Before applying prompt adjustments, offline benchmarks evaluate candidate performance without network calls (`jarvis/improvement/sandbox.py`):
- **Instruction Adherence**: Validates that prompt responses adhere strictly to defined response formats, schemas, and constraints.
- **Security Boundary Preservation**: Tests adversarial prompts against jailbreaks, unauthorized capability escalations, and system prompt exfiltration attempts.
- **Token Budget Compliance**: Verifies that generated prompts remain bounded within conservative token consumption limits.

---

## Capabilities & RBAC

| Capability | Role | Enforced Endpoints |
| :--- | :--- | :--- |
| `IMPROVEMENT_READ` | Read access | `GET /improvement/proposals`, `GET /improvement/proposals/{id}`, `POST /improvement/benchmarks/run` |
| `IMPROVEMENT_PROPOSE` | Mutation proposal | `POST /improvement/proposals` |
| `IMPROVEMENT_APPROVE` | Approval & Rollback | `POST /improvement/proposals/{id}/decide`, `POST /improvement/proposals/{id}/restore` |

---

## Data Models & Endpoints

### Models
- `PromptMutationProposal`: ID, owner, target_name, original_content, proposed_content, diff, rationale, status (`pending`, `approved`, `rejected`, `restored`), snapshot_id, timestamps.
- `ProposalCreateRequest`: target_name, proposed_content, rationale.
- `ProposalDecisionRequest`: decision (`approved` | `rejected`), comment.
- `BenchmarkSuiteResult`: suite_id, total_benchmarks, passed_benchmarks, all_passed, metrics list, evaluated_at.

### API Endpoints
- `POST /improvement/proposals` (201 Created): Submit mutation proposal and generate diff.
- `GET /improvement/proposals` (200 OK): Paginated list of proposals.
- `GET /improvement/proposals/{id}` (200 OK): Retrieve proposal details and diff.
- `POST /improvement/proposals/{id}/decide` (200 OK): Approve or reject proposal. Applies change upon approval and takes snapshot.
- `POST /improvement/proposals/{id}/restore` (200 OK): 1-click restore from snapshot.
- `POST /improvement/benchmarks/run` (200 OK): Run offline benchmark evaluation suite.

---

## Verification & Test Results
- `tests/test_improvement.py`: 5 passed
  - `test_immutable_security_file_protection`
  - `test_unified_diff_generation`
  - `test_offline_benchmarks_evaluation`
  - `test_improvement_service_proposal_lifecycle_and_rejection`
  - `test_approval_and_one_click_snapshot_restore`
- `tests/test_improvement_api.py`: 6 passed
  - `test_unauthenticated_requests_fail` (401)
  - `test_capability_enforcement` (403)
  - `test_immutable_security_path_rejection_api` (422)
  - `test_proposal_lifecycle_and_restore_api` (201, 200, snapshot, restore)
  - `test_benchmark_evaluation_api` (200)
  - `test_nonexistent_proposal_returns_404` (404)
