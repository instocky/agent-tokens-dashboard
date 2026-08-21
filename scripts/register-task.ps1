<#
.SYNOPSIS
    Register the `agentdash-service` Windows Scheduled Task (AtLogOn trigger).

.DESCRIPTION
    Idempotent. Refuses to overwrite an existing task. Refuses to register
    if port 8021 is already bound (prevents silent collision with a
    manually-started uvicorn).

    The registered task runs scripts/run-service.cmd as the current user
    at logon, in an interactive session (so `uv` resolves on PATH), with
    auto-restart on crash.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1

.NOTES
    Phase 7. Windows-only. Run from an elevated or non-elevated PowerShell
    both work; the task itself does not require admin (LogonType=Interactive,
    RunLevel=Limited).
#>

[CmdletBinding()]
param(
    [switch]$NoElevate
)

# Self-elevate: Task Scheduler registration needs admin. If we are not
# already elevated, re-launch this script via UAC. The child re-runs
# with -NoElevate to avoid an infinite UAC loop.
if (-not $NoElevate) {
    $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr  = New-Object Security.Principal.WindowsPrincipal($id)
    $isAdmin = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        $args2 = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-NoElevate')
        Write-Host "requesting UAC elevation..." -ForegroundColor Yellow
        $proc = Start-Process -FilePath 'powershell' -ArgumentList $args2 -Verb RunAs -PassThru
        $proc.WaitForExit()
        exit $proc.ExitCode
    }
}

$ErrorActionPreference = 'Stop'

# --- inputs -------------------------------------------------------------------

$TaskName       = 'agentdash-service'
$ProjectDir     = 'C:\Projects\Python\0803_agent-tokens-dashboard'
$WrapperScript  = Join-Path $ProjectDir 'scripts\run-service.cmd'
$LogDir         = Join-Path $env:LOCALAPPDATA 'agentdash-service'
$Port           = 8021

# --- preflight ---------------------------------------------------------------

if (-not (Test-Path $WrapperScript)) {
    throw "wrapper script not found: $WrapperScript"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not on PATH; install uv (https://docs.astral.sh/uv/) and re-run"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Write-Host "task '$TaskName' already registered - state: $($existing.State)" -ForegroundColor Yellow
    Write-Host "to re-register, run scripts\unregister-task.ps1 first"
    exit 0
}

# Refuse if port 8021 is already bound - most likely a manual uvicorn is up.
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($null -ne $listener) {
    $proc = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    throw "port $Port already bound by $proc (pid $($listener.OwningProcess)) - stop it first"
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# --- define task -------------------------------------------------------------

$action    = New-ScheduledTaskAction `
                -Execute $WrapperScript `
                -WorkingDirectory $ProjectDir

$trigger   = New-ScheduledTaskTrigger -AtLogOn

$settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = unlimited

$principal = New-ScheduledTaskPrincipal `
                -User $env:USERNAME `
                -LogonType Interactive `
                -RunLevel Limited

# --- register ----------------------------------------------------------------

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Description 'Local FastAPI dashboard service (127.0.0.1:8021). Restarts on crash.' `
    | Out-Null

Write-Host "registered '$TaskName' (AtLogOn -> $WrapperScript)" -ForegroundColor Green
Write-Host "log: $LogDir\service.log"
Write-Host "next: log out and back in (or run: Start-ScheduledTask -TaskName '$TaskName')"
