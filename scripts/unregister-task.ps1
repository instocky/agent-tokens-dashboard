<#
.SYNOPSIS
    Remove the `agentdash-service` Windows Scheduled Task.

.DESCRIPTION
    Idempotent. Exits 0 if the task is already absent. Does NOT kill the
    currently-running uvicorn (if any) - that is the operator's call.
    Refusing to touch the process keeps the script safe to re-run.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\unregister-task.ps1
#>

[CmdletBinding()]
param(
    [switch]$NoElevate
)

# Self-elevate: Task Scheduler mutation needs admin.
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

$TaskName = 'agentdash-service'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Host "task '$TaskName' not registered - nothing to do"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null

Write-Host "unregistered '$TaskName'" -ForegroundColor Green
Write-Host "to stop a currently-running instance: Get-Process python -ErrorAction SilentlyContinue | Where-Object { `$_.MainWindowTitle -eq '' } | Stop-Process -Force"
