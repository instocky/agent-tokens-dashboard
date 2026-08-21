@echo off
setlocal
rem refresh-session-dashboard.cmd - rebuilds session-dashboard.html from runtime-state.sqlite.
rem Run from Explorer (double-click), Start Menu, or pinned taskbar.
rem Window auto-closes on success; stays open on error for inspection.

cd /d "%~dp0"

python build_session_dashboard.py
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo [refresh-session-dashboard.cmd] build_session_dashboard.py exited with code %RC%
    echo Press any key to close...
    pause >nul
) else (
    start "" "%~dp0session-dashboard.html"
)

endlocal & exit /b %RC%
