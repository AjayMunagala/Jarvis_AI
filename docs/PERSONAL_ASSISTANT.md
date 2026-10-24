# Personal Assistant Foundation (v3.1)

v3.1 introduces the foundational personal assistant capabilities for Project JARVIS: structured tasks, personal notes, scheduled reminders, and a local calendar abstraction.

## Core Principles

1. **Owner Isolation & Security First**: All assistant resources are scoped to `owner_id`. Access requires an authenticated session and server-side capability enforcement (`Capability.ASSISTANT_READ` and `Capability.ASSISTANT_WRITE`).
2. **Dual-Store Architecture**: Non-durable in-memory store (`MemoryAssistantStore`) by default; durable PostgreSQL store (`PostgresAssistantStore`) when persistence is configured. Parameterized queries and bounded statement timeouts are strictly enforced.
3. **Audit Trail Accountability**: Mutating assistant operations (create, update, delete) emit structured audit events through `AuditService` with `category="tool"` and subjects such as `assistant:task:<id>`, ensuring conformance with database constraints.
4. **Local-Only Calendar Abstraction**: The `CalendarProvider` protocol and `LocalCalendarProvider` explicitly disclaim external calendar synchronization in v3.1. All calendar operations remain isolated and local.
5. **Read-Only AI Orchestration Tools**: Provides model inspection capabilities via `tasks.list`, `notes.search`, and `calendar.list` without exposing autonomous mutating tool side effects.

---

## Domain Models

Domain models are strictly validated Pydantic models with `frozen=True` and `extra="forbid"`:

- **`Task`**: Title, optional description, priority (`low`, `medium`, `high`, `urgent`), status (`pending`, `in_progress`, `completed`, `cancelled`), due date, normalized tags (lowercased, deduplicated).
- **`Note`**: Title, content (bounded up to 50,000 characters), normalized tags.
- **`Reminder`**: Title, `remind_at` timestamp, optional associated `task_id`, status (`pending`, `triggered`, `acknowledged`, `dismissed`).
- **`CalendarEvent`**: Title, description, start time, end time, location. Validates that `end_time >= start_time`.

---

## Database Migration

Migration `database/scripts/0007_personal_assistant.sql` introduces:
- `jarvis_app.tasks`
- `jarvis_app.notes`
- `jarvis_app.reminders`
- `jarvis_app.calendar_events`

Each table enforces:
- `owner_id UUID NOT NULL` with foreign key to `jarvis_app.users(id) ON DELETE CASCADE`.
- Indexing on `owner_id`, status, and time range columns.
- Text search indexes (`gin(to_tsvector('english', title || ' ' || content))`) for notes.

---

## REST API Endpoints

All routes reside under prefix `/assistant` and require authentication:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/assistant/tasks` | `POST` | `ASSISTANT_WRITE` | Create a new task (emits audit) |
| `/assistant/tasks` | `GET` | `ASSISTANT_READ` | List tasks with filters (`status`, `priority`, `tag`) and pagination |
| `/assistant/tasks/{id}` | `GET` | `ASSISTANT_READ` | Retrieve specific task by ID |
| `/assistant/tasks/{id}` | `PATCH` | `ASSISTANT_WRITE` | Update task status or fields |
| `/assistant/tasks/{id}` | `DELETE` | `ASSISTANT_WRITE` | Delete task |
| `/assistant/notes` | `POST` | `ASSISTANT_WRITE` | Create a new note |
| `/assistant/notes` | `GET` | `ASSISTANT_READ` | List or search notes (`query`, `tag`) |
| `/assistant/notes/{id}` | `GET` | `ASSISTANT_READ` | Retrieve note by ID |
| `/assistant/notes/{id}` | `PATCH` | `ASSISTANT_WRITE` | Update note content or title |
| `/assistant/notes/{id}` | `DELETE` | `ASSISTANT_WRITE` | Delete note |
| `/assistant/reminders` | `POST` | `ASSISTANT_WRITE` | Schedule a reminder |
| `/assistant/reminders` | `GET` | `ASSISTANT_READ` | List reminders by status |
| `/assistant/reminders/{id}` | `PATCH` | `ASSISTANT_WRITE` | Update reminder status (e.g. dismissed) |
| `/assistant/reminders/{id}` | `DELETE` | `ASSISTANT_WRITE` | Delete reminder |
| `/assistant/calendar/events` | `POST` | `ASSISTANT_WRITE` | Schedule local calendar event |
| `/assistant/calendar/events` | `GET` | `ASSISTANT_READ` | Query events in time window (`start_range`, `end_range`) |
| `/assistant/calendar/events/{id}` | `GET` | `ASSISTANT_READ` | Retrieve event details |
| `/assistant/calendar/events/{id}` | `PATCH` | `ASSISTANT_WRITE` | Update event timing or location |
| `/assistant/calendar/events/{id}` | `DELETE` | `ASSISTANT_WRITE` | Delete event |

---

## Read-Only Orchestration Tools

The following tools are exposed to the AI orchestration runtime with explicit schemas in `tools.json`:

1. `tasks.list`: Lists tasks with optional status/priority filtering.
2. `notes.search`: Searches notes by textual query or tag.
3. `calendar.list`: Lists events within a specified ISO-8601 date range.

---

## Frontend Integration

The web frontend includes:
- **`AssistantPanel`** (`frontend/src/components/AssistantPanel.tsx` and `.css`): A tabbed interface for managing Tasks, Notes, Reminders, and Calendar.
- **Top-level Navigation**: An "Assistant" tab in `App.tsx` with dedicated icon.
- **Explicit Disclaimers**: Highlights that calendar events are strictly local to JARVIS.
- **Session Security**: Respects workspace lock/unlock states.

---

## Verification & Test Coverage

- `tests/test_assistant.py`: Unit tests for domain models, validation constraints, `MemoryAssistantStore`, `LocalCalendarProvider`, and `AssistantService` audit emissions.
- `tests/test_assistant_api.py`: Integration tests for all `/assistant/*` routes, bearer authentication, 401/403 capability enforcement, and 404/422 validations.
- `frontend/src/components/AssistantPanel.test.tsx`: Vitest tests for the tab navigation, task completion, note searching, reminder dismissal, and calendar disclaimers.
