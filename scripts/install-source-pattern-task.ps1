# Opt-in one-time local installation; does NOT start the collector.
param(
    [string]$TaskName = 'Holoclip Source Patterns',
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '03:00'
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'Windows Task Scheduler is required'
}
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run-source-pattern-worker.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'Source-pattern task runner is missing'
}
# Resolve before registration: the task keeps working even when launched outside
# the repo. uv is required only when the scheduled action actually starts.
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $runner
$action = New-ScheduledTaskAction -Execute $psExe -Argument $taskArgs -WorkingDirectory $repoRoot
$startAt = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
# Zero execution limit avoids scheduler hard-terminating a partially written
# media/measurement step. Overlapping task instances are skipped.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Bounded unreviewed public Holoclip source-video controls (CPU-only)' -Force | Out-Null
Write-Host "Registered local daily source-pattern collector: $TaskName at $At"
Write-Host 'The task has not been started. Disable it to stop future automatic runs.'
