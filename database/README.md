# JARVIS PostgreSQL setup

This directory provides optional Windows tooling to prepare PostgreSQL and track schema migrations. Ordinary backend startup and tests do not need a database. Migration `0001` creates the ledger; `0002` adds the optional conversation/message schema; `0003` adds explicitly managed long-term memories. User accounts, agents, tools, audit storage, and other application tables remain out of scope.

## Requirements and configuration

- Windows 10 or 11 with `cmd.exe` and Python 3.11 or later available on `PATH`.
- PostgreSQL client tools from a compatible PostgreSQL installation: `psql.exe` and `createdb.exe` for installation; `psql.exe` for verification; `pg_dump.exe` and `pg_restore.exe` for backups.
- Network access from this machine to the configured PostgreSQL host and port.
- The optional FastAPI adapter requires `psycopg[binary]`, installed with `python -m pip install -e ".[persistence]"`.
- A local PostgreSQL password file. The runner uses `PGPASSFILE` if set, otherwise `%APPDATA%\postgresql\pgpass.conf`. It passes `--no-password` to PostgreSQL tools, so a missing or incorrect password file fails without prompting or exposing a password.

Copy `config\db_config.example.bat` to `config\db_config.local.bat` and adjust its values for the actual server. The example values (`localhost`, `5432`, database `jarvis_AI`, and bootstrap role `postgre`) are initial development settings only; this repository could not verify the server, port, role, or database. Confirm the host and port in pgAdmin or with your server administrator. The scripts require explicit host and port values; they do not silently assume a PostgreSQL port. You may instead set these environment variables before launching a script:

| Variable | Purpose | Example |
| --- | --- | --- |
| `JARVIS_DB_HOST` | PostgreSQL host name or IP | `localhost` |
| `JARVIS_DB_PORT` | PostgreSQL TCP port, 1–65535 | `5432` |
| `JARVIS_DB_NAME` | Target database identifier | `jarvis_AI` |
| `JARVIS_DB_BOOTSTRAP_USER` | Administrative setup role | `postgre` |
| `JARVIS_DB_MAINTENANCE_DB` | Existing database used for catalog checks and creation | `postgres` |
| `JARVIS_PG_BIN_DIR` | Optional directory containing the PostgreSQL executables | `C:\Program Files\PostgreSQL\18\bin` |
| `PGPASSFILE` | Password-file path consumed by libpq tools | `%APPDATA%\postgresql\pgpass.conf` |
| `JARVIS_DB_BACKUP_DIR` | Backup output folder | `%USERPROFILE%\Documents\JARVIS-Backups` |

The `.bat` files load `config\db_config.local.bat` when present. Tool lookup uses `PATH` unless `JARVIS_PG_BIN_DIR` is set; there are no hardcoded PostgreSQL installation paths. The installer checks required values, files, tools, server connectivity, and database privileges before applying migrations.

## Credential handling

Never put a password in a `.bat`, `.env`, SQL file, command argument, or Git-tracked file. PostgreSQL's `pgpass.conf` format is one line per connection:

```text
host:port:database:username:password
```

On Windows, store it under `%APPDATA%\postgresql\pgpass.conf` or set `PGPASSFILE` to a protected local file. Since setup connects to both the maintenance database and target, use `*` for the database field unless you add matching entries for each database. The host and port fields must match the configured connection. For example, after setting up the directory, edit the file locally and restrict its ACL:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\postgresql" | Out-Null
notepad "$env:APPDATA\postgresql\pgpass.conf"
icacls "$env:APPDATA\postgresql\pgpass.conf" /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

Use this row format in the file, replacing the final value locally:

```text
localhost:5432:*:postgre:REPLACE_WITH_LOCAL_PASSWORD
```

Do not paste a real credential into this documentation. The scripts and runner never read or print the password file contents. The repository ignores the local database config, project-local pgpass file, and backup output directory. If a password was exposed in a shared conversation and is reused elsewhere, rotate it.

