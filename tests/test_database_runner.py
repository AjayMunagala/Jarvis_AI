from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from database import runner


SECRET = "do-not-print-this-password"
MIGRATION_ID = "0001_migration_metadata"


def make_config(tmp_path: Path) -> runner.DatabaseConfig:
    passfile = tmp_path / "protected pgpass.conf"
    passfile.write_text(f"localhost:5432:jarvis_AI:postgre:{SECRET}\n", encoding="utf-8")
    return runner.DatabaseConfig(
        host="localhost",
        port=5432,
        database="jarvis_AI",
        username="postgre",
        maintenance_database="postgres",
        pg_bin_dir=None,
        pgpass_file=passfile,
        backup_dir=tmp_path / "backup output",
    )


def migration_checksum(directory: Path, migration_id: str = MIGRATION_ID) -> str:
    return next(checksum for found_id, _, _, checksum in runner._migration_files(directory) if found_id == migration_id)


def applied_migrations(directory: Path = runner.MIGRATIONS_DIR) -> dict[str, str]:
    return {migration_id: checksum for migration_id, _, _, checksum in runner._migration_files(directory)}


def database_runner(*, exists: bool = True, permission: bool = True, ledger: bool = False, applied: dict[str, str] | None = None, fail_file: str | None = None):
    calls: list[list[str]] = []
    state = {"exists": exists, "permission": permission, "ledger": ledger, "applied": dict(applied or {})}

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "fake-createdb.exe":
            state["exists"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] != "fake-psql.exe":
            raise AssertionError(f"Unexpected executable: {command[0]}")
        if "--file" in command:
            filename = Path(command[command.index("--file") + 1]).name
            if filename == fail_file:
                return subprocess.CompletedProcess(command, 1, "", SECRET)
            temp_sql = Path(command[command.index("--file") + 1]).read_text(encoding="utf-8")
            match = re.search(r"VALUES \('([0-9]{4,}_[a-z0-9_]+)', '([0-9a-f]{64})'\);", temp_sql)
            assert match
            state["applied"][match.group(1)] = match.group(2)
            state["ledger"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        query = command[command.index("--command") + 1]
        if "FROM pg_database" in query:
            output = f"{'true' if state['exists'] else 'false'}|{'true' if state['permission'] else 'false'}\n"
        elif "to_regclass" in query:
            output = f"{'t' if state['ledger'] else 'f'}\n"
        elif "pg_class c" in query:
            output = "r|migration_id:text|checksum_sha256:bpchar|applied_at:timestamptz\n"
        elif "migration_id || '|' || checksum_sha256" in query:
            output = "".join(f"{key}|{value}\n" for key, value in sorted(state["applied"].items()))
        else:
            raise AssertionError(f"Unexpected SQL: {query}")
        return subprocess.CompletedProcess(command, 0, output, "")

    return calls, run


def fake_tool_lookup(monkeypatch):
    monkeypatch.setattr(runner, "_require_tools", lambda names, config, environ: {name: f"fake-{name}.exe" for name in names})


def test_configuration_rejects_invalid_port_and_database_identifier():
    base = {
        "JARVIS_DB_HOST": "127.0.0.1",
        "JARVIS_DB_PORT": "5432",
        "JARVIS_DB_NAME": "jarvis_AI",
        "JARVIS_DB_BOOTSTRAP_USER": "postgre",
        "JARVIS_DB_MAINTENANCE_DB": "postgres",
    }
    with pytest.raises(runner.SetupError, match="JARVIS_DB_PORT"):
        runner.DatabaseConfig.from_env({**base, "JARVIS_DB_PORT": "70000"})
    with pytest.raises(runner.SetupError, match="JARVIS_DB_NAME"):
        runner.DatabaseConfig.from_env({**base, "JARVIS_DB_NAME": "jarvis; DROP DATABASE"})
    with pytest.raises(runner.SetupError, match="JARVIS_DB_HOST"):
        runner.DatabaseConfig.from_env({})


def test_missing_postgresql_tools_are_reported(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    monkeypatch.setattr(runner.shutil, "which", lambda executable, path=None: None)
    with pytest.raises(runner.SetupError, match="psql, createdb"):
        runner._require_tools(("psql", "createdb"), config, {"PATH": ""})


def test_migrations_are_sorted_and_reject_psql_meta_commands(tmp_path):
    scripts = tmp_path / "scripts with spaces"
    scripts.mkdir()
    (scripts / "0002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (scripts / "0001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    files = runner._migration_files(scripts)
    assert [item[0] for item in files] == ["0001_first", "0002_second"]
    first_hash = files[0][3]
    (scripts / "0001_first.sql").write_bytes(b"SELECT 1;\r\n")
    assert runner._migration_files(scripts)[0][3] == first_hash

    (scripts / "0003_unsafe.sql").write_text("\\! whoami\n", encoding="utf-8")
    with pytest.raises(runner.SetupError, match="meta-command"):
        runner._migration_files(scripts)


def test_existing_database_is_preserved_and_applied_migration_is_skipped(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    scripts = Path(runner.MIGRATIONS_DIR)
    calls, fake_run = database_runner(ledger=True, applied=applied_migrations(scripts))
    fake_tool_lookup(monkeypatch)

    status = runner.install(config, environ={"PATH": ""}, runner=fake_run, database_dir=runner.DATABASE_DIR)

    assert status == 0
    assert not any(call[0] == "fake-createdb.exe" for call in calls)
    assert not any("--file" in call for call in calls)
    assert "already exists; preserving it" in capsys.readouterr().out
    assert all("DROP DATABASE" not in " ".join(call).upper() for call in calls)


def test_missing_database_is_created_and_installation_returns_zero(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    checksum = migration_checksum(runner.MIGRATIONS_DIR)
    calls, fake_run = database_runner(exists=False, ledger=True, applied={MIGRATION_ID: checksum})
    fake_tool_lookup(monkeypatch)

    status = runner.install(config, environ={"PATH": ""}, runner=fake_run)

    assert status == 0
    assert sum(call[0] == "fake-createdb.exe" for call in calls) == 1


def test_failed_migration_stops_before_following_migrations(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "0001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (scripts / "0002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    calls, fake_run = database_runner(ledger=False, fail_file="0001_first.sql")
    fake_tool_lookup(monkeypatch)
    temp_dir_factory = tempfile.TemporaryDirectory
    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", lambda prefix: temp_dir_factory(prefix=prefix, dir=tmp_path))

    status = runner.install(config, environ={"PATH": ""}, runner=fake_run, database_dir=tmp_path)

    migration_calls = [call for call in calls if "--file" in call]
    assert status == 1
    assert len(migration_calls) == 1
    assert "Migration 0001_first.sql failed" in capsys.readouterr().err


def test_existing_database_without_create_privilege_is_left_unchanged(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    calls, fake_run = database_runner(exists=True, permission=False, ledger=False)
    fake_tool_lookup(monkeypatch)

    assert runner.install(config, environ={"PATH": ""}, runner=fake_run) == 1
    assert not any(call[0] == "fake-createdb.exe" or "--file" in call for call in calls)
    assert "CREATE privilege" in capsys.readouterr().err


def test_install_failure_does_not_print_password(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    fake_tool_lookup(monkeypatch)

    commands = []

    def failed_connection(command, **kwargs):
        commands.append(command)
        assert "PGPASSWORD" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 2, "", SECRET)

    assert runner.install(config, environ={"PATH": "", "PGPASSWORD": SECRET}, runner=failed_connection) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "raw client output are suppressed" in captured.err
    assert SECRET not in " ".join(" ".join(call) for call in commands)


def test_verify_rejects_missing_or_incompatible_schema(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    fake_tool_lookup(monkeypatch)
    calls, missing_ledger = database_runner(ledger=False)
    assert runner.verify(config, environ={"PATH": ""}, runner=missing_ledger) == 1
    assert "schema_migrations is missing" in capsys.readouterr().err

    def wrong_schema(command, **kwargs):
        if command[0] == "fake-psql.exe":
            query = command[command.index("--command") + 1]
            if "FROM pg_database" in query:
                return subprocess.CompletedProcess(command, 0, "true|true\n", "")
            if "to_regclass" in query:
                return subprocess.CompletedProcess(command, 0, "t\n", "")
            if "pg_class c" in query:
                return subprocess.CompletedProcess(command, 0, "v|migration_id:text|checksum_sha256:bpchar|applied_at:timestamptz\n", "")
        raise AssertionError("Unexpected database command")

    assert runner.verify(config, environ={"PATH": ""}, runner=wrong_schema) == 1
    assert "incompatible column layout" in capsys.readouterr().err


def test_runner_paths_are_derived_from_checkout_not_c_drive():
    assert runner.DATABASE_DIR == Path(__file__).resolve().parents[1] / "database"
    assert runner.MIGRATIONS_DIR.parent == runner.DATABASE_DIR


def test_password_file_is_required_and_backup_does_not_overwrite(monkeypatch, tmp_path, capsys):
    config = make_config(tmp_path)
    fake_tool_lookup(monkeypatch)
    config.pgpass_file.unlink()
    assert runner.verify(config, environ={"PATH": ""}) == 1
    assert "password file not found" in capsys.readouterr().err

    config = make_config(tmp_path)
    config.backup_dir.mkdir()
    existing = config.backup_dir / "jarvis_AI_20261011_120000.dump"
    existing.write_bytes(b"keep")
    fixed_now = runner.datetime(2026, 10, 11, 12, 0, 0)
    _, fake_run = database_runner(ledger=True, applied=applied_migrations())
    fake_tool_lookup(monkeypatch)
    assert runner.backup(config, environ={"PATH": ""}, runner=fake_run, now=fixed_now) == 1
    assert existing.read_bytes() == b"keep"
    assert "already exists" in capsys.readouterr().err


def test_backup_writes_timestamped_archive_and_validates_it(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    _, base_runner = database_runner(ledger=True, applied=applied_migrations())
    fake_tool_lookup(monkeypatch)
    now = runner.datetime(2026, 10, 11, 12, 34, 56)

    def fake_run(command, **kwargs):
        if command[0] == "fake-pg_dump.exe":
            Path(command[command.index("--file") + 1]).write_bytes(b"validated by fake archive reader")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "fake-pg_restore.exe":
            assert command[1:2] == ["--list"]
            return subprocess.CompletedProcess(command, 0, "archive contents", "")
        return base_runner(command, **kwargs)

    assert runner.backup(config, environ={"PATH": ""}, runner=fake_run, now=now) == 0
    archive = config.backup_dir / "jarvis_AI_20261011_123456.dump"
    assert archive.read_bytes() == b"validated by fake archive reader"
