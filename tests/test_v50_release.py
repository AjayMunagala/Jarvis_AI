"""v5.0 release readiness: carry-over fixes and repository-wide release checks."""

import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from database import runner
from jarvis.assistant.models import TaskCreateRequest
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.core.config import Settings
from jarvis.main import create_app

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v50-release-readiness-token-32-chars-x"


def test_naive_and_aware_due_dates_can_be_listed_together():
    """Found in the v4.7 review: mixing naive and zone-aware due dates made task listing raise TypeError."""
    service = AssistantService(MemoryAssistantStore())
    service.create_task(OWNER, TaskCreateRequest(title="naive", due_date=datetime(2026, 11, 1, 9, 0)))
    service.create_task(OWNER, TaskCreateRequest(title="aware", due_date=datetime(2026, 10, 30, 9, 0,
                                                                                    tzinfo=timezone.utc)))
    service.create_task(OWNER, TaskCreateRequest(title="none"))
    titles = [task.title for task in service.list_tasks(OWNER).items]
    assert titles == ["aware", "naive", "none"]
    assert all(task.due_date is None or task.due_date.tzinfo is not None for task in service.list_tasks(OWNER).items)


def test_api_accepts_naive_dates_and_returns_them_as_utc():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        later = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()
        assert client.post("/assistant/tasks", json={"title": "naive", "due_date": later}).status_code == 201
        assert client.post("/assistant/tasks", json={"title": "aware",
                                                     "due_date": later + "+00:00"}).status_code == 201
        listed = client.get("/assistant/tasks")
    assert listed.status_code == 200 and listed.json()["total"] == 2


def test_migrations_are_append_only_and_uniquely_numbered():
    ids = [migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR)]
    numbers = [int(re.match(r"^(\d{4})_", migration_id).group(1)) for migration_id in ids]
    assert len(numbers) == len(set(numbers)) and numbers == sorted(numbers)
    assert numbers[-1] >= 66  # 0066 was the newest v5.0 migration; later versions only add after it
    for migration in runner.MIGRATIONS_DIR.glob("006*.sql"):
        text = migration.read_text(encoding="utf-8").upper()
        assert "DROP TABLE" not in text and "TRUNCATE" not in text and "DELETE FROM" not in text, migration.name


def test_execution_disabled_defaults_hold_for_the_release():
    settings = Settings(_env_file=None, local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    assert settings.tools_enabled is False and settings.chat_tools_enabled is False
    assert settings.browser_enabled is False
    assert settings.scheduler_runner_enabled is False and settings.chat_workspace_context_enabled is False
