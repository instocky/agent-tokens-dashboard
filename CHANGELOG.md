# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not yet tag versions, so each entry is dated.

## [Unreleased] — 2026-08-22

### Changed
- **Auto-start runs hidden (no visible `cmd.exe` window).** New
  `scripts/run-service.vbs` wraps `run-service.cmd` via
  `WScript.Shell.Run` with `WindowStyle=0` (SW_HIDE). The
  Scheduled Task now invokes `wscript.exe` (explicit, not
  file-association-driven) on the .vbs. Previously the task ran
  the .cmd directly, which made Task Scheduler pop a console
  window in the user's interactive session at every logon and
  every crash-restart.
- **`bWaitOnReturn=True`** in the VBS Run call. The wrapper
  blocks until `cmd.exe` exits, so the Scheduled Task stays in
  the "Running" state for the lifetime of the service. This
  preserves the existing `RestartCount=3 / RestartInterval=1m`
  crash-restart policy — with `False` the task would complete
  the moment `wscript.exe` returns and Task Scheduler would stop
  supervising the orphaned uvicorn.

### Fixed
- **`register-task.ps1` argument construction.** The previous
  `-Argument '"' + $WrapperScript + '"'` inlined a string-concat
  inside a `New-ScheduledTaskAction` call on a backtick
  line-continuation. PowerShell parsed the `+` as a positional
  argument to the cmdlet, not as an operator, so registration
  aborted with `PositionalParameterNotFound`. The argument is
  now built into a `$wscriptArg` variable first.

### Notes
- **Verify with `curl`, not `Get-NetTCPConnection`.** The CIM
  query that backs `Get-NetTCPConnection -LocalPort 8021 -State
  Listen` occasionally returns "no objects" for freshly-bound
  loopback sockets even when the service is responding. As a
  smoke test, prefer
  `curl http://127.0.0.1:8021/api/v1/health` or
  `netstat -ano | Select-String ":8021" | Select-String "LISTENING"`.
  The `Get-NetTCPConnection` line in README/ARCHITECTURE §13.4
  is kept only because it is still useful *after* the service
  has been up for >30s, when the CIM cache has caught up.

### Files touched
- `scripts/run-service.vbs` — **new**
- `scripts/register-task.ps1` — action invocation rewritten
- `README.md` — Auto-start section + project layout
- `docs/ARCHITECTURE.md` §13 — file table, task action, verify
- `CHANGELOG.md` — **new** (this file)
