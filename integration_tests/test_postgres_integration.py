"""Opt-in PostgreSQL integration tests using an isolated local database."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
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
from jarvis.persistence.memory_postgres import PostgresMemoryRepository


RUN_INTEGRATION = "JARVIS_RUN_POSTGRES_INTEGRATION"
ADMIN_URL = "JARVIS_TEST_POSTGRES_ADMIN_URL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GENERATED_DATABASE_RE = re.compile(r"^jarvis_it_[0-9a-f]{32}$")
TEST_OWNER_ID = UUID("a5bff5ad-042a-4132-913a-4da0cfe24710")
TEST_AUTH_TOKEN = "integration-test-token-with-at-least-32-characters"


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

    def memory_repository(self) -> PostgresMemoryRepository:
        return PostgresMemoryRepository(self.database_url, connect_timeout_seconds=3)

    def client(self) -> TestClient:
        settings = Settings(
            _env_file=None,
            environment="test",
            persistence_enabled=True,
            persistence_database_url=SecretStr(self.database_url),
            persistence_connect_timeout_seconds=3,
            local_auth_token=SecretStr(TEST_AUTH_TOKEN),
            owner_id=TEST_OWNER_ID,
        )
        return TestClient(create_app(settings=settings), headers={"Authorization": f"Bearer {TEST_AUTH_TOKEN}"})


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


def test_owner_scoping_hides_foreign_and_legacy_unowned_rows(isolated_postgres):
    import psycopg

    from jarvis.memory.models import MemoryUpdate

    owner = TEST_OWNER_ID
    foreign_owner = UUID("72b0c38d-c014-486b-82db-360f95e0ab31")
    repository = isolated_postgres.memory_repository()
    owned = repository.create(owner, "owner scoped searchable record", category="private")
    foreign = repository.create(foreign_owner, "owner scoped searchable record", category="private")
    unowned = UUID("975a8346-5290-4d5a-bf8e-3dc62de597c6")
    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.execute(
            "INSERT INTO jarvis_app.memories (id, content, category) VALUES (%s, %s, %s)",
            (unowned, "owner scoped searchable record", "private"),
        )

    page = repository.list(owner, limit=20, category="private")
    searched = repository.search(owner, "owner scoped", limit=20)
    assert [item.id for item in page.items] == [owned.id]
    assert page.total == 1
    assert [item.id for item in searched.items] == [owned.id]
    assert searched.total == 1
    assert repository.get(owner, foreign.id) is None
    assert repository.get(owner, unowned) is None
    assert repository.update(owner, foreign.id, MemoryUpdate(content="forbidden")) is None
    assert repository.update(owner, unowned, MemoryUpdate(content="forbidden")) is None
    assert repository.delete(owner, foreign.id) is False
    assert repository.delete(owner, unowned) is False

    with isolated_postgres.client() as client:
        assert client.get("/memories").json()["total"] == 1
        assert client.get(f"/memories/{foreign.id}").status_code == 404
        assert client.get(f"/memories/{unowned}").status_code == 404
        assert client.patch(f"/memories/{foreign.id}", json={"content": "forbidden"}).status_code == 404
        assert client.delete(f"/memories/{foreign.id}").status_code == 404
        assert client.post("/memories/search", json={"query": "owner scoped"}).json()["total"] == 1


def test_upgrade_keeps_existing_memories_unowned(isolated_postgres):
    """A v1.1 database upgrades without silently assigning legacy memory ownership."""
    import psycopg
    from psycopg import sql

    generated_name = f"jarvis_it_{uuid4().hex}"
    assert GENERATED_DATABASE_RE.fullmatch(generated_name)
    old_config = runner.DatabaseConfig(
        host=isolated_postgres.migration_config.host,
        port=isolated_postgres.migration_config.port,
        database=generated_name,
        username=isolated_postgres.migration_config.username,
        maintenance_database="postgres",
        pg_bin_dir=isolated_postgres.migration_config.pg_bin_dir,
        pgpass_file=isolated_postgres.migration_config.pgpass_file,
        backup_dir=isolated_postgres.migration_config.backup_dir,
    )
    created = False
    legacy_id = uuid4()
    try:
        with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
            if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (generated_name,)).fetchone():
                pytest.fail("Generated upgrade-test database name unexpectedly exists.", pytrace=False)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(generated_name)))
            created = True

        with tempfile.TemporaryDirectory(prefix="jarvis-v11-migrations-") as temporary:
            old_database_dir = Path(temporary)
            scripts = old_database_dir / "scripts"
            scripts.mkdir()
            for migration in sorted(runner.MIGRATIONS_DIR.glob("000[123]_*.sql")):
                shutil.copy2(migration, scripts / migration.name)
            if runner.install(old_config, database_dir=old_database_dir) != 0:
                pytest.fail("Could not apply the v1.1 migration set to the generated test database.", pytrace=False)
            legacy_url = isolated_postgres.database_url.rsplit("/", 1)[0] + "/" + generated_name
            with psycopg.connect(legacy_url) as connection:
                connection.execute(
                    "INSERT INTO jarvis_app.memories (id, content, category) VALUES (%s, %s, %s)",
                    (legacy_id, "legacy unowned memory", "legacy"),
                )

        if runner.install(old_config) != 0:
            pytest.fail("Could not apply migration 0004 to the generated upgrade-test database.", pytrace=False)
        legacy_url = isolated_postgres.database_url.rsplit("/", 1)[0] + "/" + generated_name
        with psycopg.connect(legacy_url) as connection:
            assert connection.execute(
                "SELECT owner_id FROM jarvis_app.memories WHERE id = %s", (legacy_id,)
            ).fetchone() == (None,)
        settings = Settings(
            _env_file=None, environment="test", persistence_enabled=True,
            persistence_database_url=SecretStr(legacy_url), persistence_connect_timeout_seconds=3,
            local_auth_token=SecretStr(TEST_AUTH_TOKEN), owner_id=TEST_OWNER_ID,
        )
        with TestClient(create_app(settings=settings), headers={"Authorization": f"Bearer {TEST_AUTH_TOKEN}"}) as client:
            assert client.get("/memories").json()["total"] == 0
            assert client.get(f"/memories/{legacy_id}").status_code == 404
    finally:
        if created:
            if not GENERATED_DATABASE_RE.fullmatch(generated_name):
                raise RuntimeError("Refusing to drop a database without the generated integration-test name.")
            with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (generated_name,),
                )
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(generated_name)))


def test_memory_crud_search_ranking_and_delete(isolated_postgres):
    import psycopg

    from jarvis.memory.models import MemoryUpdate
    from jarvis.persistence.memory_postgres import PostgresMemoryRepository

    repository = PostgresMemoryRepository(isolated_postgres.database_url, connect_timeout_seconds=3)
    owner_id = TEST_OWNER_ID
    top_relevant = repository.create(
        owner_id,
        "JARVIS memory retrieval memory retrieval improves lexical ranking.", category="project"
    )
    second_relevant = repository.create(owner_id, "JARVIS memory retrieval is deterministic.", category="project")
    partial_match = repository.create(owner_id, "JARVIS memory is stored separately.", category="project")
    unrelated = repository.create(owner_id, "Tomato plants need sunlight.", category="garden")
    privacy_relevant = repository.create(owner_id, "Privacy-safe retrieval diagnostics exclude private query text.")
    repository.create(owner_id, "This record discusses diagnostics only.")

    # The expected relevant IDs are explicit for this small lexical regression corpus.
    ranked = repository.search(owner_id, "memory retrieval", limit=2)
    assert ranked.total == 2
    assert ranked.has_more is False
    assert [item.id for item in ranked.items] == [top_relevant.id, second_relevant.id]
    assert sum(item.id in {top_relevant.id, second_relevant.id} for item in ranked.items) / 2 == 1.0

    other_terms = repository.search(owner_id, "privacy retrieval", limit=5)
    assert [item.id for item in other_terms.items] == [privacy_relevant.id]
    assert partial_match.id not in {item.id for item in ranked.items}
    assert unrelated.id not in {item.id for item in ranked.items}
    assert repository.search(owner_id, "term-with-no-match", limit=5).items == ()

    first_page = repository.search(owner_id, "memory retrieval", limit=1)
    second_page = repository.search(owner_id, "memory retrieval", limit=1, offset=1)
    assert first_page.has_more is True
    assert second_page.has_more is False
    assert [item.id for item in (*first_page.items, *second_page.items)] == [
        top_relevant.id, second_relevant.id
    ]

    # Identical content and timestamps make rank equal; UUID ordering must then decide.
    tie_a = repository.create(owner_id, "tie breaker lexical corpus")
    tie_b = repository.create(owner_id, "tie breaker lexical corpus")
    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.execute(
            "UPDATE jarvis_app.memories SET updated_at = TIMESTAMPTZ '2001-01-01 00:00:00+00' "
            "WHERE id = ANY(%s)",
            ([tie_a.id, tie_b.id],),
        )
    tied_results = repository.search(owner_id, "tie breaker", limit=5)
    assert [item.id for item in tied_results.items] == sorted((tie_a.id, tie_b.id), key=str)

    duplicate_a = repository.create(owner_id, "duplicate lifecycle content", category="notes", source_reference="manual")
    duplicate_b = repository.create(owner_id, "duplicate lifecycle content", category="notes", source_reference="manual")
    assert duplicate_a.id != duplicate_b.id

    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.execute(
            "UPDATE jarvis_app.memories SET updated_at = TIMESTAMPTZ '2001-01-01 00:00:00+00' WHERE id = %s",
            (duplicate_a.id,),
        )
    before = repository.get(owner_id, duplicate_a.id)
    assert before is not None
    changed = repository.update(
        owner_id, duplicate_a.id,
        MemoryUpdate(category=None, source_reference=None),
    )
    assert changed is not None
    assert changed.content == "duplicate lifecycle content"
    assert changed.category is None and changed.source_reference is None
    assert changed.updated_at > before.updated_at
    assert repository.delete(owner_id, duplicate_a.id) is True
    assert repository.get(owner_id, duplicate_a.id) is None
    assert repository.delete(owner_id, duplicate_a.id) is False
    assert repository.get(owner_id, duplicate_b.id) is not None

    fetched = repository.get(owner_id, top_relevant.id)
    assert fetched is not None
    assert fetched.category == "project"


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


def test_memory_category_filter_is_exact_and_paginates_filtered_results(isolated_postgres):
    repository = isolated_postgres.memory_repository()
    owner_id = TEST_OWNER_ID
    first = repository.create(owner_id, "first filtered category record", category="v1.1-notes")
    second = repository.create(owner_id, "second filtered category record", category="v1.1-notes")
    repository.create(owner_id, "unrelated category record", category="V1.1-notes")
    with isolated_postgres.client() as client:
        page_one = client.get("/memories?category=v1.1-notes&limit=1&offset=0")
        page_two = client.get("/memories?category=v1.1-notes&limit=1&offset=1")
        wrong_case = client.get("/memories?category=V1.1-notes&limit=10")
        blank = client.get("/memories?category=%20%20")
    assert page_one.status_code == page_two.status_code == wrong_case.status_code == 200
    assert page_one.json()["total"] == page_two.json()["total"] == 2
    assert page_one.json()["has_more"] is True
    assert page_two.json()["has_more"] is False
    page_ids = [page_one.json()["items"][0]["id"], page_two.json()["items"][0]["id"]]
    assert set(page_ids) == {str(first.id), str(second.id)}
    assert len(set(page_ids)) == 2
    assert wrong_case.json()["total"] == 1
    assert blank.status_code == 422


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
