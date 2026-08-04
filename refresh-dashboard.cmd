@echo off
setlocal
rem refresh-dashboard.cmd - rebuilds dashboard.html from runtime-state.sqlite.
rem Run from Explorer (double-click), Start Menu, or pinned taskbar.
rem Window auto-closes on success; stays open on error for inspection.

cd /d "%~dp0"

python build_dashboard.py
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo [refresh-dashboard.cmd] build_dashboard.py exited with code %RC%
    echo Press any key to close...
    pause >nul
) else (
    start "" "%~dp0dashboard.html"
)

endlocal & exit /b %RC%
