from contextlib import AbstractContextManager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError

from jarvis.core.config import Settings
from jarvis.persistence.base import ConversationMessage, ConversationNotFound, PersistenceError
from jarvis.persistence.postgres import PostgresConversationRepository
from jarvis.models.schemas import (
    CONVERSATION_LIST_MAX_LIMIT,
    CONVERSATION_LIST_MAX_OFFSET,
)

TEST_AUTH_TOKEN = "test-only-token-with-at-least-32-characters"
TEST_OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


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
        local_auth_token=SecretStr(TEST_AUTH_TOKEN),
        owner_id=TEST_OWNER_ID,
    ).persistence_enabled


def test_persistent_configuration_requires_strong_token_and_non_nil_owner():
    database = SecretStr("postgresql://jarvis@localhost/jarvis")
    with pytest.raises(ValidationError, match="JARVIS_LOCAL_AUTH_TOKEN"):
        Settings(_env_file=None, persistence_enabled=True, persistence_database_url=database)
    with pytest.raises(ValidationError, match="JARVIS_OWNER_ID"):
        Settings(
            _env_file=None, persistence_enabled=True, persistence_database_url=database,
            local_auth_token=SecretStr(TEST_AUTH_TOKEN),
        )
    for token in ("", "short", " " + TEST_AUTH_TOKEN, "token with embedded whitespace and length"):
        with pytest.raises(ValidationError, match="JARVIS_LOCAL_AUTH_TOKEN"):
            Settings(
                _env_file=None, persistence_enabled=True, persistence_database_url=database,
                local_auth_token=SecretStr(token), owner_id=TEST_OWNER_ID,
            )
    with pytest.raises(ValidationError, match="nil UUID"):
        Settings(
            _env_file=None, persistence_enabled=True, persistence_database_url=database,
            local_auth_token=SecretStr(TEST_AUTH_TOKEN), owner_id=UUID(int=0),
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, persistence_enabled=True, persistence_database_url=database,
            local_auth_token=SecretStr(TEST_AUTH_TOKEN), owner_id="not-a-uuid",
        )


@pytest.mark.parametrize("timeout", [0, 16])
def test_database_connect_timeout_is_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, persistence_connect_timeout_seconds=timeout)


def test_persistence_operation_timeout_rejects_values_outside_one_to_sixty_seconds():
    assert Settings(_env_file=None).persistence_operation_timeout_seconds == 5
    for timeout in (0, 61):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, persistence_operation_timeout_seconds=timeout)


@pytest.mark.parametrize("role", ["system", "tool", "provider"])
def test_message_contract_rejects_unsupported_roles(role):
    with pytest.raises(ValidationError):
        ConversationMessage(
            id=uuid4(), conversation_id=uuid4(), role=role, content="text",
            sequence_number=0,
            created_at=datetime.now(timezone.utc),
        )


def test_message_contract_rejects_blank_content():
    with pytest.raises(ValidationError):
        ConversationMessage(
            id=uuid4(), conversation_id=uuid4(), role="user", content="  \n",
            sequence_number=0,
            created_at=datetime.now(timezone.utc),
        )


class FakeConnection(AbstractContextManager):
    def __init__(
        self,
        rows=(),
        *,
        fail_on_call=None,
        next_sequence=0,
        conversation_exists=True,
        conversation_rows=(),
    ):
        self.calls = []
        self.rows = list(rows)
        self.message_rows = list(rows)
        self.conversation_rows = list(conversation_rows)
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
        if "FROM jarvis_app.conversations AS c" in query:
            requested_limit, offset = params
            self.rows = self.conversation_rows[offset : offset + requested_limit]
        elif "SELECT id FROM jarvis_app.conversations" in query:
            self.rows = [(params[0],)] if self.conversation_exists else []
        elif "RETURNING id, conversation_id, sequence_number, role, content, created_at" in query:
            self.rows = [(*params[:3], params[3], params[4], datetime.now(timezone.utc))]
        elif "SELECT id, conversation_id, sequence_number, role, content, created_at" in query:
            self.rows = (
                list(reversed(self.message_rows))
                if "ORDER BY sequence_number DESC" in query
                else self.message_rows
            )
        elif "SELECT COALESCE(MAX(sequence_number)" in query:
            self.rows = [(self.next_sequence,)]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_expired_conversation_operation_budget_is_rejected_before_connect(monkeypatch):
    import jarvis.persistence.postgres as postgres

    clock = iter((10.0, 11.0))
    monkeypatch.setattr(postgres, "monotonic", lambda: next(clock))
    connect_calls = []
    repository = PostgresConversationRepository(
        "private-url",
        operation_timeout_seconds=1,
        connect=lambda *args, **kwargs: connect_calls.append((args, kwargs)),
    )

    with pytest.raises(PersistenceError):
        repository.create_conversation()

    assert connect_calls == []


