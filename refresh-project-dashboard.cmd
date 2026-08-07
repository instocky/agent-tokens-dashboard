@echo off
setlocal
rem refresh-project-dashboard.cmd - rebuilds project-dashboard.html from runtime-state.sqlite.
rem Run from Explorer (double-click), Start Menu, or pinned taskbar.
rem Window auto-closes on success; stays open on error for inspection.

cd /d "%~dp0"

python build_project_dashboard.py
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo [refresh-project-dashboard.cmd] build_project_dashboard.py exited with code %RC%
    echo Press any key to close...
    pause >nul
) else (
    start "" "%~dp0project-dashboard.html"
)

endlocal & exit /b %RC%
