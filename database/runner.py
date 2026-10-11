"""Portable PostgreSQL provisioning, migration, verification, and backup tools."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


DATABASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = DATABASE_DIR / "scripts"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
MIGRATION_RE = re.compile(r"^(?P<id>[0-9]{4,}_[a-z0-9_]+)\.sql$")
Runner = Callable[..., subprocess.CompletedProcess[str]]


class SetupError(Exception):
    """An actionable setup failure whose message contains no credentials."""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    username: str
    maintenance_database: str
    pg_bin_dir: Path | None
    pgpass_file: Path
    backup_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "DatabaseConfig":
        required = ("JARVIS_DB_HOST", "JARVIS_DB_PORT", "JARVIS_DB_NAME", "JARVIS_DB_BOOTSTRAP_USER", "JARVIS_DB_MAINTENANCE_DB")
        missing = [name for name in required if not environ.get(name, "").strip()]
        if missing:
            raise SetupError("Set the PostgreSQL connection values in config\\db_config.local.bat or the process environment: " + ", ".join(missing) + ".")

        host = environ["JARVIS_DB_HOST"].strip()
        if not host or any(char.isspace() for char in host):
            raise SetupError("JARVIS_DB_HOST must be a hostname or IP address without whitespace.")

        port_value = environ["JARVIS_DB_PORT"].strip()
        try:
            port = int(port_value)
        except ValueError as exc:
            raise SetupError("JARVIS_DB_PORT must be an integer between 1 and 65535.") from exc
        if not 1 <= port <= 65535:
            raise SetupError("JARVIS_DB_PORT must be an integer between 1 and 65535.")

        database = environ["JARVIS_DB_NAME"].strip()
        username = environ["JARVIS_DB_BOOTSTRAP_USER"].strip()
        maintenance_database = environ["JARVIS_DB_MAINTENANCE_DB"].strip()
        for label, value in (("JARVIS_DB_NAME", database), ("JARVIS_DB_BOOTSTRAP_USER", username), ("JARVIS_DB_MAINTENANCE_DB", maintenance_database)):
            if not IDENTIFIER_RE.fullmatch(value):
                raise SetupError(f"{label} must be a PostgreSQL identifier using letters, digits, and underscores (maximum 63 characters).")

        pg_bin = environ.get("JARVIS_PG_BIN_DIR", "").strip()
        pg_bin_dir = Path(pg_bin).expanduser() if pg_bin else None
        if pg_bin_dir is not None and not pg_bin_dir.is_dir():
            raise SetupError("JARVIS_PG_BIN_DIR does not point to an existing directory.")

        app_data = environ.get("APPDATA", "").strip()
        default_passfile = Path(app_data) / "postgresql" / "pgpass.conf" if app_data else Path.home() / "AppData" / "Roaming" / "postgresql" / "pgpass.conf"
        passfile_value = environ.get("PGPASSFILE", "").strip()
        pgpass_file = Path(passfile_value).expanduser() if passfile_value else default_passfile

        user_profile = environ.get("USERPROFILE", "").strip()
        default_backup_dir = (Path(user_profile) if user_profile else Path.home()) / "Documents" / "JARVIS-Backups"
        backup_value = environ.get("JARVIS_DB_BACKUP_DIR", "").strip()
        backup_dir = Path(backup_value).expanduser() if backup_value else default_backup_dir

        return cls(host, port, database, username, maintenance_database, pg_bin_dir, pgpass_file, backup_dir)


def _base_env(config: DatabaseConfig, environ: Mapping[str, str]) -> dict[str, str]:
    env = dict(environ)
    env.pop("PGPASSWORD", None)
    env.pop("PGSERVICE", None)
    env.pop("PGSERVICEFILE", None)
    env["PGPASSFILE"] = str(config.pgpass_file)
    return env


def _find_tool(name: str, config: DatabaseConfig, environ: Mapping[str, str]) -> str | None:
    executable = f"{name}.exe" if os.name == "nt" else name
    if config.pg_bin_dir is not None:
        candidate = config.pg_bin_dir / executable
        return str(candidate) if candidate.is_file() else None
    return shutil.which(executable, path=environ.get("PATH"))


def _require_tools(names: Sequence[str], config: DatabaseConfig, environ: Mapping[str, str]) -> dict[str, str]:
    tools = {name: _find_tool(name, config, environ) for name in names}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        joined = ", ".join(missing)
        raise SetupError(
            f"Required PostgreSQL client tool(s) not found: {joined}. Install PostgreSQL client tools, add their bin directory to PATH, or set JARVIS_PG_BIN_DIR."
        )
    return {name: path for name, path in tools.items() if path is not None}


def _validate_credentials_file(config: DatabaseConfig) -> None:
    if not config.pgpass_file.is_file():
        raise SetupError(
            f"PostgreSQL password file not found at {config.pgpass_file}. Configure PGPASSFILE or create the standard Windows pgpass.conf file; passwords are never accepted in this setup config."
        )


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    runner: Runner,
    step: str,
    discard_stdout: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        run_options = {"text": True, "check": False, "env": dict(env), "timeout": 120}
        if discard_stdout:
            run_options.update(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            run_options["capture_output"] = True
        result = runner(list(command), **run_options)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"{step} could not be started. Check that PostgreSQL client tools are installed and accessible.") from exc
    if result.returncode != 0:
        raise SetupError(f"{step} failed (exit code {result.returncode}). Check PostgreSQL service status, configured access, and required privileges. Password values and raw client output are suppressed.")
    return result


def _psql_args(tool: str, config: DatabaseConfig, database: str, *, query: str | None = None, file: Path | None = None) -> list[str]:
    args = [
        tool,
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--no-password",
        "--host", config.host,
        "--port", str(config.port),
        "--username", config.username,
        "--dbname", database,
    ]
    if query is not None:
        args += ["--quiet", "--tuples-only", "--no-align", "--command", query]
    if file is not None:
        args += ["--single-transaction", "--file", str(file)]
    return args


def _database_state(config: DatabaseConfig, psql: str, *, env: Mapping[str, str], runner: Runner) -> tuple[bool, bool]:
    db_literal = config.database.replace("'", "''")
    query = (
        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '" + db_literal + "')::text || '|' || "
        "(CASE WHEN EXISTS (SELECT 1 FROM pg_database WHERE datname = '" + db_literal + "') "
        "THEN has_database_privilege(current_user, '" + db_literal + "', 'CREATE') "
        "ELSE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND (rolsuper OR rolcreatedb)) END)::text;"
    )
    result = _run(_psql_args(psql, config, config.maintenance_database, query=query), env=env, runner=runner, step="PostgreSQL connectivity and permission check")
    fields = result.stdout.strip().split("|")
    if len(fields) != 2 or any(value not in {"t", "f", "true", "false"} for value in fields):
        raise SetupError("PostgreSQL returned an unexpected database/permission check result.")
    return fields[0] in {"t", "true"}, fields[1] in {"t", "true"}


def _migration_files(directory: Path) -> list[tuple[str, Path, bytes, str]]:
    try:
        candidates = sorted(directory.glob("*.sql"), key=lambda item: item.name)
    except OSError as exc:
        raise SetupError("Could not read the database migration directory.") from exc
    migrations: list[tuple[str, Path, bytes, str]] = []
    seen: set[str] = set()
    for path in candidates:
        match = MIGRATION_RE.fullmatch(path.name)
        if not match:
            raise SetupError(f"Migration filename is invalid: {path.name}. Use NNNN_lowercase_name.sql.")
        migration_id = match.group("id")
        if migration_id in seen:
            raise SetupError(f"Duplicate migration identifier: {migration_id}.")
        seen.add(migration_id)
        try:
            contents = path.read_bytes()
        except OSError as exc:
            raise SetupError(f"Could not read migration file {path.name}.") from exc
        if not contents.strip():
            raise SetupError(f"Migration file is empty: {path.name}.")
        if any(line.lstrip().startswith(b"\\") for line in contents.splitlines()):
            raise SetupError(f"Migration {path.name} uses a psql meta-command. Only SQL statements are allowed.")
        normalized = contents.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        migrations.append((migration_id, path, normalized, hashlib.sha256(normalized).hexdigest()))
    return migrations


def _ledger_exists(config: DatabaseConfig, psql: str, *, env: Mapping[str, str], runner: Runner) -> bool:
    result = _run(
        _psql_args(psql, config, config.database, query="SELECT to_regclass('jarvis_meta.schema_migrations') IS NOT NULL;"),
        env=env,
        runner=runner,
        step="Migration ledger check",
    )
    value = result.stdout.strip()
    if value not in {"t", "f"}:
        raise SetupError("PostgreSQL returned an unexpected migration ledger check result.")
    return value == "t"


def _validate_ledger_schema(config: DatabaseConfig, psql: str, *, env: Mapping[str, str], runner: Runner) -> None:
    query = (
        "SELECT c.relkind::text || '|' || string_agg(a.attname::text || ':' || t.typname::text, '|' ORDER BY a.attnum) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid JOIN pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = 'jarvis_meta' AND c.relname = 'schema_migrations' "
        "AND a.attnum > 0 AND NOT a.attisdropped GROUP BY c.relkind;"
    )
    result = _run(
        _psql_args(psql, config, config.database, query=query),
        env=env,
        runner=runner,
        step="Migration ledger compatibility check",
    )
    columns = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    expected = ["r|migration_id:text|checksum_sha256:bpchar|applied_at:timestamptz"]
    if columns != expected:
        raise SetupError("The migration ledger has an incompatible column layout; it was left unchanged.")


def _read_ledger(config: DatabaseConfig, psql: str, *, env: Mapping[str, str], runner: Runner) -> dict[str, str]:
    result = _run(
        _psql_args(psql, config, config.database, query="SELECT migration_id || '|' || checksum_sha256 FROM jarvis_meta.schema_migrations ORDER BY migration_id;"),
        env=env,
        runner=runner,
        step="Migration ledger compatibility check",
    )
    applied: dict[str, str] = {}
    for row in result.stdout.splitlines():
        if not row.strip():
            continue
        fields = row.strip().split("|")
        if len(fields) != 2 or not re.fullmatch(r"[0-9]{4,}_[a-z0-9_]+", fields[0]) or not re.fullmatch(r"[0-9a-f]{64}", fields[1]):
            raise SetupError("The migration ledger contains an invalid row; setup stopped without changing it.")
        if fields[0] in applied:
            raise SetupError("The migration ledger contains a duplicate identifier; setup stopped without changing it.")
        applied[fields[0]] = fields[1]
    return applied


def _migration_script(contents: bytes, migration_id: str, checksum: str) -> bytes:
    tail = (
        "\n\n-- Record this migration in the same transaction as its schema changes.\n"
        "INSERT INTO jarvis_meta.schema_migrations (migration_id, checksum_sha256) VALUES ('"
        + migration_id
        + "', '"
        + checksum
        + "');\n"
    )
    return contents + tail.encode("ascii")


def _apply_migrations(
    config: DatabaseConfig,
    psql: str,
    *,
    env: Mapping[str, str],
    runner: Runner,
    directory: Path = MIGRATIONS_DIR,
) -> list[str]:
    migrations = _migration_files(directory)
    if not migrations:
        raise SetupError("No numbered SQL migrations were found.")
    ledger_exists = _ledger_exists(config, psql, env=env, runner=runner)
    if ledger_exists:
        _validate_ledger_schema(config, psql, env=env, runner=runner)
    applied = _read_ledger(config, psql, env=env, runner=runner) if ledger_exists else {}
    local_by_id = {migration_id: (path, contents, checksum) for migration_id, path, contents, checksum in migrations}

    unknown = sorted(set(applied) - set(local_by_id))
    if unknown:
        raise SetupError("The database has migration(s) not present in this checkout: " + ", ".join(unknown) + ". Update the project before installing.")
    for migration_id, checksum in applied.items():
        if local_by_id[migration_id][2] != checksum:
            raise SetupError(f"Applied migration {migration_id} differs from the checked-in file. Add a new migration; do not edit deployed migrations.")

    newly_applied: list[str] = []
    for migration_id, path, contents, checksum in migrations:
        if migration_id in applied:
            print(f"Skipped already-applied migration {migration_id}.")
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-db-migration-") as temp_dir:
                temp_script = Path(temp_dir) / path.name
                temp_script.write_bytes(_migration_script(contents, migration_id, checksum))
                _run(
                    _psql_args(psql, config, config.database, file=temp_script),
                    env=env,
                    runner=runner,
                    step=f"Migration {path.name}",
                )
        except OSError as exc:
            raise SetupError(f"Could not stage migration {path.name} in the temporary directory.") from exc
        print(f"Applied migration {migration_id}.")
        newly_applied.append(migration_id)
    return newly_applied


def _verify_schema(config: DatabaseConfig, psql: str, *, env: Mapping[str, str], runner: Runner, directory: Path = MIGRATIONS_DIR) -> list[str]:
    exists, _ = _database_state(config, psql, env=env, runner=runner)
    if not exists:
        raise SetupError("The configured PostgreSQL database does not exist. Run install_db.bat first.")
    if not _ledger_exists(config, psql, env=env, runner=runner):
        raise SetupError("The database is reachable, but jarvis_meta.schema_migrations is missing. Run install_db.bat to apply the metadata migration.")
    _validate_ledger_schema(config, psql, env=env, runner=runner)
    applied = _read_ledger(config, psql, env=env, runner=runner)
    migrations = _migration_files(directory)
    local_by_id = {migration_id: checksum for migration_id, _, _, checksum in migrations}
    unknown = sorted(set(applied) - set(local_by_id))
    if unknown:
        raise SetupError("The database contains migration(s) absent from this checkout: " + ", ".join(unknown) + ". Use a matching project version.")
    for migration_id, checksum in applied.items():
        if local_by_id[migration_id] != checksum:
            raise SetupError(f"Migration checksum mismatch for {migration_id}.")
    missing = sorted(set(local_by_id) - set(applied))
    if missing:
        raise SetupError("Required migration(s) have not been applied: " + ", ".join(missing) + ". Run install_db.bat.")
    if not applied:
        raise SetupError("The migration ledger exists but contains no applied migrations.")
    return sorted(applied)


def install(
    config: DatabaseConfig,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    database_dir: Path = DATABASE_DIR,
) -> int:
    env_source = dict(os.environ if environ is None else environ)
    try:
        tools = _require_tools(("psql", "createdb"), config, env_source)
        _validate_credentials_file(config)
        command_env = _base_env(config, env_source)
        exists, permission = _database_state(config, tools["psql"], env=command_env, runner=runner)
        if not permission:
            needed = "CREATEDB (or superuser)" if not exists else "CREATE privilege on the configured database"
            raise SetupError(f"The configured bootstrap role lacks {needed}; no database changes were made.")
        if exists:
            print(f"Database {config.database} already exists; preserving it and its data.")
        else:
            _run(
                [tools["createdb"], "--no-password", "--host", config.host, "--port", str(config.port), "--username", config.username, "--maintenance-db", config.maintenance_database, config.database],
                env=command_env,
                runner=runner,
                step=f"Creating database {config.database}",
            )
            print(f"Created database {config.database}.")
        _apply_migrations(config, tools["psql"], env=command_env, runner=runner, directory=database_dir / "scripts")
        applied = _verify_schema(config, tools["psql"], env=command_env, runner=runner, directory=database_dir / "scripts")
        print("Database verification succeeded. Applied migrations: " + ", ".join(applied))
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def verify(
    config: DatabaseConfig,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    database_dir: Path = DATABASE_DIR,
) -> int:
    env_source = dict(os.environ if environ is None else environ)
    try:
        tools = _require_tools(("psql",), config, env_source)
        _validate_credentials_file(config)
        applied = _verify_schema(config, tools["psql"], env=_base_env(config, env_source), runner=runner, directory=database_dir / "scripts")
        print(f"Database {config.database} is reachable and its migration metadata matches this checkout.")
        print("Applied migrations: " + ", ".join(applied))
        return 0
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def backup(
    config: DatabaseConfig,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    database_dir: Path = DATABASE_DIR,
    now: datetime | None = None,
) -> int:
    env_source = dict(os.environ if environ is None else environ)
    backup_path: Path | None = None
    try:
        tools = _require_tools(("psql", "pg_dump", "pg_restore"), config, env_source)
        _validate_credentials_file(config)
        command_env = _base_env(config, env_source)
        _verify_schema(config, tools["psql"], env=command_env, runner=runner, directory=database_dir / "scripts")
        config.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        backup_path = config.backup_dir / f"{config.database}_{timestamp}.dump"
        backup_path.touch(exist_ok=False)
        _run(
            [tools["pg_dump"], "--no-password", "--format=custom", "--file", str(backup_path), "--host", config.host, "--port", str(config.port), "--username", config.username, "--dbname", config.database],
            env=command_env,
            runner=runner,
            step="PostgreSQL backup",
        )
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise SetupError("PostgreSQL backup produced an empty or missing file; the backup is invalid.")
        _run([tools["pg_restore"], "--list", str(backup_path)], env=command_env, runner=runner, step="Backup archive validation", discard_stdout=True)
        print(f"Backup created and archive listing validated: {backup_path}")
        return 0
    except FileExistsError:
        print("ERROR: A backup with this timestamp already exists; no existing file was overwritten.", file=sys.stderr)
        return 1
    except (OSError, SetupError) as exc:
        if backup_path is not None and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        message = str(exc) if isinstance(exc, SetupError) else "The backup output directory or file could not be accessed."
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone JARVIS PostgreSQL setup tools")
    parser.add_argument("action", choices=("install", "verify", "backup"))
    args = parser.parse_args(argv)
    try:
        config = DatabaseConfig.from_env(os.environ)
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    actions = {"install": install, "verify": verify, "backup": backup}
    return actions[args.action](config)


if __name__ == "__main__":
    raise SystemExit(main())