def test_expiry_mid_exchange_rolls_back_without_a_partial_message_pair(monkeypatch):
    import jarvis.persistence.postgres as postgres

    clock = iter((10.0, 10.0, 10.0, 10.0, 15.0))
    monkeypatch.setattr(postgres, "monotonic", lambda: next(clock))
    connection = FakeConnection()
    repository = PostgresConversationRepository(
        "private-url", operation_timeout_seconds=5,
        connect=lambda *args, **kwargs: connection,
    )

    with pytest.raises(PersistenceError):
        repository.save_exchange("private user content", "private assistant content")

    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    assert len(sql_calls) == 1 and "INSERT INTO jarvis_app.conversations" in sql_calls[0][0]
    assert connection.rolled_back and not connection.committed


def test_statement_timeout_is_positive_and_recalculated_for_each_statement():
    from jarvis.persistence.operation_timeout import execute_with_deadline

    clock = iter((10.0, 10.0, 11.0, 11.0))
    connection = FakeConnection()
    execute_with_deadline(connection, 15.0, "SELECT 1", clock=lambda: next(clock))
    execute_with_deadline(connection, 15.0, "SELECT 2", clock=lambda: next(clock))

    assert connection.calls == [
        ("SELECT set_config('statement_timeout', %s, true)", ("5000ms",)),
        ("SELECT 1", ()),
        ("SELECT set_config('statement_timeout', %s, true)", ("4000ms",)),
        ("SELECT 2", ()),
    ]


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
    timeout_calls = [item for item in connection.calls if "set_config('statement_timeout'" in item[0]]
    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    assert len(timeout_calls) == len(sql_calls) == 2
    assert all(int(params[0][:-2]) > 0 and params[0].endswith("ms") for _, params in timeout_calls)
    assert sql_calls[0][1] == (conversation_id,)
    message_query, parameters = sql_calls[1]
    assert "%s" in message_query and "sequence_number" in message_query
    assert parameters[1] == conversation_id and parameters[2] == 0 and parameters[3] == "question"
    assert parameters[5] == conversation_id and parameters[6] == 1 and parameters[7] == "answer"


def test_save_exchange_rolls_back_and_sanitizes_database_failure():
    connection = FakeConnection(fail_on_call=4)
    repository = PostgresConversationRepository("do-not-log-secret", connect=lambda _, **__: connection)

    with pytest.raises(PersistenceError) as error:
        repository.save_exchange("question", "answer")

    assert connection.rolled_back
    assert not connection.committed
    assert str(error.value) == "Conversation persistence is unavailable."
    assert "do-not-log-secret" not in str(error.value)
    assert "private database password" not in str(error.value)


def test_save_exchange_appends_to_existing_conversation_atomically():
    conversation_id = uuid4()
    connection = FakeConnection(next_sequence=2)
    repository = PostgresConversationRepository(
        "postgresql://secret", connect=lambda _, **__: connection
    )

    saved_id = repository.save_exchange("next question", "next answer", conversation_id)

    assert saved_id == conversation_id
    assert connection.committed
    assert "INSERT INTO jarvis_app.conversations" not in " ".join(
        query for query, _ in connection.calls
    )
    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    insert_query, params = sql_calls[2]
    assert "sequence_number" in insert_query
    assert params[1:4] == (conversation_id, 2, "next question")
    assert params[5:8] == (conversation_id, 3, "next answer")


def test_save_exchange_unknown_conversation_does_not_create_replacement():
    conversation_id = uuid4()
    connection = FakeConnection(conversation_exists=False)
    repository = PostgresConversationRepository(
        "postgresql://secret", connect=lambda _, **__: connection
    )

    with pytest.raises(ConversationNotFound):
        repository.save_exchange("question", "answer", conversation_id)

    assert connection.rolled_back
    assert all("INSERT INTO jarvis_app.conversations" not in query for query, _ in connection.calls)


def test_message_retrieval_rejects_unknown_conversation():
    connection = FakeConnection(conversation_exists=False)
    repository = PostgresConversationRepository(
        "postgresql://secret", connect=lambda _, **__: connection
    )
    with pytest.raises(ConversationNotFound):
        repository.get_messages(uuid4())


def test_message_retrieval_uses_stable_insertion_order():
    conversation_id = uuid4()
    earlier_id, later_id = uuid4(), uuid4()
    earlier = (earlier_id, conversation_id, 0, "user", "first", datetime(2026, 1, 1, tzinfo=timezone.utc))
    later = (later_id, conversation_id, 1, "assistant", "second", datetime(2026, 1, 2, tzinfo=timezone.utc))
    connection = FakeConnection((earlier, later))
    repository = PostgresConversationRepository("postgresql://secret", connect=lambda _, **__: connection)

    messages = repository.get_messages(conversation_id)

    assert [message.content for message in messages] == ["first", "second"]
    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    query, params = sql_calls[1]
    assert "WHERE conversation_id = %s" in query
    assert "ORDER BY sequence_number DESC" in query
    assert params == (conversation_id, 51)
    assert [message.sequence_number for message in messages] == [0, 1]


