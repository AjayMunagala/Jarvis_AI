# Proactive Assistance & Scheduling (v3.5)

v3.5 introduces an in-process background scheduler, anti-fatigue notification throttling, resource-bounded execution, and user notification acknowledgement.

## Architecture & Operational Bounds

### 1. In-Process Scheduler Engine
- Manages recurring and one-shot tasks (`interval`, `cron`, `once`).
- Enforces strict execution safety boundaries:
  - **Maximum 2 concurrent running jobs**: Prevents resource starvation and runaway threads across the runtime. Additional execution attempts receive `HTTP 429 Too Many Requests`.
  - **Maximum 60-second execution ceiling**: Guarantees termination and prevents stalled background tasks.
  - Updates job lifecycle states monotonically (`idle` -> `running` -> `success` / `failure`) and tracks execution timing.

### 2. Anti-Fatigue Notification Throttling
Protects the user from proactive cognitive overload:
- Enforces a **15-minute quiet period** (900 seconds) per notification category for `low` and `normal` priority notifications.
- Suppressed notifications return `HTTP 429 Too Many Requests` without side effects.
- High-priority and urgent notifications (`high`, `urgent`) bypass the quiet period immediately.

### 3. Audit Accountability
- Job creations, updates, deletions, and executions record audit events in `jarvis_app.audit_events` with categories and structured execution metrics.
- Notification dispatches and acknowledgements are similarly audited.

---

## Database Migration (`0011_scheduler.sql`)

- `jarvis_app.scheduled_jobs`: `id` (UUID PK), `owner_id` (UUID), `name` (VARCHAR 200), `schedule_type` (VARCHAR 20), `schedule_expr` (VARCHAR 100), `action_type` (VARCHAR 50), `payload` (JSONB), `enabled` (BOOLEAN), `next_run_at` (TIMESTAMPTZ), `last_run_at` (TIMESTAMPTZ), `last_status` (VARCHAR 20), `created_at` (TIMESTAMPTZ), `updated_at` (TIMESTAMPTZ).
  - Index: `idx_scheduled_jobs_owner_next` on `(owner_id, enabled, next_run_at)`.
- `jarvis_app.notifications`: `id` (UUID PK), `owner_id` (UUID), `category` (VARCHAR 50), `title` (VARCHAR 300), `message` (TEXT), `priority` (VARCHAR 20), `read` (BOOLEAN), `delivered_at` (TIMESTAMPTZ), `created_at` (TIMESTAMPTZ).
  - Index: `idx_notifications_owner_read` on `(owner_id, read, created_at DESC)`.

---

## API Endpoints

All endpoints are mounted under `/scheduler`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/scheduler/jobs` | `POST` | `SCHEDULER_WRITE` | Register scheduled job |
| `/scheduler/jobs` | `GET` | `SCHEDULER_READ` | List scheduled jobs with pagination |
| `/scheduler/jobs/{id}` | `GET` | `SCHEDULER_READ` | Retrieve specific scheduled job |
| `/scheduler/jobs/{id}` | `PATCH` | `SCHEDULER_WRITE` | Update scheduled job parameters |
| `/scheduler/jobs/{id}` | `DELETE` | `SCHEDULER_WRITE` | Remove scheduled job |
| `/scheduler/jobs/{id}/run` | `POST` | `SCHEDULER_WRITE` | Trigger immediate job run (max 2 concurrent) |
| `/scheduler/notifications` | `POST` | `SCHEDULER_WRITE` | Send notification with anti-fatigue gating |
| `/scheduler/notifications` | `GET` | `SCHEDULER_READ` | List notifications (optional `unread_only` filter) |
| `/scheduler/notifications/{id}` | `GET` | `SCHEDULER_READ` | Retrieve specific notification |
| `/scheduler/notifications/{id}/read` | `PATCH` | `SCHEDULER_WRITE` | Mark notification as read |

---

## Verification & Test Coverage

- `tests/test_scheduler.py`: Unit tests for `calculate_next_run`, anti-fatigue quiet window enforcement and priority bypass, memory store operations, concurrency limit enforcement (`MaxConcurrencyError`), and notification read transitions.
- `tests/test_scheduler_api.py`: FastAPI TestClient integration tests verifying authentication, capability checks (401/403), job CRUD and execution, notification dispatch throttling (429), and unread filtering.
