"""Shared fixtures for the opt-in live PostgreSQL suites.

The session-scoped ``isolated_postgres`` fixture lives here so every module in
``integration_tests`` shares ONE generated, migrated, and finally dropped
database per run (defining it in a test module made each importing module
create its own database).
"""

import os
import warnings
from pathlib import Path
from urllib.parse import unquote, urlunsplit
from uuid import uuid4

import pytest

from database import runner
from test_postgres_integration import (
    ADMIN_URL,
    GENERATED_DATABASE_RE,
    RUN_INTEGRATION,
    IsolatedPostgres,
    _classify_preexisting_generated_database,
    parse_safe_admin_url,
)


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
            # A process interruption can leave a generated database behind.
            # Catalog presence and connection counts cannot prove staleness, so
            # only report such databases; never clean them up automatically.
            existing_generated = admin.execute(
                "SELECT d.datname, COUNT(a.pid)::integer "
                "FROM pg_database AS d "
                "LEFT JOIN pg_stat_activity AS a "
                "ON a.datid = d.oid AND a.pid <> pg_backend_pid() "
                "WHERE d.datname ~ %s AND d.datname <> %s "
                "GROUP BY d.datname",
                (GENERATED_DATABASE_RE.pattern, integration_db),
            ).fetchall()
            classifications = [
                _classify_preexisting_generated_database(name, count)
                for name, count in existing_generated
            ]
            active_count = classifications.count("active")
            uncertain_count = classifications.count("uncertain")
            observed_count = active_count + uncertain_count
            if observed_count:
                warnings.warn(
                    "Found pre-existing generated PostgreSQL integration database(s) "
                    f"requiring manual review (active connections: {active_count}; "
                    f"inactive or uncertain: {uncertain_count}). The fixture will not "
                    "modify or remove them; no active connections does not establish staleness.",
                    pytest.PytestWarning,
                    stacklevel=2,
                )
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
                    remaining = admin.execute(
                        "SELECT 1 FROM pg_database WHERE datname = %s", (integration_db,)
                    ).fetchone()
                    if remaining is not None:
                        raise RuntimeError("generated database remains after drop")
            except Exception as exc:
                pytest.fail(
                    f"Could not complete or verify cleanup of generated isolated integration database "
                    f"{integration_db} ({type(exc).__name__}); credentials and connection details were suppressed.",
                    pytrace=False,
                )
