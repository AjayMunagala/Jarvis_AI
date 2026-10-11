@echo off
rem Copy this file to db_config.local.bat and set values for your server.
rem These are examples and have not been verified against your PostgreSQL instance.
set "JARVIS_DB_HOST=localhost"
set "JARVIS_DB_PORT=5432"
set "JARVIS_DB_NAME=jarvis_AI"
set "JARVIS_DB_BOOTSTRAP_USER=postgre"
set "JARVIS_DB_MAINTENANCE_DB=postgres"
rem Optional: set to the PostgreSQL bin directory if its CLI tools are not on PATH.
set "JARVIS_PG_BIN_DIR="
rem Optional backup directory. The default is Documents\JARVIS-Backups.
set "JARVIS_DB_BACKUP_DIR=%USERPROFILE%\Documents\JARVIS-Backups"
rem Use PostgreSQL's local password file; never put the password in this file.
set "PGPASSFILE=%APPDATA%\postgresql\pgpass.conf"