The bootstrap role is used only by these setup scripts. No application role is created by this foundation. For application use, provision a separate least-privilege role and grant only the operations required by the deployed API on `jarvis_app`, `conversations`, `messages`, and `memories`, then configure its URL independently. Memory CRUD requires `SELECT, INSERT, UPDATE, DELETE` on `memories`; chat/history requires `SELECT, INSERT` on `conversations` and `messages`. For example, run the following as an authorized database owner after replacing the role name:

```sql
GRANT USAGE ON SCHEMA jarvis_app TO jarvis_app_role;
GRANT SELECT, INSERT ON jarvis_app.conversations, jarvis_app.messages TO jarvis_app_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON jarvis_app.memories TO jarvis_app_role;
```

Neither adapter needs schema DDL privileges. Conversation persistence uses insert/select on conversations and messages; the memory adapter needs select/insert/update/delete on memories for the exposed CRUD endpoints.

## Files and execution order

```text
database/
  README.md
  runner.py
  install_db.bat
  verify_db.bat
  backup_db.bat
  config/db_config.example.bat
  scripts/0001_migration_metadata.sql
  scripts/0002_conversation_persistence.sql
  scripts/0003_long_term_memories.sql
  migrations/README.md
```

Migration files are sorted by their numeric identifier. `0001_migration_metadata.sql` creates `jarvis_meta.schema_migrations`. `0002_conversation_persistence.sql` creates `jarvis_app.conversations` and `jarvis_app.messages`, with role/content constraints, a foreign key, and ordered-message index. `0003_long_term_memories.sql` creates `jarvis_app.memories` and its listing/full-text indexes. The migrations require schema-creation permission. There are no placeholder SQL files.

## Install or update

From the repository root, run:

```cmd
database\install_db.bat
```

From another working directory, call the batch file using its full path.

The installer connects to the configured maintenance database, checks reachability and required privileges, and checks whether the target database exists. It creates the database only if it is missing. If it already exists, the installer does not drop, truncate, recreate, or overwrite its data; it applies only pending numbered migrations. Each migration and its ledger record run in one PostgreSQL transaction with `ON_ERROR_STOP`. A failed migration stops the run and does not record that migration as applied. The command returns non-zero on failure.

On a newly created database, the configured bootstrap role owns the database. On an existing database, that role needs `CREATE` privilege to add the migration schema/table. The role also needs `CREATEDB` (or superuser) only when the target database does not yet exist. The application itself is not granted privileges by this foundation.

The migration ledger prevents successful one-time migrations from running again. The runner checks migration checksums over normalized LF line endings so Windows CRLF checkouts have the same hash, rejects changed already-applied files, and rejects ledger entries not present in the current checkout. If a run fails, read the reported failed migration identifier, fix the connection or prerequisite, then run `database\verify_db.bat` followed by `database\install_db.bat`. Do not edit an already deployed migration to repair it; add a new ordered migration after diagnosing the state. Migration changes are not automatically undone.

## Verify

```cmd
database\verify_db.bat
```

Verification checks the configured server and target database, confirms the migration ledger has the expected column layout, compares recorded checksums with the migrations in this checkout, and prints applied identifiers. It returns non-zero if the database or required migration metadata is missing or incompatible. It verifies migration ledger/checksum state; it does not introspect all application table constraints.

## Opt-in PostgreSQL integration tests

The live integration suite is separate from the default offline test suite at `integration_tests/test_postgres_integration.py`. It is skipped unless explicitly enabled. It uses the declared `persistence` extra and the existing migration runner, and creates a uniquely named `jarvis_it_<random hex>` database on a local loopback PostgreSQL server. The generated database is dropped during fixture cleanup. Do not point this suite at production or a database containing user data. The configured bootstrap account must be able to create databases and apply migrations. Tests cover migration checksums and reruns, chat persistence/history paging, unknown conversation IDs, transaction rollback/recovery, concurrent appends, and sanitized connection failures.

From PowerShell at the repository root, install the optional adapter dependency if it is not already installed and configure a protected `PGPASSFILE` with a matching local account entry. Do not put a password in the URL or command line:

