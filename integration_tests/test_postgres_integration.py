"""Opt-in PostgreSQL integration tests using an isolated local database."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from database import runner
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.persistence.base import ConversationNotFound, PersistenceError
from jarvis.persistence.postgres import PostgresConversationRepository


RUN_INTEGRATION = "JARVIS_RUN_POSTGRES_INTEGRATION"
ADMIN_URL = "JARVIS_TEST_POSTGRES_ADMIN_URL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GENERATED_DATABASE_RE = re.compile(r"^jarvis_it_[0-9a-f]{32}$")


def parse_safe_admin_url(value: str):
    """Accept only a password-free loopback URL to the maintenance database."""
    try:
        parsed = urlsplit(value)
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("The PostgreSQL integration admin URL is malformed.") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or (parsed.hostname or "").lower() not in LOCAL_HOSTS
        or parsed.path != "/postgres"
        or not parsed.username
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError(
            "Use a password-free PostgreSQL URL for the local postgres maintenance database; "
            "credentials must come from PGPASSFILE."
        )
    return parsed, port


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://tester@db.example.com:5432/postgres",
        "postgresql://tester@localhost:5432/jarvis",
        "postgresql://tester:secret@localhost:5432/postgres",
        "https://tester@localhost:5432/postgres",
        "postgresql://tester@localhost:5432/postgres?sslmode=require",
    ],
)
def test_integration_guard_rejects_nonlocal_or_credential_urls(url):
    with pytest.raises(ValueError):
        parse_safe_admin_url(url)


@dataclass(frozen=True)
class IsolatedPostgres:
    admin_url: str
    database_url: str
    database_name: str
    migration_config: runner.DatabaseConfig

    def repository(self) -> PostgresConversationRepository:
        return PostgresConversationRepository(self.database_url, connect_timeout_seconds=3)

    def client(self) -> TestClient:
        settings = Settings(
            _env_file=None,
            environment="test",
            persistence_enabled=True,
            persistence_database_url=SecretStr(self.database_url),
            persistence_connect_timeout_seconds=3,
        )
        return TestClient(create_app(settings=settings))


@pytest.fixture(scope="session")
def isolated_postgres():
    if os.environ.get(RUN_INTEGRATION) != "1":
        pytest.skip(f"Opt-in only; set {RUN_INTEGRATION}=1 to run live PostgreSQL tests.")
    admin_url = os.environ.get(ADMIN_URL, "").strip()
    if not admin_url:
        pytest.skip(f"Live PostgreSQL tests not run: {ADMIN_URL} is not configured.")
    try:
        parsed, port = parse_safe_admin_url(admin_url)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)

    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        pytest.skip("Live PostgreSQL tests not run: install the declared 'persistence' extra first.")

    env = dict(os.environ)
    passfile_value = env.get("PGPASSFILE", "").strip()
    if not passfile_value or not Path(passfile_value).is_file():
        pytest.skip("Live PostgreSQL tests not run: PGPASSFILE must point to an existing protected password file.")

    pg_bin_value = env.get("JARVIS_PG_BIN_DIR", "").strip()
    integration_db = f"jarvis_it_{uuid4().hex}"
    if not GENERATED_DATABASE_RE.fullmatch(integration_db):
        pytest.fail("Internal isolated database name validation failed.", pytrace=False)
    migration_config = runner.DatabaseConfig(
        host=parsed.hostname or "",
        port=port,
        database=integration_db,
        username=unquote(parsed.username or ""),
        maintenance_database="postgres",
        pg_bin_dir=Path(pg_bin_value) if pg_bin_value else None,
        pgpass_file=Path(passfile_value),
        backup_dir=Path(env.get("TEMP", str(Path.home()))) / "jarvis-integration-backups",
    )
    try:
        runner._require_tools(("psql", "createdb"), migration_config, env)
        runner._validate_credentials_file(migration_config)
    except runner.SetupError as exc:
        pytest.skip(f"Live PostgreSQL tests not run: migration prerequisites unavailable ({exc}).")

    database_url = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{integration_db}", "", "")
    )
    created = False
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            collision = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (integration_db,)
            ).fetchone()
            if collision is not None:
                pytest.fail("Generated isolated database name unexpectedly exists.", pytrace=False)
            # This name is generated in-process and validated against the test-only prefix above.
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(integration_db)))
            created = True

        if runner.install(migration_config, environ=env) != 0:
            pytest.fail(
                "The existing migration runner could not initialize the isolated integration database; "
                "review its sanitized diagnostic above.",
                pytrace=False,
            )
        yield IsolatedPostgres(admin_url, database_url, integration_db, migration_config)
    except pytest.skip.Exception:
        raise
    except pytest.fail.Exception:
        raise
    except Exception as exc:
        # Do not include driver messages or connection details; they may contain sensitive context.
        pytest.fail(
            f"PostgreSQL integration setup failed ({type(exc).__name__}); connection details were suppressed.",
            pytrace=False,
        )
    finally:
        if created:
            if not GENERATED_DATABASE_RE.fullmatch(integration_db):
                raise RuntimeError("Refusing to drop a database without the generated integration-test name.")
            try:
                with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (integration_db,),
                    )
                    admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(integration_db)))
            except Exception as exc:
                pytest.fail(
                    f"Could not remove the generated isolated integration database ({type(exc).__name__}); "
                    "no other database was targeted.",
                    pytrace=False,
                )


def test_clean_migrations_checksums_and_rerun(isolated_postgres):
    expected = {
        migration_id: checksum
        for migration_id, _, _, checksum in runner._migration_files(runner.MIGRATIONS_DIR)
    }
    assert runner.verify(isolated_postgres.migration_config) == 0
    assert runner.install(isolated_postgres.migration_config) == 0
    assert runner.verify(isolated_postgres.migration_config) == 0

    import psycopg

    with psycopg.connect(isolated_postgres.database_url) as connection:
        rows = connection.execute(
            "SELECT migration_id, checksum_sha256 FROM jarvis_meta.schema_migrations ORDER BY migration_id"
        ).fetchall()
    assert dict(rows) == expected


def test_chat_creates_continues_and_reads_history_across_pages(isolated_postgres):
    with isolated_postgres.client() as client:
        first = client.post("/chat", json={"message": "first turn"})
        assert first.status_code == 200
        conversation_id = UUID(first.json()["conversation_id"])

        second = client.post(
            "/chat", json={"message": "second turn", "conversation_id": str(conversation_id)}
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == str(conversation_id)
        third = client.post(
            "/chat", json={"message": "third turn", "conversation_id": str(conversation_id)}
        )
        assert third.status_code == 200

        newest = client.get(f"/conversations/{conversation_id}/messages?limit=2")
        assert newest.status_code == 200
        newest_page = newest.json()
        assert [m["sequence_number"] for m in newest_page["messages"]] == [4, 5]
        assert [m["role"] for m in newest_page["messages"]] == ["user", "assistant"]
        assert newest_page["has_more"] is True
        assert newest_page["next_before_sequence"] == 4

        middle = client.get(
            f"/conversations/{conversation_id}/messages?limit=2&before_sequence={newest_page['next_before_sequence']}"
        )
        assert middle.status_code == 200
        middle_page = middle.json()
        assert [m["sequence_number"] for m in middle_page["messages"]] == [2, 3]
        assert [m["role"] for m in middle_page["messages"]] == ["user", "assistant"]
        assert middle_page["has_more"] is True
        assert middle_page["next_before_sequence"] == 2

        oldest = client.get(
            f"/conversations/{conversation_id}/messages?limit=2&before_sequence={middle_page['next_before_sequence']}"
        )
        assert oldest.status_code == 200
        oldest_page = oldest.json()
        assert [m["sequence_number"] for m in oldest_page["messages"]] == [0, 1]
        assert [m["role"] for m in oldest_page["messages"]] == ["user", "assistant"]
        assert oldest_page["has_more"] is False
        assert oldest_page["next_before_sequence"] is None

        all_pages = [newest_page, middle_page, oldest_page]
        sequences = [
            message["sequence_number"]
            for page in all_pages
            for message in page["messages"]
        ]
        assert sequences == [4, 5, 2, 3, 0, 1]
        assert sorted(sequences) == list(range(6))
        assert len(set(sequences)) == 6
        assert all(
            message["conversation_id"] == str(conversation_id)
            for page in all_pages
            for message in page["messages"]
        )


def test_unknown_conversation_is_not_created(isolated_postgres):
    import psycopg

    conversation_id = uuid4()
    with isolated_postgres.client() as client:
        response = client.post(
            "/chat", json={"message": "must not create", "conversation_id": str(conversation_id)}
        )
    assert response.status_code == 404
    with psycopg.connect(isolated_postgres.database_url) as connection:
        found = connection.execute(
            "SELECT 1 FROM jarvis_app.conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
    assert found is None
    with pytest.raises(ConversationNotFound):
        isolated_postgres.repository().get_messages(conversation_id)


def test_failed_exchange_rolls_back_and_repository_recovers(isolated_postgres):
    import psycopg

    repository = isolated_postgres.repository()
    conversation_id = repository.save_exchange("seed", "seed-reply")
    with pytest.raises(PersistenceError) as error:
        repository.save_exchange("must roll back", "x" * 40001, conversation_id)
    assert str(error.value) == "Conversation persistence is unavailable."

    after_failure = repository.get_messages(conversation_id, limit=10)
    assert [item.content for item in after_failure] == ["seed", "seed-reply"]
    repository.save_exchange("recovery", "recovered", conversation_id)
    recovered = repository.get_messages(conversation_id, limit=10)
    assert [item.content for item in recovered] == ["seed", "seed-reply", "recovery", "recovered"]
    with psycopg.connect(isolated_postgres.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM jarvis_app.messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()[0] == 4


def test_concurrent_exchange_appends_have_complete_unique_sequences(isolated_postgres):
    repository = isolated_postgres.repository()
    conversation_id = repository.save_exchange("seed", "seed-reply")
    exchange_count = 8

    def append(index: int):
        return repository.save_exchange(f"user-{index}", f"assistant-{index}", conversation_id)

    with ThreadPoolExecutor(max_workers=4) as executor:
        returned_ids = list(executor.map(append, range(exchange_count)))

    assert returned_ids == [conversation_id] * exchange_count
    messages = repository.get_messages(conversation_id, limit=2 * (exchange_count + 1))
    assert [message.sequence_number for message in messages] == list(range(2 * (exchange_count + 1)))
    assert len(messages) == 2 * (exchange_count + 1)
    assert all(messages[index].role == "user" for index in range(0, len(messages), 2))
    assert all(messages[index].role == "assistant" for index in range(1, len(messages), 2))
    assert {(messages[i].content, messages[i + 1].content) for i in range(2, len(messages), 2)} == {
        (f"user-{n}", f"assistant-{n}") for n in range(exchange_count)
    }


def test_unreachable_postgres_connection_is_sanitized(isolated_postgres):
    repository = PostgresConversationRepository(
        "postgresql://test@127.0.0.1:1/jarvis_it_unreachable", connect_timeout_seconds=1
    )
    with pytest.raises(PersistenceError) as error:
        repository.save_exchange("question", "answer")
    assert str(error.value) == "Conversation persistence is unavailable."
    assert "127.0.0.1" not in str(error.value)
