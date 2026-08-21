@echo off
setlocal

rem ---------------------------------------------------------------------------
rem run-service.cmd
rem
rem Foreground entrypoint invoked by the `agentdash-service` Scheduled Task
rem (see register-task.ps1). Redirects uvicorn stdout+stderr into a per-user
rem log file under %LOCALAPPDATA%\agentdash-service\.
rem
rem This file is the *only* place we do `>> log 2>&1` — the .ps1 register
rem script stays clean and the .cmd is a thin wrapper the operator can read
rem in five seconds.
rem ---------------------------------------------------------------------------

set "PROJECT_DIR=C:\Projects\Python\0803_agent-tokens-dashboard"
set "LOG_DIR=%LOCALAPPDATA%\agentdash-service"
set "LOG_FILE=%LOG_DIR%\service.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%PROJECT_DIR%"

rem -- ISO 8601 banner so the log is greppable across restarts
echo === agentdash-service start %DATE% %TIME% ===================== >> "%LOG_FILE%"

rem -- Unbuffered (-u) so log lines appear promptly, not on Python's own
rem    block-flush. The 2>&1 collapses uvicorn's stderr (which uvicorn
rem    uses for `--log-level info` access logs) into the same file.
uv run python -u -m uvicorn agentdash_service.main:app ^
    --host 127.0.0.1 ^
    --port 8021 ^
    >> "%LOG_FILE%" 2>&1

endlocal