def test_message_retrieval_cursor_filters_to_older_sequence_numbers():
    conversation_id = uuid4()
    connection = FakeConnection()
    repository = PostgresConversationRepository(
        "postgresql://secret", connect=lambda _, **__: connection
    )

    assert repository.get_messages(conversation_id, before_sequence=9, limit=21) == ()

    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    query, params = sql_calls[1]
    assert "sequence_number < %s" in query
    assert params == (conversation_id, 9, 21)


def test_conversation_listing_summarizes_empty_and_populated_conversations():
    conversation_id = UUID("00000000-0000-0000-0000-000000000001")
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_activity = datetime(2026, 1, 2, tzinfo=timezone.utc)
    empty_id = UUID("00000000-0000-0000-0000-000000000002")
    connection = FakeConnection(conversation_rows=[
        (conversation_id, created_at, last_activity, "first user message"),
        (empty_id, created_at, created_at, None),
    ])
    repository = PostgresConversationRepository(
        "private-url", connect=lambda *_args, **_kwargs: connection
    )

    page = repository.list_conversations(limit=2)

    assert [item.conversation_id for item in page.items] == [conversation_id, empty_id]
    assert page.items[0].preview == "first user message"
    assert page.items[1].preview is None
    assert page.items[1].last_activity_at == created_at
    assert page.limit == 2 and page.offset == 0 and not page.has_more
    sql_calls = [call for call in connection.calls if "set_config('statement_timeout'" not in call[0]]
    query, params = sql_calls[0]
    assert params == (3, 0)
    assert "LEFT JOIN LATERAL" in query
    assert "m.role = 'user'" in query
    assert "ORDER BY m.sequence_number ASC, m.id ASC" in query
    assert "ORDER BY last_activity_at DESC, c.id DESC" in query
    assert "LEFT(m.content, 121)" in query


def test_conversation_listing_truncates_preview_by_unicode_codepoint():
    conversation_id = uuid4()
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    exact_preview = "é" * 120
    overlong_preview = "猫" * 121
    connection = FakeConnection(conversation_rows=[
        (conversation_id, timestamp, timestamp, exact_preview),
        (uuid4(), timestamp, timestamp, overlong_preview),
    ])
    repository = PostgresConversationRepository(
        "private-url", connect=lambda *_args, **_kwargs: connection
    )

    page = repository.list_conversations(limit=2)

    assert page.items[0].preview == exact_preview
    assert page.items[1].preview == ("猫" * 119) + "…"
    assert len(page.items[1].preview) == 120


def test_conversation_listing_uses_offset_and_reports_has_more():
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        (UUID(int=4 - index), timestamp, timestamp, f"preview {index}")
        for index in range(4)
    ]
    connection = FakeConnection(conversation_rows=rows)
    repository = PostgresConversationRepository(
        "private-url", connect=lambda *_args, **_kwargs: connection
    )

    page = repository.list_conversations(limit=2, offset=1)

    assert [item.preview for item in page.items] == ["preview 1", "preview 2"]
    assert page.has_more is True
    assert len(page.items) <= page.limit
    sql_calls = [call for call in connection.calls if "set_config('statement_timeout'" not in call[0]]
    assert sql_calls[0][1] == (3, 1)
    assert "ORDER BY last_activity_at DESC, c.id DESC" in sql_calls[0][0]


@pytest.mark.parametrize(
    ("limit", "offset"),
    [
        (0, 0),
        (CONVERSATION_LIST_MAX_LIMIT + 1, 0),
        (True, 0),
        (1, -1),
        (1, CONVERSATION_LIST_MAX_OFFSET + 1),
        (1, False),
    ],
)
def test_conversation_listing_rejects_invalid_bounds_before_connect(limit, offset):
    connect_calls = []
    repository = PostgresConversationRepository(
        "private-url",
        connect=lambda *args, **kwargs: connect_calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError):
        repository.list_conversations(limit=limit, offset=offset)

    assert connect_calls == []


def test_conversation_listing_sanitizes_persistence_failures():
    connection = FakeConnection(fail_on_call=2)
    repository = PostgresConversationRepository(
        "private database URL", connect=lambda *_args, **_kwargs: connection
    )

    with pytest.raises(PersistenceError) as error:
        repository.list_conversations()

    assert str(error.value) == "Conversation persistence is unavailable."
    assert "private database URL" not in str(error.value)
    assert "private database password" not in str(error.value)
    assert connection.rolled_back and not connection.committed


def test_append_message_assigns_next_conversation_sequence_and_checks_parent():
    conversation_id = uuid4()
    connection = FakeConnection(next_sequence=2)
    repository = PostgresConversationRepository("postgresql://secret", connect=lambda _, **__: connection)

    message = repository.append_message(conversation_id, "assistant", "third")

    assert message.conversation_id == conversation_id
    assert message.role == "assistant"
    assert message.content == "third"
    sql_calls = [item for item in connection.calls if "set_config('statement_timeout'" not in item[0]]
    insert_query, params = sql_calls[2]
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
