@echo off
setlocal
set "JARVIS_DATABASE_DIR=%~dp0"
if exist "%JARVIS_DATABASE_DIR%config\db_config.local.bat" call "%JARVIS_DATABASE_DIR%config\db_config.local.bat"
python "%JARVIS_DATABASE_DIR%runner.py" install
set "JARVIS_DATABASE_EXIT=%ERRORLEVEL%"
exit /b %JARVIS_DATABASE_EXIT%
