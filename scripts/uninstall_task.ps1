<#
.SYNOPSIS
    Removes the StratBot paper trading scheduled task.

.DESCRIPTION
    Stops the task if it is running, then unregisters it. Does not delete the
    SQLite ledger or logs, so evidence from the run is preserved.

.EXAMPLE
    .\scripts\uninstall_task.ps1
    .\scripts\uninstall_task.ps1 -TaskName StratBotPaper -DeleteLogs
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "StratBotPaper",
    [string]$ProjectDir = $(if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }),
    [switch]$DeleteLogs
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Task '$TaskName' is not installed. Nothing to do." -ForegroundColor Yellow
    return
}

if ($PSCmdlet.ShouldProcess($TaskName, "stop and unregister scheduled task")) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
}

$ledger = Join-Path $ProjectDir "data\bot.sqlite3"
if (Test-Path $ledger) {
    Write-Host "Ledger preserved: $ledger"
}

$logDir = Join-Path $ProjectDir "logs"
if ($DeleteLogs -and (Test-Path $logDir)) {
    Remove-Item $logDir -Recurse -Force
    Write-Host "Logs deleted: $logDir" -ForegroundColor Yellow
} elseif (Test-Path $logDir) {
    Write-Host "Logs preserved: $logDir"
}