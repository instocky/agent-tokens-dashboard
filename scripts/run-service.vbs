' ---------------------------------------------------------------------------
' run-service.vbs
'
' Hidden launcher for run-service.cmd, invoked by the agentdash-service
' Scheduled Task (see register-task.ps1). The native .cmd would otherwise
' pop a visible cmd.exe window in the interactive session because
' Task Scheduler launches console scripts with default visibility.
'
' WindowStyle=0  -> SW_HIDE  (no console window).
' bWaitOnReturn  -> True      (block WScript until cmd.exe exits, i.e. until
'                                uvicorn dies). This keeps the Scheduled
'                                Task in the "Running" state for the
'                                lifetime of the service so the existing
'                                RestartCount=3 / RestartInterval=1m
'                                settings actually fire when uvicorn
'                                crashes. With bWaitOnReturn=False the
'                                task completes the moment WScript
'                                returns and Task Scheduler stops
'                                supervising the orphaned uvicorn.)
'
' Quoting: in a VBS string literal, "" encodes one literal ". So the
' argument below becomes:
'     cmd.exe /c "C:\...\run-service.cmd"
' which is the canonical way to invoke a console script whose path
' contains spaces.
' ---------------------------------------------------------------------------

Option Explicit

Dim shell
Set shell = CreateObject("WScript.Shell")

shell.Run "cmd.exe /c ""C:\Projects\Python\0803_agent-tokens-dashboard\scripts\run-service.cmd""", 0, True

Set shell = Nothing
