<#
.SYNOPSIS
    Installs a Windows scheduled task that keeps the paper bot running.

.DESCRIPTION
    Phase 3 requires weeks of continuous runtime. A task left running in a
    terminal dies the moment you close it or log off. This registers a task
    that starts on login, restarts on failure, and survives reboots.

    Start the task:  Start-ScheduledTask -TaskName StratBotPaper
    Stop it:         Stop-ScheduledTask  -TaskName StratBotPaper
    Remove it:       Unregister-ScheduledTask -TaskName StratBotPaper -Confirm:$false
    Status:          Get-ScheduledTask  -TaskName StratBotPaper | Get-ScheduledTaskInfo

.EXAMPLE
    .\scripts\install_task.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "StratBotPaper",
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [int]$PollSeconds = 900
)

$ErrorActionPreference = "Stop"

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtualenv not found at $python. Run: python -m venv .venv"
}

# Log to the scratchpad so a crash is never silent.
$logDir = Join-Path $env:LOCALAPPDATA "StratBot\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "run_bot.py --forever --sleep $PollSeconds" `
    -WorkingDirectory $ProjectDir

# On logon, plus at startup for machines that stay signed in.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$bootTrigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries

# Least privilege: this bot only needs to read market data and write its own files.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($trigger, $bootTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "StratBot paper trading engine (simulation only, places no real orders)" `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  python : $python"
Write-Host "  workdir: $ProjectDir"
Write-Host "  poll   : every $PollSeconds seconds"
Write-Host ""
Write-Host "Start it with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop it with:   Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "Remove it with: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"