```powershell
python -m pip install -e ".[persistence]"
$env:PGPASSFILE = "$env:APPDATA\postgresql\pgpass.conf"
$env:JARVIS_TEST_POSTGRES_ADMIN_URL = "postgresql://YOUR_LOCAL_ADMIN@localhost:5432/postgres"
$env:JARVIS_RUN_POSTGRES_INTEGRATION = "1"
python -m pytest -v integration_tests/test_postgres_integration.py
```

If PostgreSQL client tools are not on `PATH`, set `JARVIS_PG_BIN_DIR` to their `bin` directory. The suite requires `psql.exe` and `createdb.exe` because it exercises the project migration runner. It accepts only a password-free admin URL naming `localhost`, `127.0.0.1`, or `::1` and the `postgres` maintenance database. Missing opt-in settings or prerequisites produce explicit skips. A skipped integration suite does not verify live migrations or adapter behavior.

The fixture checks that `PGPASSFILE` names an existing file; it does not inspect or enforce its Windows ACL. Before enabling the suite, restrict access to the account running the tests (see the `icacls` example in [Credential handling](#credential-handling)) and verify the ACL locally. Keep the password only in that file, never in environment output, a URL, or a command argument.

## Backup and restore

```cmd
database\backup_db.bat
```

The backup script requires a successfully verified setup, writes a timestamped custom-format `pg_dump` archive to `JARVIS_DB_BACKUP_DIR` (default: `%USERPROFILE%\Documents\JARVIS-Backups`), refuses to overwrite an existing path, and checks the archive with `pg_restore --list`. A valid listing confirms the archive can be read, but is not a restore drill and does not prove recoverability. Store backups separately from the repository and protect them according to the data they contain.

To restore into a new, empty database, create it explicitly and use:

```cmd
pg_restore --no-password --host HOST --port PORT --username BOOTSTRAP_USER --dbname NEW_DATABASE --exit-on-error "PATH_TO_BACKUP.dump"
```

Use the same protected `PGPASSFILE` configuration. The restore target must be an existing database and is not created, dropped, or cleared by `backup_db.bat`. Review target contents and PostgreSQL version compatibility before restoring. Do not restore over the existing `jarvis_AI` database without a separately reviewed recovery plan.

## Add a migration

Add a new non-empty `scripts/NNNN_lowercase_name.sql` file with a unique identifier greater than existing migrations. Include a header describing purpose and prerequisites. Use explicit constraints and safe, forward-only changes. Do not include psql meta-commands, credentials, `DROP DATABASE`, or unreviewed destructive statements. Run the installer to apply it; the runner hashes the file after normalizing line endings to LF and records the identifier/checksum atomically. Do not edit a migration after it has been applied anywhere; add another migration instead. Full details are in [migrations/README.md](migrations/README.md).

## Troubleshooting and current limits

- **Client tool missing:** install PostgreSQL command-line tools or put the PostgreSQL `bin` directory on `PATH`; alternatively set `JARVIS_PG_BIN_DIR`. The setup requires `psql` and `createdb`; backups additionally need `pg_dump` and `pg_restore`.
- **Connection refused or timeout:** confirm the PostgreSQL service is running, the host/port match pgAdmin's server settings, and local firewall/network policy permits the connection. Do not expose PostgreSQL publicly just to make this script work.
- **Password authentication failed:** confirm the matching host, port, database, and username entry in the protected password file. Passwords are not shown in runner errors.
- **Permission denied:** use an authorized bootstrap account with `CREATEDB` only when creating a new database, and `CREATE` on the target database for migrations. Do not grant superuser to an application role.
- **Checksum or unknown migration:** use a project revision matching the database's migration history; do not reset the ledger or edit deployed files.
- **Backup file exists:** keep the existing backup and rerun after the timestamp changes or choose another output directory.

This is a Windows setup foundation with standard-library Python and PostgreSQL CLI tools; the optional FastAPI adapter uses the separately installed psycopg extra. It does not install PostgreSQL, configure server authentication, create application roles, add pgvector, or implement automated backup scheduling. Conversation history and explicitly created memory are separate. Validate the schema and adapter against the PostgreSQL versions and deployment configuration you intend to use before relying on them operationally. Previous provisioning/restore-drill results do not verify the new application adapter.
