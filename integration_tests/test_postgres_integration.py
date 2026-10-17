"""Opt-in PostgreSQL integration tests using an isolated local database."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from database import runner
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.persistence.base import ConversationNotFound, PersistenceError
from jarvis.persistence.postgres import PostgresConversationRepository
from jarvis.persistence.memory_postgres import PostgresMemoryRepository
from jarvis.memory.base import MemoryDeadlineExceeded


RUN_INTEGRATION = "JARVIS_RUN_POSTGRES_INTEGRATION"
ADMIN_URL = "JARVIS_TEST_POSTGRES_ADMIN_URL"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GENERATED_DATABASE_RE = re.compile(r"^jarvis_it_[0-9a-f]{32}$")
TEST_OWNER_ID = UUID("a5bff5ad-042a-4132-913a-4da0cfe24710")
TEST_AUTH_TOKEN = "integration-test-token-with-at-least-32-characters"


def _classify_preexisting_generated_database(name: object, connection_count: object) -> str | None:
    """Classify a generated-name database for reporting, never for deletion."""
    if not isinstance(name, str) or not GENERATED_DATABASE_RE.fullmatch(name):
        return None
    if isinstance(connection_count, bool) or not isinstance(connection_count, int) or connection_count < 0:
        return "uncertain"
    return "active" if connection_count else "uncertain"


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


@pytest.mark.parametrize(
    ("name", "connection_count", "expected"),
    [
        ("jarvis_AI", 0, None),
        ("jarvis_it_" + "a" * 32, 0, "uncertain"),
        ("jarvis_it_" + "a" * 31, 0, None),
        ("jarvis_it_" + "b" * 32, 2, "active"),
        ("jarvis_it_" + "c" * 32, 0, "uncertain"),
        ("jarvis_it_" + "d" * 32, None, "uncertain"),
    ],
)
def test_preexisting_database_review_never_marks_unsafe_or_uncertain_database_for_cleanup(
    name, connection_count, expected
):
    assert _classify_preexisting_generated_database(name, connection_count) == expected


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


def test_authenticated_document_imports_create_owner_scoped_memory_records(isolated_postgres):
    repository = isolated_postgres.memory_repository()
    imported_ids = []
    statuses = []
    for _ in range(2):
        with isolated_postgres.client() as client:
            response = client.post(
                "/memories/ingest",
                content=b"v1 document import integration marker",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Document-Filename": r"C:\private\integration-note.MD",
                },
            )
        statuses.append(response.status_code)
        record = response.json()
        assert record["content"] == "v1 document import integration marker"
        assert record["source_reference"] == "integration-note.MD"
        assert "private" not in record["source_reference"]
        imported_ids.append(UUID(record["id"]))

    # Since v2.2, re-importing identical content returns the existing record (HTTP 200).
    assert statuses == [201, 200], statuses
    assert imported_ids[0] == imported_ids[1]
    assert repository.get(TEST_OWNER_ID, imported_ids[0]) is not None
    other_owner_id = UUID("b5bff5ad-042a-4132-913a-4da0cfe24710")
    assert repository.get(other_owner_id, imported_ids[0]) is None
    matching = repository.search(TEST_OWNER_ID, "v1 document import integration marker", limit=5)
    assert {item.id for item in matching.items} == {imported_ids[0]}
    assert matching.total == 1


def test_memory_lexical_retrieval_evaluation(isolated_postgres):
    """Record deterministic PostgreSQL simple-config lexical-search baselines."""
    import psycopg

    repository = isolated_postgres.memory_repository()
    owner_id = TEST_OWNER_ID
    foreign_owner_id = UUID("72b0c38d-c014-486b-82db-360f95e0ab31")
    first_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Fixed UUIDs and timestamps make ranking tie-breaks reproducible. The shared
    # final token supports a result-limit case without changing other query terms.
    owner_corpus = (
        ("alpha_adjacent", "alpha beta common"),
        ("alpha_near", "alpha x beta common"),
        ("alpha_far", "alpha one two three beta common"),
        ("alpha_repeated", "alpha beta alpha beta common"),
        ("alpha_only", "alpha only common"),
        ("beta_only", "beta only common"),
        ("violet_single", "violet common"),
        ("violet_double", "violet violet common"),
        ("violet_triple", "violet violet violet common"),
        ("signal_single", "signal common"),
        ("signal_double", "signal signal common"),
        ("signal_triple", "signal signal signal common"),
        ("red_adjacent", "red blue common"),
        ("red_far", "red one two blue common"),
        ("red_repeated", "red blue red blue common"),
        ("cache_plain", "cache key common"),
        ("cache_punctuation", "CACHE, KEY! common"),
        ("prefix_only", "alphabeta common"),
        ("tie_a", "tieprobe breakerprobe common"),
        ("tie_b", "tieprobe breakerprobe common"),
        ("owner_isolation", "orchid sentinel common"),
    )
    identifiers = {
        name: UUID(int=0x17000000000000000000000000000000 + index)
        for index, (name, _) in enumerate(owner_corpus, start=1)
    }
    foreign_ids = (
        UUID(int=0x17000000000000000000000000000101),
        UUID(int=0x17000000000000000000000000000102),
    )
    unowned_ids = (
        UUID(int=0x17000000000000000000000000000201),
        UUID(int=0x17000000000000000000000000000202),
    )
    labels_by_id = {str(memory_id): label for label, memory_id in identifiers.items()}
    labels_by_id.update(
        {str(memory_id): f"foreign_{index}" for index, memory_id in enumerate(foreign_ids, 1)}
    )
    labels_by_id.update(
        {str(memory_id): f"unowned_{index}" for index, memory_id in enumerate(unowned_ids, 1)}
    )

    rows = []
    for index, (label, content) in enumerate(owner_corpus, start=1):
        timestamp_index = 19 if label in {"tie_a", "tie_b"} else index
        timestamp = first_timestamp + timedelta(minutes=timestamp_index)
        rows.append((identifiers[label], owner_id, content, "evaluation", None, timestamp, timestamp))
    for index, memory_id in enumerate(foreign_ids, start=1):
        content = "orchid sentinel common foreign control" if index == 1 else "alpha beta common foreign control"
        timestamp = first_timestamp + timedelta(minutes=100 + index)
        rows.append((memory_id, foreign_owner_id, content, "evaluation", None, timestamp, timestamp))
    for index, memory_id in enumerate(unowned_ids, start=1):
        content = "orchid sentinel common unowned control" if index == 1 else "alpha beta common unowned control"
        timestamp = first_timestamp + timedelta(minutes=110 + index)
        rows.append((memory_id, None, content, "evaluation", None, timestamp, timestamp))

    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.cursor().executemany(
            """INSERT INTO jarvis_app.memories
               (id, owner_id, content, category, source_reference, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )

    cases = (
        ("single_term_violet", "violet", ("violet_triple", "violet_double", "violet_single"), 100),
        ("term_frequency_signal", "signal", ("signal_triple", "signal_double", "signal_single"), 100),
        (
            "multi_term_and_cover_density", "alpha beta",
            ("alpha_repeated", "alpha_adjacent", "alpha_near", "alpha_far"), 100,
        ),
        ("proximity_red_blue", "red blue", ("red_repeated", "red_adjacent", "red_far"), 100),
        ("punctuation_in_document", "cache key", ("cache_punctuation", "cache_plain"), 100),
        ("case_and_punctuation_in_query", "CACHE, KEY!", ("cache_punctuation", "cache_plain"), 100),
        ("prefix_is_not_prefix_search", "alph", (), 100),
        ("zero_match", "unfindablequartz", (), 100),
        ("equal_score_uuid_tie_break", "tieprobe breakerprobe", ("tie_a", "tie_b"), 100),
        ("owner_isolation", "orchid sentinel", ("owner_isolation",), 100),
        (
            "five_result_limit_and_timestamps", "common",
            ("owner_isolation", "tie_a", "tie_b", "prefix_only", "cache_punctuation"), 5,
        ),
        (
            "repeat_query_determinism", "alpha beta",
            ("alpha_repeated", "alpha_adjacent", "alpha_near", "alpha_far"), 100,
        ),
    )
    assert len(cases) == 12

    diagnostics = []
    actual_by_case = {}
    allowed_ids = set(identifiers.values())
    for case_name, query, expected_labels, limit in cases:
        page = repository.search(owner_id, query, limit=limit)
        actual_ids = tuple(item.id for item in page.items)
        expected_ids = tuple(identifiers[label] for label in expected_labels)
        assert actual_ids == expected_ids, f"{case_name}: query={query!r}"
        assert set(actual_ids) <= allowed_ids
        assert len(page.items) <= limit
        actual_by_case[case_name] = actual_ids

        with psycopg.connect(isolated_postgres.database_url) as connection:
            scored_rows = connection.execute(
                """SELECT id::text,
                          ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', %s)) AS score
                   FROM jarvis_app.memories
                   WHERE owner_id = %s
                     AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                   ORDER BY score DESC, updated_at DESC, id ASC
                   LIMIT %s""",
                (query, owner_id, query, limit),
            ).fetchall()
        diagnostics.append({
            "case": case_name,
            "query": query,
            "expected": list(expected_labels),
            "actual": [labels_by_id[str(memory_id)] for memory_id in actual_ids],
            "total": page.total,
            "limit": page.limit,
            "has_more": page.has_more,
            "rank_scores": [
                {"record": labels_by_id[memory_id], "ts_rank_cd": float(score)}
                for memory_id, score in scored_rows
            ],
        })

    assert (
        actual_by_case["repeat_query_determinism"]
        == actual_by_case["multi_term_and_cover_density"]
    )
    assert diagnostics[8]["rank_scores"][0]["ts_rank_cd"] == pytest.approx(
        diagnostics[8]["rank_scores"][1]["ts_rank_cd"]
    )
    limited = next(item for item in diagnostics if item["case"] == "five_result_limit_and_timestamps")
    assert limited["total"] == len(owner_corpus)
    assert limited["has_more"] is True
    assert len(limited["actual"]) == 5

    stage1_candidate_diagnostics = []
    for case_name, query, expected_labels, limit in cases:
        with psycopg.connect(isolated_postgres.database_url) as connection:
            candidate_rows = connection.execute(
                """SELECT id::text,
                          ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', %s), 1) AS score
                   FROM jarvis_app.memories
                   WHERE owner_id = %s
                     AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                   ORDER BY score DESC, updated_at DESC, id ASC
                   LIMIT %s""",
                (query, owner_id, query, limit),
            ).fetchall()
        candidate_labels = [labels_by_id[memory_id] for memory_id, _ in candidate_rows]
        stage1_candidate_diagnostics.append(
            {
                "case": case_name,
                "query": query,
                "expected": list(expected_labels),
                "candidate_normalization_1": candidate_labels,
                "candidate_scores": [
                    {"record": labels_by_id[memory_id], "ts_rank_cd": float(score)}
                    for memory_id, score in candidate_rows
                ],
                "matches_stage_1_expected_ranking": candidate_labels == list(expected_labels),
            }
        )
    stage1_candidate_has_no_regressions = all(
        item["matches_stage_1_expected_ranking"] for item in stage1_candidate_diagnostics
    )

    stage1_normalization_2_diagnostics = []
    for case_name, query, expected_labels, limit in cases:
        with psycopg.connect(isolated_postgres.database_url) as connection:
            candidate_rows = connection.execute(
                """SELECT id::text,
                          ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', %s), 2) AS score
                   FROM jarvis_app.memories
                   WHERE owner_id = %s
                     AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                   ORDER BY score DESC, updated_at DESC, id ASC
                   LIMIT %s""",
                (query, owner_id, query, limit),
            ).fetchall()
            repeated_rows = connection.execute(
                """SELECT id::text,
                          ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', %s), 2) AS score
                   FROM jarvis_app.memories
                   WHERE owner_id = %s
                     AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                   ORDER BY score DESC, updated_at DESC, id ASC
                   LIMIT %s""",
                (query, owner_id, query, limit),
            ).fetchall()
        candidate_ids = [memory_id for memory_id, _ in candidate_rows]
        candidate_labels = [labels_by_id[memory_id] for memory_id in candidate_ids]
        stage1_normalization_2_diagnostics.append(
            {
                "case": case_name,
                "query": query,
                "expected": list(expected_labels),
                "candidate_normalization_2": candidate_labels,
                "candidate_scores": [
                    {"record": labels_by_id[memory_id], "ts_rank_cd": float(score)}
                    for memory_id, score in candidate_rows
                ],
                "matches_stage_1_expected_ranking": candidate_labels == list(expected_labels),
                "repeat_query_deterministic": repeated_rows == candidate_rows,
                "owner_scoped": set(candidate_ids) <= allowed_ids,
                "within_requested_limit": len(candidate_ids) <= limit,
            }
        )
    stage1_normalization_2_has_no_regressions = all(
        item["matches_stage_1_expected_ranking"]
        and item["repeat_query_deterministic"]
        and item["owner_scoped"]
        and item["within_requested_limit"]
        for item in stage1_normalization_2_diagnostics
    )

    print(
        "LEXICAL_RETRIEVAL_BASELINE="
        + json.dumps(
            {
                "postgres_database": isolated_postgres.database_name,
                "owner_record_count": len(owner_corpus),
                "foreign_control_count": len(foreign_ids),
                "unowned_control_count": len(unowned_ids),
                "cases": diagnostics,
            },
            sort_keys=True,
        )
    )

    # Keep the Stage 1 corpus and its 12 expected rankings unchanged. Add a
    # separate length-sensitive corpus only after those regression gates have
    # completed. Each query has one concise relevant record and two longer
    # records that also satisfy plainto_tsquery's AND matching predicate.
    length_cases = (
        {
            "case": "length_sensitivity_cobalt_orbit",
            "query": "cobalt orbit",
            "relevant": "cobalt_orbit_short",
            "repeated": "cobalt_orbit_repeated_long",
            "single": "cobalt_orbit_single_long",
        },
        {
            "case": "length_sensitivity_amber_valley",
            "query": "amber valley",
            "relevant": "amber_valley_short",
            "repeated": "amber_valley_repeated_long",
            "single": "amber_valley_single_long",
        },
        {
            "case": "length_sensitivity_silver_meadow",
            "query": "silver meadow",
            "relevant": "silver_meadow_short",
            "repeated": "silver_meadow_repeated_long",
            "single": "silver_meadow_single_long",
        },
    )
    length_identifiers = {
        label: UUID(int=0x17000000000000000000000000001000 + index)
        for index, label in enumerate(
            (
                "cobalt_orbit_short", "cobalt_orbit_repeated_long", "cobalt_orbit_single_long",
                "amber_valley_short", "amber_valley_repeated_long", "amber_valley_single_long",
                "silver_meadow_short", "silver_meadow_repeated_long", "silver_meadow_single_long",
            ),
            start=1,
        )
    }

    def alphabetic_token(index: int) -> str:
        """Return a unique alphabet-only lexeme for predictable simple FTS."""
        suffix = ""
        while index:
            index, remainder = divmod(index, 26)
            suffix = chr(ord("a") + remainder) + suffix
        return "padding" + (suffix or "a")

    length_rows = []
    for case_index, case in enumerate(length_cases):
        first, second = case["query"].split()
        # Hundreds of distinct alphabetic terms make these documents long in
        # the tsvector lexeme-count sense used by ts_rank_cd normalization 1,
        # while staying under the memory content bound. Noise never overlaps
        # the Stage 1 query vocabulary or another length-sensitive query.
        padding = " ".join(
            alphabetic_token(case_index * 400 + token_index + 1)
            for token_index in range(300)
        )
        timestamp_base = first_timestamp + timedelta(days=case_index + 1)
        contents = (
            (case["relevant"], f"{first} {second}", 1),
            (case["repeated"], f"{first} {second} {first} {second} {padding}", 3),
            (case["single"], f"{first} {second} {padding}", 2),
        )
        for label, content, minute in contents:
            assert len(content) <= 4000, f"{label} exceeds the memory content limit"
            timestamp = timestamp_base + timedelta(minutes=minute)
            length_rows.append(
                (length_identifiers[label], owner_id, content, "evaluation", None, timestamp, timestamp)
            )

    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.cursor().executemany(
            """INSERT INTO jarvis_app.memories
               (id, owner_id, content, category, source_reference, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            length_rows,
        )

    length_labels = {str(memory_id): label for label, memory_id in length_identifiers.items()}
    length_diagnostics = []

    def scored_ranking(query: str, normalization: int):
        # Both candidates use the same vector, query, owner predicate,
        # matching predicate, stable tie-breakers, and result limit. Only the
        # ts_rank_cd normalization argument differs (0=current, 1=candidate).
        with psycopg.connect(isolated_postgres.database_url) as connection:
            return connection.execute(
                """SELECT id::text,
                          ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', %s), %s) AS score
                   FROM jarvis_app.memories
                   WHERE owner_id = %s
                     AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                   ORDER BY score DESC, updated_at DESC, id ASC
                   LIMIT 5""",
                (query, normalization, owner_id, query),
            ).fetchall()

    def metrics(labels: list[str], relevant: set[str]) -> dict[str, float]:
        top_five = labels[:5]
        relevant_count = sum(label in relevant for label in top_five)
        first_relevant_rank = next(
            (rank for rank, label in enumerate(top_five, start=1) if label in relevant),
            None,
        )
        return {
            "precision_at_5": relevant_count / 5,
            "recall_at_5": relevant_count / len(relevant),
            "mrr_at_5": 0.0 if first_relevant_rank is None else 1 / first_relevant_rank,
        }

    for case in length_cases:
        query = case["query"]
        relevant = {case["relevant"]}
        expected_current = [case["repeated"], case["single"], case["relevant"]]
        expected_candidate = [case["relevant"], case["repeated"], case["single"]]

        current_page = repository.search(owner_id, query, limit=5)
        current_ids = [str(item.id) for item in current_page.items]
        current_labels = [length_labels[memory_id] for memory_id in current_ids]
        current_scored_rows = scored_ranking(query, 0)
        current_scored_ids = [row[0] for row in current_scored_rows]
        # Verify the diagnostic query exactly reproduces the production
        # repository's current ranking before comparing the candidate.
        assert current_ids == current_scored_ids
        assert current_labels == expected_current, f"current ranking: {case['case']}"

        candidate_rows = scored_ranking(query, 1)
        candidate_ids = [row[0] for row in candidate_rows]
        candidate_labels = [length_labels[memory_id] for memory_id in candidate_ids]
        assert candidate_labels == expected_candidate, f"normalized candidate: {case['case']}"
        repeated_candidate_rows = scored_ranking(query, 1)
        assert repeated_candidate_rows == candidate_rows

        normalization_2_rows = scored_ranking(query, 2)
        normalization_2_ids = [row[0] for row in normalization_2_rows]
        normalization_2_labels = [length_labels[memory_id] for memory_id in normalization_2_ids]
        expected_normalization_2 = [case["relevant"], case["repeated"], case["single"]]
        assert scored_ranking(query, 2) == normalization_2_rows
        assert len(current_ids) <= 5 and len(candidate_ids) <= 5
        assert len(normalization_2_ids) <= 5
        assert set(current_ids) <= {str(value) for value in length_identifiers.values()}
        assert set(candidate_ids) <= {str(value) for value in length_identifiers.values()}
        assert set(normalization_2_ids) <= {str(value) for value in length_identifiers.values()}

        repeated_page = repository.search(owner_id, query, limit=5)
        assert [str(item.id) for item in repeated_page.items] == current_ids
        assert repeated_page.total == current_page.total == 3

        current_metrics = metrics(current_labels, relevant)
        candidate_metrics = metrics(candidate_labels, relevant)
        normalization_2_metrics = metrics(normalization_2_labels, relevant)
        length_diagnostics.append(
            {
                "case": case["case"],
                "query": query,
                "relevant_records": sorted(relevant),
                "expected_current": expected_current,
                "actual_current": current_labels,
                "current_scores": [
                    {"record": length_labels[memory_id], "ts_rank_cd": float(score)}
                    for memory_id, score in current_scored_rows
                ],
                "expected_candidate_normalization_1": expected_candidate,
                "actual_candidate_normalization_1": candidate_labels,
                "candidate_scores": [
                    {"record": length_labels[memory_id], "ts_rank_cd": float(score)}
                    for memory_id, score in candidate_rows
                ],
                "expected_normalization_2": expected_normalization_2,
                "actual_normalization_2": normalization_2_labels,
                "normalization_2_matches_expected_order": (
                    normalization_2_labels == expected_normalization_2
                ),
                "normalization_2_scores": [
                    {"record": length_labels[memory_id], "ts_rank_cd": float(score)}
                    for memory_id, score in normalization_2_rows
                ],
                "current_metrics_at_5": current_metrics,
                "candidate_metrics_at_5": candidate_metrics,
                "normalization_2_metrics_at_5": normalization_2_metrics,
                "repeat_query_deterministic": True,
                "repeat_candidate_query_deterministic": True,
                "repeat_normalization_2_query_deterministic": True,
            }
        )

    aggregate = {
        f"{candidate}_{metric}": sum(
            item[f"{candidate}_metrics_at_5"][metric] for item in length_diagnostics
        )
        / len(length_diagnostics)
        for candidate in ("current", "candidate")
        for metric in ("precision_at_5", "recall_at_5", "mrr_at_5")
    }
    aggregate["mean_mrr_at_5_improvement"] = (
        aggregate["candidate_mrr_at_5"] - aggregate["current_mrr_at_5"]
    )
    assert aggregate["mean_mrr_at_5_improvement"] >= 0.10
    normalization_2_aggregate = {
        metric: sum(item["normalization_2_metrics_at_5"][metric] for item in length_diagnostics)
        / len(length_diagnostics)
        for metric in ("precision_at_5", "recall_at_5", "mrr_at_5")
    }
    normalization_2_aggregate["mean_mrr_at_5_improvement"] = (
        normalization_2_aggregate["mrr_at_5"] - aggregate["current_mrr_at_5"]
    )
    normalization_2_adoption_gate = (
        normalization_2_aggregate["mean_mrr_at_5_improvement"] >= 0.10
        and stage1_normalization_2_has_no_regressions
        and all(
            item["repeat_normalization_2_query_deterministic"]
            and len(item["actual_normalization_2"]) <= 5
            for item in length_diagnostics
        )
    )
    print(
        "LENGTH_SENSITIVE_RETRIEVAL_EVALUATION="
        + json.dumps(
            {
                "postgres_database": isolated_postgres.database_name,
                "ranking_comparisons": [
                    "normalization 0 (current) versus 1 (Stage 2 candidate)",
                    "normalization 0 (current) versus 2 (Stage 3 candidate)",
                ],
                "metric_definition": {
                    "precision_at_5": "relevant returned in first 5 divided by 5",
                    "recall_at_5": "relevant returned in first 5 divided by known relevant count",
                    "mrr_at_5": "reciprocal rank of first relevant record in first 5, else 0",
                },
                "cases": length_diagnostics,
                "aggregate_mean_metrics": aggregate,
                "candidate_threshold_met": aggregate["mean_mrr_at_5_improvement"] >= 0.10,
                "normalization_2_aggregate_mean_metrics": normalization_2_aggregate,
                "normalization_2_stage_1_rankings": stage1_normalization_2_diagnostics,
                "normalization_2_stage_1_regressions": not stage1_normalization_2_has_no_regressions,
                "normalization_2_adoption_gate_passed": normalization_2_adoption_gate,
                "normalization_2_candidate_outcome": (
                    "accepted for further review" if normalization_2_adoption_gate else "rejected"
                ),
                "stage_1_candidate_rankings": stage1_candidate_diagnostics,
                "stage_1_expected_rankings_regressed": not stage1_candidate_has_no_regressions,
                "candidate_adoption_recommended": (
                    aggregate["mean_mrr_at_5_improvement"] >= 0.10
                    and stage1_candidate_has_no_regressions
                ),
            },
            sort_keys=True,
        )
    )


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


def test_conversation_listing_repository_and_authenticated_api(isolated_postgres):
    """Verify bounded summaries and ordering only in the generated disposable database."""
    import psycopg

    assert GENERATED_DATABASE_RE.fullmatch(isolated_postgres.database_name)
    assert isolated_postgres.database_name != "jarvis_AI"
    conversation_ids = {
        "empty": UUID("11111111-1111-4111-8111-111111111101"),
        "first_user": UUID("11111111-1111-4111-8111-111111111102"),
        "exact_preview": UUID("11111111-1111-4111-8111-111111111103"),
        "unicode_preview": UUID("11111111-1111-4111-8111-111111111104"),
        "tie_low": UUID("11111111-1111-4111-8111-111111111105"),
        "tie_high": UUID("11111111-1111-4111-8111-111111111106"),
    }
    created_at = {
        "empty": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "first_user": datetime(2098, 1, 1, tzinfo=timezone.utc),
        "exact_preview": datetime(2097, 1, 1, tzinfo=timezone.utc),
        "unicode_preview": datetime(2096, 1, 1, tzinfo=timezone.utc),
        "tie_low": datetime(2095, 1, 1, tzinfo=timezone.utc),
        "tie_high": datetime(2095, 1, 1, tzinfo=timezone.utc),
    }
    empty_activity = created_at["empty"]
    first_user_activity = datetime(2098, 1, 5, tzinfo=timezone.utc)
    exact_activity = datetime(2097, 1, 2, tzinfo=timezone.utc)
    unicode_activity = datetime(2096, 1, 2, tzinfo=timezone.utc)
    tie_activity = datetime(2095, 1, 2, tzinfo=timezone.utc)
    exact_text = "é" * 120
    unicode_text = "雪🙂" * 65
    later_than_first_user = "Later user text must not become the preview."

    messages = {
        "first_user": [
            ("assistant", "Assistant opening must not become the preview.", datetime(2098, 1, 2, tzinfo=timezone.utc)),
            ("user", "First user message is the preview.", datetime(2098, 1, 3, tzinfo=timezone.utc)),
            ("assistant", "Assistant response must not become the preview.", datetime(2098, 1, 4, tzinfo=timezone.utc)),
            ("user", later_than_first_user, first_user_activity),
        ],
        "exact_preview": [("user", exact_text, exact_activity)],
        "unicode_preview": [("user", unicode_text, unicode_activity)],
        "tie_low": [("user", "Lower UUID tie record.", tie_activity)],
        "tie_high": [("user", "Higher UUID tie record.", tie_activity)],
    }

    with psycopg.connect(isolated_postgres.database_url) as connection:
        assert connection.execute("SELECT current_database()").fetchone() == (
            isolated_postgres.database_name,
        )
        for label, conversation_id in conversation_ids.items():
            connection.execute(
                "INSERT INTO jarvis_app.conversations (id, created_at) VALUES (%s, %s)",
                (conversation_id, created_at[label]),
            )
            for sequence_number, (role, content, message_time) in enumerate(messages.get(label, ())):
                connection.execute(
                    "INSERT INTO jarvis_app.messages "
                    "(id, conversation_id, sequence_number, role, content, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        uuid5(
                            NAMESPACE_URL,
                            f"jarvis-v2-phase3:{conversation_id}:{sequence_number}",
                        ),
                        conversation_id,
                        sequence_number,
                        role,
                        content,
                        message_time,
                    ),
                )

    repository = isolated_postgres.repository()
    default_page = repository.list_conversations()
    expected_order = [
        conversation_ids[label]
        for label in ("empty", "first_user", "exact_preview", "unicode_preview", "tie_high", "tie_low")
    ]
    assert default_page.limit == 20 and default_page.offset == 0
    assert [item.conversation_id for item in default_page.items[:6]] == expected_order
    assert len(default_page.items) <= default_page.limit

    by_id = {item.conversation_id: item for item in default_page.items}
    assert by_id[conversation_ids["empty"]].last_activity_at == created_at["empty"]
    assert by_id[conversation_ids["empty"]].preview is None
    assert by_id[conversation_ids["first_user"]].last_activity_at == first_user_activity
    assert by_id[conversation_ids["first_user"]].preview == "First user message is the preview."
    assert by_id[conversation_ids["first_user"]].preview != later_than_first_user
    assert by_id[conversation_ids["exact_preview"]].preview == exact_text
    assert by_id[conversation_ids["unicode_preview"]].preview == unicode_text[:119] + "…"
    assert len(by_id[conversation_ids["unicode_preview"]].preview) == 120
    assert by_id[conversation_ids["tie_high"]].last_activity_at == tie_activity
    assert by_id[conversation_ids["tie_low"]].last_activity_at == tie_activity

    custom_page = repository.list_conversations(limit=2, offset=1)
    assert custom_page.limit == 2 and custom_page.offset == 1
    assert [item.conversation_id for item in custom_page.items] == expected_order[1:3]
    assert custom_page.has_more is True
    assert len(custom_page.items) <= custom_page.limit

    final_page = repository.list_conversations(limit=100, offset=1_000_000)
    assert final_page.items == []
    assert final_page.has_more is False
    assert len(final_page.items) <= final_page.limit

    with isolated_postgres.client() as client:
        response = client.get("/conversations")
        assert response.status_code == 200
        api_page = response.json()
        assert api_page["limit"] == 20 and api_page["offset"] == 0
        assert [item["conversation_id"] for item in api_page["items"][:6]] == [
            str(conversation_id) for conversation_id in expected_order
        ]
        assert api_page["items"][0]["preview"] is None
        assert api_page["items"][3]["preview"] == unicode_text[:119] + "…"

        custom_response = client.get("/conversations?limit=2&offset=1")
        assert custom_response.status_code == 200
        custom_api_page = custom_response.json()
        assert custom_api_page["limit"] == 2 and custom_api_page["offset"] == 1
        assert [item["conversation_id"] for item in custom_api_page["items"]] == [
            str(conversation_id) for conversation_id in expected_order[1:3]
        ]
        assert custom_api_page["has_more"] is True
        assert len(custom_api_page["items"]) <= custom_api_page["limit"]


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


def test_memory_search_uses_psycopg_set_config_and_resets_timeout_after_commit(isolated_postgres):
    import psycopg

    class ReusableConnection:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            return False

        def execute(self, query, parameters=()):
            self.statements.append((query, parameters))
            return self.connection.execute(query, parameters)

    with psycopg.connect(isolated_postgres.database_url) as connection:
        baseline = connection.execute("SHOW statement_timeout").fetchone()[0]
        connection.commit()
        wrapper = ReusableConnection(connection)
        repository = PostgresMemoryRepository(
            isolated_postgres.database_url,
            connect=lambda *_args, **_kwargs: wrapper,
        )
        repository.search_with_deadline(
            TEST_OWNER_ID, "timeout integration absent phrase", deadline=monotonic() + 3, limit=5
        )
        timeout_statements = [item for item in wrapper.statements if "set_config('statement_timeout'" in item[0]]
        assert len(timeout_statements) == 2
        assert all("%s" in statement and "true" in statement for statement, _ in timeout_statements)
        assert all(parameters[0].endswith("ms") and int(parameters[0][:-2]) > 0 for _, parameters in timeout_statements)
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == baseline
        connection.commit()


def test_memory_statement_timeout_rolls_back_and_connection_recovers(isolated_postgres):
    import psycopg

    class SleepOnCountConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            return False

        def execute(self, query, parameters=()):
            if "SELECT count(*) FROM jarvis_app.memories" in query:
                return self.connection.execute("SELECT pg_sleep(2.0)")
            return self.connection.execute(query, parameters)

    with psycopg.connect(isolated_postgres.database_url) as connection:
        baseline = connection.execute("SHOW statement_timeout").fetchone()[0]
        connection.commit()
        wrapper = SleepOnCountConnection(connection)
        repository = PostgresMemoryRepository(
            isolated_postgres.database_url,
            connect=lambda *_args, **_kwargs: wrapper,
        )
        with pytest.raises(MemoryDeadlineExceeded):
            repository.search_with_deadline(
                TEST_OWNER_ID, "timeout integration", deadline=monotonic() + 1.5, limit=5
            )
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == baseline
        connection.commit()


def test_conversation_statement_timeout_rolls_back_and_connection_recovers(isolated_postgres):
    import psycopg

    conversation_repository = isolated_postgres.repository()
    conversation_id = conversation_repository.save_exchange("seed user", "seed assistant")

    class SlowMessageInsertConnection:
        def __init__(self, connection):
            self.connection = connection
            self.timeout_values = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            return False

        def execute(self, query, parameters=()):
            if "set_config('statement_timeout'" in query:
                self.timeout_values.append(parameters[0])
            if "INSERT INTO jarvis_app.messages" in query:
                return self.connection.execute("SELECT pg_sleep(3.0)")
            return self.connection.execute(query, parameters)

    with psycopg.connect(isolated_postgres.database_url) as connection:
        baseline = connection.execute("SHOW statement_timeout").fetchone()[0]
        connection.commit()
        wrapped_connection = SlowMessageInsertConnection(connection)
        timeout_repository = PostgresConversationRepository(
            isolated_postgres.database_url,
            connect_timeout_seconds=3,
            operation_timeout_seconds=2,
            connect=lambda *_args, **_kwargs: wrapped_connection,
        )

        with pytest.raises(PersistenceError):
            timeout_repository.save_exchange("must roll back", "must also roll back", conversation_id)

        assert wrapped_connection.timeout_values
        assert all(value.endswith("ms") and int(value[:-2]) > 0 for value in wrapped_connection.timeout_values)
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == baseline
        connection.commit()

    stored = conversation_repository.get_messages(conversation_id, limit=10)
    assert [item.content for item in stored] == ["seed user", "seed assistant"]
    conversation_repository.save_exchange("recovered user", "recovered assistant", conversation_id)
    recovered = conversation_repository.get_messages(conversation_id, limit=10)
    assert [item.content for item in recovered] == [
        "seed user", "seed assistant", "recovered user", "recovered assistant"
    ]


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


# --- Memory/context phase: source kinds, document de-duplication, context search ---


def test_memory_source_kind_upgrade_preserves_pre_0005_rows(isolated_postgres):
    """Exercise 0005 against rows stored before the source-kind column exists."""
    import psycopg
    from psycopg import sql

    generated_name = f"jarvis_it_{uuid4().hex}"
    assert GENERATED_DATABASE_RE.fullmatch(generated_name)
    config = runner.DatabaseConfig(
        host=isolated_postgres.migration_config.host,
        port=isolated_postgres.migration_config.port,
        database=generated_name,
        username=isolated_postgres.migration_config.username,
        maintenance_database="postgres",
        pg_bin_dir=isolated_postgres.migration_config.pg_bin_dir,
        pgpass_file=isolated_postgres.migration_config.pgpass_file,
        backup_dir=isolated_postgres.migration_config.backup_dir,
    )
    database_url = isolated_postgres.database_url.rsplit("/", 1)[0] + "/" + generated_name
    rows = [
        (UUID(int=7101), TEST_OWNER_ID, "Previously saved memory", "notes", None),
        (UUID(int=7102), UUID(int=7100), "Previously imported document", None, "legacy.md"),
        (UUID(int=7103), None, "Previously unowned memory", "legacy", None),
    ]
    created = False
    try:
        with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
            if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (generated_name,)).fetchone():
                pytest.fail("Generated source-kind upgrade database name unexpectedly exists.", pytrace=False)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(generated_name)))
            created = True

        with tempfile.TemporaryDirectory(prefix="jarvis-pre-0005-") as temporary:
            database_dir = Path(temporary)
            scripts = database_dir / "scripts"
            scripts.mkdir()
            for migration in sorted(runner.MIGRATIONS_DIR.glob("000[1234]_*.sql")):
                shutil.copy2(migration, scripts / migration.name)
            assert len(list(scripts.glob("*.sql"))) == 4
            if runner.install(config, database_dir=database_dir) != 0:
                pytest.fail("Could not initialize the disposable pre-0005 database.", pytrace=False)
            with psycopg.connect(database_url, connect_timeout=3) as connection:
                assert connection.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = 'jarvis_app' "
                    "AND table_name = 'memories' AND column_name = 'source_kind'"
                ).fetchone() is None
                for row in rows:
                    connection.execute(
                        "INSERT INTO jarvis_app.memories (id, owner_id, content, category, source_reference) "
                        "VALUES (%s, %s, %s, %s, %s)", row,
                    )
            shutil.copy2(runner.MIGRATIONS_DIR / "0005_memory_source_kind.sql", scripts)
            if runner.install(config, database_dir=database_dir) != 0:
                pytest.fail("Could not apply 0005 to the disposable upgrade database.", pytrace=False)
            assert runner.verify(config, database_dir=database_dir) == 0

        with psycopg.connect(database_url, connect_timeout=3) as connection:
            upgraded = connection.execute(
                "SELECT id, owner_id, content, category, source_reference, source_kind "
                "FROM jarvis_app.memories ORDER BY id"
            ).fetchall()
            assert upgraded == [(*row, "memory") for row in rows]
            assert connection.execute(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'jarvis_app' AND table_name = 'memories' AND column_name = 'source_kind'"
            ).fetchone() == ("NO", "'memory'::text")
            index = connection.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'jarvis_app' "
                "AND indexname = 'memories_owner_document_digest_idx'"
            ).fetchone()
            assert index is not None
            assert all(fragment in index[0] for fragment in ("owner_id", "md5(content)", "source_kind", "document", "IS NOT NULL"))
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        "INSERT INTO jarvis_app.memories (id, content, source_kind) VALUES (%s, %s, %s)",
                        (UUID(int=7104), "Invalid source kind", "unsupported"),
                    )
    except psycopg.Error as exc:
        pytest.fail(f"Disposable source-kind upgrade failed ({type(exc).__name__}); details suppressed.", pytrace=False)
    finally:
        if created:
            if not GENERATED_DATABASE_RE.fullmatch(generated_name):
                raise RuntimeError("Refusing cleanup without a generated integration-test name.")
            try:
                with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (generated_name,),
                    )
                    admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(generated_name)))
                    if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (generated_name,)).fetchone():
                        raise RuntimeError("generated database remains after drop")
            except Exception as exc:
                pytest.fail(
                    f"Could not complete or verify cleanup of generated upgrade database {generated_name} "
                    f"({type(exc).__name__}); details suppressed.", pytrace=False,
                )


def test_memory_source_kind_migration_defaults_existing_rows_to_memory(isolated_postgres):
    import psycopg

    legacy_owner = UUID("5f1e8c1e-6f43-4ab9-9a59-0d7a0f3f2c01")
    legacy_id = UUID("5f1e8c1e-6f43-4ab9-9a59-0d7a0f3f2c02")
    with psycopg.connect(isolated_postgres.database_url) as connection:
        connection.execute(
            "INSERT INTO jarvis_app.memories (id, owner_id, content) VALUES (%s, %s, %s)",
            (legacy_id, legacy_owner, "legacy import without a kind"),
        )
        kind = connection.execute(
            "SELECT source_kind FROM jarvis_app.memories WHERE id = %s", (legacy_id,)
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO jarvis_app.memories (id, owner_id, content, source_kind) VALUES (%s, %s, %s, %s)",
                (UUID("5f1e8c1e-6f43-4ab9-9a59-0d7a0f3f2c03"), legacy_owner, "bad kind", "secret"),
            )
    assert kind == "memory"


def test_document_import_deduplicates_per_owner_and_stays_distinct_from_memories(isolated_postgres):
    owner = UUID("2b9f4d1e-1111-4c4c-9a9a-000000000001")
    other_owner = UUID("2b9f4d1e-1111-4c4c-9a9a-000000000002")
    repository = isolated_postgres.memory_repository()

    first, created_first = repository.create_document(owner, "Backup runbook: run nightly.", "backup.md")
    again, created_again = repository.create_document(owner, "Backup runbook: run nightly.", "renamed.md")
    foreign, created_foreign = repository.create_document(other_owner, "Backup runbook: run nightly.", "backup.md")
    memory = repository.create(owner, "Backup runbook: run nightly.", category="ops")

    assert created_first is True and created_again is False and created_foreign is True
    assert again.id == first.id and again.source_reference == "backup.md"
    assert foreign.id != first.id
    assert first.source_kind == "document" and memory.source_kind == "memory"
    documents = repository.list(owner, limit=20, source_kind="document")
    memories = repository.list(owner, limit=20, source_kind="memory")
    assert [item.id for item in documents.items] == [first.id]
    assert [item.id for item in memories.items] == [memory.id]


def test_concurrent_identical_document_imports_store_one_record(isolated_postgres):
    owner = UUID("2b9f4d1e-1111-4c4c-9a9a-000000000003")
    repository = isolated_postgres.memory_repository()

    def import_once(_):
        return repository.create_document(owner, "Concurrent identical import.", "same.md")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(import_once, range(6)))

    assert sum(created for _, created in results) == 1
    assert len({record.id for record, _ in results}) == 1
    assert repository.list(owner, limit=20, source_kind="document").total == 1


def test_context_search_is_owner_scoped_prefix_matching_and_kind_filtered(isolated_postgres):
    from jarvis.memory.query import extract_query_terms
    from jarvis.memory.retrieval import retrieve_context

    owner = UUID("2b9f4d1e-1111-4c4c-9a9a-000000000004")
    foreign_owner = UUID("2b9f4d1e-1111-4c4c-9a9a-000000000005")
    repository = isolated_postgres.memory_repository()
    decision = repository.create(owner, "We chose PostgreSQL as the persistence database.", category="decisions")
    runbook, _ = repository.create_document(owner, "Database backups run nightly via backup_db.bat.", "runbook.md")
    repository.create(owner, "Coffee preferences.")
    repository.create(foreign_owner, "We chose PostgreSQL as the persistence database. FOREIGN_MARKER")

    terms = extract_query_terms("Which database did we choose for persistence?")
    candidates = repository.search_context(
        owner, terms, deadline=None, candidate_limit=40, source_kinds=("memory", "document"),
    )
    assert {item.id for item in candidates} == {decision.id, runbook.id}
    assert all("FOREIGN_MARKER" not in item.content for item in candidates)
    memories_only = repository.search_context(
        owner, terms, deadline=None, candidate_limit=40, source_kinds=("memory",),
    )
    assert [item.id for item in memories_only] == [decision.id]

    result = retrieve_context(
        repository, "Which database did we choose for persistence?", owner_id=owner,
        limit=3, document_limit=2, execution_id="integration",
    )
    assert result.outcome == "success"
    assert result.records[0].id == decision.id
    # The legacy all-words search still finds nothing for this chat-style query.
    assert repository.search(owner, "Which database did we choose for persistence?").total == 0


def test_chat_uses_live_history_and_reports_context_sources(isolated_postgres):
    with isolated_postgres.client() as client:
        first = client.post("/chat", json={"message": "Remember that the deploy window is Tuesday."})
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]
        second = client.post("/chat", json={"message": "When is the window?", "conversation_id": conversation_id})
        missing = client.post(
            "/chat", json={"message": "hello", "conversation_id": "00000000-0000-4000-8000-00000000abcd"}
        )
    assert second.status_code == 200
    context = second.json()["context"]
    assert context["history_status"] == "included"
    assert [source["source_type"] for source in context["sources"]] == ["conversation", "conversation"]
    assert "deploy window is Tuesday" in second.json()["reply"]  # mock echoes the rendered context
    assert missing.status_code == 404
