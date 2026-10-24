"""Tests for Audit Category Integration (Migration 0059).

Validates:
1. All five audit categories ('tool', 'browser', 'approval', 'intelligence', 'orchestration') are accepted.
2. Unknown / unsupported categories are strictly rejected by the model.
3. In-memory and PostgreSQL stores correctly persist and query events across all five categories.
4. Non-durable status is reported accurately when running in-memory (durable=False), and durable=True for PostgreSQL.
5. Migration 0059 is discovered, checksummed, and contains the expected CHECK constraint.
"""

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from database import runner
from jarvis.audit.models import AuditCategory, AuditRecord
from jarvis.audit.service import AuditService
from jarvis.audit.stores import MemoryAuditStore, PostgresAuditStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ALL_CATEGORIES: list[AuditCategory] = [
    "tool",
    "browser",
    "approval",
    "intelligence",
    "orchestration",
]


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.log.append((" ".join(query.split()), params))
        return FakeCursor(self.rows if "set_config" not in query else [])


def fake_connect(log, rows=()):
    def connect(url, connect_timeout):
        log.append(("connect", connect_timeout))
        return FakeConnection(log, list(rows))
    return connect


def test_all_five_categories_are_accepted_by_model():
    now_str = datetime.now(timezone.utc).isoformat()
    for cat in ALL_CATEGORIES:
        record = AuditRecord(
            event_id="AE-0123456789abcdef",
            owner_id=OWNER,
            occurred_at=now_str,
            category=cat,
            event_type="test.event",
            decision="allow",
            status="success",
            subject="test_subject",
            correlation_id=None,
            details={"cat": cat},
            durable=False,
        )
        assert record.category == cat


def test_unknown_category_is_rejected_by_model():
    now_str = datetime.now(timezone.utc).isoformat()
    for bad_cat in ["unknown", "shell", "magic", ""]:
        with pytest.raises(ValidationError):
            AuditRecord(
                event_id="AE-0123456789abcdef",
                owner_id=OWNER,
                occurred_at=now_str,
                category=bad_cat,  # type: ignore[arg-type]
                event_type="test.event",
                decision="allow",
                status="success",
                subject="test_subject",
                correlation_id=None,
                details={},
                durable=False,
            )



def test_memory_store_persists_and_queries_all_five_categories():
    store = MemoryAuditStore()
    assert store.durable is False

    service = AuditService(store)
    now = datetime.now(timezone.utc)

    for cat in ALL_CATEGORIES:
        service.record(
            owner_id=OWNER,
            category=cat,
            event_type=f"{cat}.event",
            decision="allow",
            status="success",
            subject=f"subject_{cat}",
            details={"step": cat},
        )

    # Verify total events recorded
    page_all = service.page(OWNER, limit=10)
    assert page_all.durable_storage is False
    assert len(page_all.events) == 5

    # Verify querying by specific category works for all 5
    for cat in ALL_CATEGORIES:
        page_cat = service.page(OWNER, category=cat, limit=10)
        assert len(page_cat.events) == 1
        assert page_cat.events[0].category == cat
        assert page_cat.events[0].durable is False


def test_postgres_store_persists_and_queries_all_five_categories():
    log = []
    now = datetime.now(timezone.utc)

    rows = [
        (
            f"AE-{i:016d}",
            OWNER,
            now,
            cat,
            f"{cat}.event",
            "allow",
            "success",
            f"subject_{cat}",
            None,
            {"step": cat},
        )
        for i, cat in enumerate(ALL_CATEGORIES)
    ]

    store = PostgresAuditStore("postgresql://x", connect=fake_connect(log, rows))
    assert store.durable is True

    service = AuditService(store)

    # Append events across all 5 categories
    for cat in ALL_CATEGORIES:
        record = service.record(
            owner_id=OWNER,
            category=cat,
            event_type=f"{cat}.event",
            decision="allow",
            status="success",
            subject=f"subject_{cat}",
            details={"step": cat},
        )
        assert record.durable is True

    # Check insert statements
    inserts = [e for e in log if e[0].startswith("INSERT INTO jarvis_app.audit_events")]
    assert len(inserts) == 5
    for insert, cat in zip(inserts, ALL_CATEGORIES):
        params = insert[1]
        assert params[1] == OWNER
        assert params[3] == cat  # category column

    # Query with specific category filter
    page = service.page(OWNER, category="orchestration", limit=10)
    assert page.durable_storage is True
    selects = [e for e in log if e[0].startswith("SELECT id, owner_id")]
    assert any("category = %s" in s[0] and "orchestration" in s[1] for s in selects)


def test_migration_0059_is_discovered_and_expands_categories():
    files = {
        migration_id: checksum
        for migration_id, _, _, checksum in runner._migration_files(runner.MIGRATIONS_DIR)
    }
    assert "0059_audit_orchestration_category" in files
    assert len(files["0059_audit_orchestration_category"]) == 64

    sql = (runner.MIGRATIONS_DIR / "0059_audit_orchestration_category.sql").read_text(
        encoding="utf-8"
    )
    assert "audit_events_category_allowed" in sql
    for cat in ALL_CATEGORIES:
        assert f"'{cat}'" in sql
