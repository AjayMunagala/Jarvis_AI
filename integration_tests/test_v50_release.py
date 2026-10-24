"""v5.0 release readiness (live PostgreSQL): fresh install, upgrade from the v4.0 schema, idempotent re-install."""

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from database import runner
from test_postgres_integration import TEST_OWNER_ID


def _scratch_config(isolated_postgres, name):
    base = isolated_postgres.migration_config
    return runner.DatabaseConfig(host=base.host, port=base.port, database=name, username=base.username,
                                 maintenance_database="postgres", pg_bin_dir=base.pg_bin_dir,
                                 pgpass_file=base.pgpass_file, backup_dir=base.backup_dir)


def _ledger(url):
    with psycopg.connect(url) as connection:
        return [row[0] for row in connection.execute(
            "SELECT migration_id FROM jarvis_meta.schema_migrations ORDER BY migration_id")]


def test_fresh_install_applies_every_local_migration_and_verifies(isolated_postgres):
    local = sorted(migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR))
    assert _ledger(isolated_postgres.database_url) == local
    assert runner.verify(isolated_postgres.migration_config) == 0


def test_upgrade_from_the_v40_schema_keeps_data_and_is_idempotent(isolated_postgres):
    name = f"jarvis_it_{uuid4().hex}"
    config = _scratch_config(isolated_postgres, name)
    url = isolated_postgres.database_url.rsplit("/", 1)[0] + "/" + name
    created = False
    try:
        with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="jarvis-v40-migrations-") as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            for migration in sorted(runner.MIGRATIONS_DIR.glob("00[0-5][0-9]_*.sql")):  # everything up to 0059
                shutil.copy2(migration, scripts / migration.name)
            assert runner.install(config, database_dir=Path(temporary)) == 0
        assert _ledger(url)[-1] == "0059_audit_orchestration_category"
        doc_id, chunk_id = uuid4(), uuid4()
        with psycopg.connect(url) as connection:  # data written by a v4.0 installation
            connection.execute(
                """INSERT INTO jarvis_app.approval_requests (id, owner_id, category, action, target, reason, risk,
                       expected_side_effects, scope_sha256, created_at, expires_at, state)
                   VALUES (%s, %s, 'tool', 'files.write', 't', 'r', 'high', 'e', %s, now(), now() + interval '1 hour',
                           'pending')""", ("AR-" + "3" * 16, TEST_OWNER_ID, "c" * 64))
            connection.execute(
                """INSERT INTO jarvis_app.knowledge_documents (id, owner_id, title, source_type, content, metadata)
                   VALUES (%s, %s, 'Old doc', 'manual', 'old content', '{}'::jsonb)""", (doc_id, TEST_OWNER_ID))
            connection.execute(
                """INSERT INTO jarvis_app.knowledge_chunks (id, document_id, owner_id, chunk_index, content, token_count)
                   VALUES (%s, %s, %s, 0, 'old content', 3)""", (chunk_id, doc_id, TEST_OWNER_ID))
        assert runner.install(config) == 0  # applies 0060-0066 only
        assert runner.verify(config) == 0
        local = sorted(migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR))
        assert _ledger(url) == local
        with psycopg.connect(url) as connection:
            approval = connection.execute("SELECT state, consumed_at FROM jarvis_app.approval_requests WHERE id = %s",
                                          ("AR-" + "3" * 16,)).fetchone()
            chunk = connection.execute("SELECT untrusted FROM jarvis_app.knowledge_chunks WHERE id = %s",
                                       (chunk_id,)).fetchone()
            tables = {row[0] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'jarvis_app'")}
        assert approval == ("pending", None) and chunk == (False,)
        assert {"integration_enrollments", "improvement_proposals", "improvement_snapshots",
                "improvement_target_contents"} <= tables
        assert runner.install(config) == 0  # a second install changes nothing
        assert _ledger(url) == local
    finally:
        if created:
            with psycopg.connect(isolated_postgres.admin_url, connect_timeout=3, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
