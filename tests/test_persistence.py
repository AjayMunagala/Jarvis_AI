from contextlib import AbstractContextManager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError

from jarvis.core.config import Settings
from jarvis.persistence.base import ConversationMessage, PersistenceError
from jarvis.persistence.postgres import PostgresConversationRepository


def test_persistence_is_disabled_by_default_and_needs_no_database():
    settings = Settings(_env_file=None)
    assert settings.persistence_enabled is False
    assert settings.persistence_database_url is None


def test_enabled_persistence_requires_a_postgresql_url():
    with pytest.raises(ValidationError, match="JARVIS_PERSISTENCE_DATABASE_URL is required"):
        Settings(_env_file=None, persistence_enabled=True)
    with pytest.raises(ValidationError, match="must include a PostgreSQL host and database name"):
        Settings(
            _env_file=None,
            persistence_enabled=True,
            persistence_database_url=SecretStr("https://user:password@example.test/db"),
        )
    with pytest.raises(ValidationError, match="must include a PostgreSQL host and database name"):
        Settings(
            _env_file=None,
            persistence_enabled=True,
            persistence_database_url=SecretStr("postgresql://"),
        )
    assert Settings(
        _env_file=None,
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://jarvis:password@localhost/jarvis"),
    ).persistence_enabled


@pytest.mark.parametrize("timeout", [0, 16])
def test_database_connect_timeout_is_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, persistence_connect_timeout_seconds=timeout)


@pytest.mark.parametrize("role", ["system", "tool", "provider"])
def test_message_contract_rejects_unsupported_roles(role):
    with pytest.raises(ValidationError):
        ConversationMessage(
            id=uuid4(), conversation_id=uuid4(), role=role, content="text",
            created_at=datetime.now(timezone.utc),
        )


def test_message_contract_rejects_blank_content():
    with pytest.raises(ValidationError):
        ConversationMessage(
            id=uuid4(), conversation_id=uuid4(), role="user", content="  \n",
            created_at=datetime.now(timezone.utc),
        )


class FakeConnection(AbstractContextManager):
    def __init__(self, rows=(), *, fail_on_call=None, next_sequence=0, conversation_exists=True):
        self.calls = []
        self.rows = list(rows)
        self.fail_on_call = fail_on_call
        self.next_sequence = next_sequence
        self.conversation_exists = conversation_exists
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def execute(self, query, params):
        self.calls.append((query, params))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("private database password and provider payload")
        if "SELECT id FROM jarvis_app.conversations" in query:
            self.rows = [(params[0],)] if self.conversation_exists else []
        elif "SELECT COALESCE(MAX(sequence_number)" in query:
            self.rows = [(self.next_sequence,)]
        elif "RETURNING id, conversation_id, role, content, created_at" in query:
            self.rows = [(*params[:2], params[3], params[4], datetime.now(timezone.utc))]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_save_exchange_is_transactional_and_parameterized():
    connection = FakeConnection()
    supplied_urls = []

    def connect(url, **kwargs):
        supplied_urls.append((url, kwargs))
        return connection

    repository = PostgresConversationRepository("postgresql://secret", connect=connect)
    conversation_id = repository.save_exchange("question", "answer")

    assert isinstance(conversation_id, UUID)
    assert connection.committed
    assert not connection.rolled_back
    assert supplied_urls == [("postgresql://secret", {"connect_timeout": 3})]
    assert len(connection.calls) == 2
    assert connection.calls[0][1] == (conversation_id,)
    message_query, parameters = connection.calls[1]
    assert "%s" in message_query and "sequence_number" in message_query
    assert parameters[1] == conversation_id and parameters[2] == "question"
    assert parameters[4] == conversation_id and parameters[5] == "answer"


def test_save_exchange_rolls_back_and_sanitizes_database_failure():
    connection = FakeConnection(fail_on_call=2)
    repository = PostgresConversationRepository("do-not-log-secret", connect=lambda _, **__: connection)

    with pytest.raises(PersistenceError) as error:
        repository.save_exchange("question", "answer")

    assert connection.rolled_back
    assert not connection.committed
    assert str(error.value) == "Conversation persistence is unavailable."
    assert "do-not-log-secret" not in str(error.value)
    assert "private database password" not in str(error.value)


def test_message_retrieval_uses_stable_insertion_order():
    conversation_id = uuid4()
    earlier_id, later_id = uuid4(), uuid4()
    earlier = (earlier_id, conversation_id, "user", "first", datetime(2026, 1, 1, tzinfo=timezone.utc))
    later = (later_id, conversation_id, "assistant", "second", datetime(2026, 1, 2, tzinfo=timezone.utc))
    connection = FakeConnection((earlier, later))
    repository = PostgresConversationRepository("postgresql://secret", connect=lambda _, **__: connection)

    messages = repository.get_messages(conversation_id)

    assert [message.content for message in messages] == ["first", "second"]
    query, params = connection.calls[0]
    assert "ORDER BY sequence_number, id" in query
    assert params == (conversation_id,)


def test_append_message_assigns_next_conversation_sequence_and_checks_parent():
    conversation_id = uuid4()
    connection = FakeConnection(next_sequence=2)
    repository = PostgresConversationRepository("postgresql://secret", connect=lambda _, **__: connection)

    message = repository.append_message(conversation_id, "assistant", "third")

    assert message.conversation_id == conversation_id
    assert message.role == "assistant"
    assert message.content == "third"
    insert_query, params = connection.calls[2]
    assert "sequence_number" in insert_query
    assert params[2] == 2
    assert connection.committed

    missing_parent = FakeConnection(conversation_exists=False)
    missing_repository = PostgresConversationRepository(
        "postgresql://secret", connect=lambda _, **__: missing_parent
    )
    with pytest.raises(PersistenceError):
        missing_repository.append_message(conversation_id, "user", "orphan")
    assert missing_parent.rolled_back


def test_schema_migration_defines_roles_foreign_key_and_order_index():
    from pathlib import Path

    migration = (Path(__file__).parents[1] / "database/scripts/0002_conversation_persistence.sql").read_text()
    normalized = " ".join(migration.lower().split())
    assert "role in ('user', 'assistant')" in normalized
    assert "references jarvis_app.conversations(id) on delete cascade" in normalized
    assert "messages_conversation_order_idx" in normalized
    assert "sequence_number integer not null check (sequence_number >= 0)" in normalized
    assert "create unique index messages_conversation_order_idx" in normalized
