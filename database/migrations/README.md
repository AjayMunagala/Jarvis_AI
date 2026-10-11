# Migration strategy

The Python-standard-library runner applies `scripts/NNNN_name.sql` files in lexical identifier order. Each file is hashed with SHA-256; its identifier and checksum are recorded in `jarvis_meta.schema_migrations` in the same PostgreSQL transaction as the migration. Applied files are skipped, changed applied files and database migrations unknown to the local checkout stop the run, and each failed migration rolls back without marking itself applied.

`0001_migration_metadata.sql` creates only the migration ledger. Do not edit a migration after it has been deployed; add the next unique numeric identifier instead. The runner must be used to apply migrations so SQL and ledger updates remain atomic. Concurrent installers may cause one run to stop on a uniqueness/locking error; rerun verification and then installation after the other run finishes.
