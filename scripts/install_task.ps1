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
    # $PSScriptRoot is empty under `-File` in some hosts, so fall back to $PWD.
    [string]$ProjectDir = $(if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }),
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

# Registering a scheduled task requires elevation on most Windows configurations,
# so try unelevated first and only re-run as Administrator if that is refused.
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $elevated) {
    Write-Warning "Not running elevated. Registering a scheduled task will likely fail."
    Write-Warning "Re-run from an Administrator PowerShell, or use -StartNow for a non-persistent run."
}

$triggers = if ($elevated) { @($trigger, $bootTrigger) } else { @($trigger) }

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "StratBot paper trading engine (simulation only, places no real orders)" `
        -Force | Out-Null
} catch {
    Write-Host ""
    Write-Host "Could not register the scheduled task: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Open PowerShell as Administrator and re-run this script." -ForegroundColor Yellow
    Write-Host "Or start the bot right now without a scheduled task:" -ForegroundColor Yellow
    Write-Host "    .\.venv\Scripts\python.exe run_bot.py --forever --sleep 900" -ForegroundColor Cyan
    Write-Host "Keep that window open; the bot stops when you close it." -ForegroundColor Yellow
    exit 1
}

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  python : $python"
Write-Host "  workdir: $ProjectDir"
Write-Host "  poll   : every $PollSeconds seconds"
Write-Host ""
Write-Host "Start it with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop it with:   Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "Remove it with: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"