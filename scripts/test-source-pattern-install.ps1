# Native WindowsPowerShell 5.1 task registration contract tests.
# All ScheduledTasks cmdlets are shadowed. Never registers or starts a real task.
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('holoclip-task-install-test-' + [guid]::NewGuid().ToString('N'))
$bin = Join-Path $testRoot 'fake tools with spaces'
$originalPath = $env:PATH
$script:captured = @()
function Assert-Task([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    [pscustomobject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}
function New-ScheduledTaskTrigger {
    param([switch]$Daily, [datetime]$At)
    [pscustomobject]@{ Daily = $Daily.IsPresent; At = $At }
}
function New-ScheduledTaskSettingsSet {
    param([string]$MultipleInstances, [switch]$StartWhenAvailable, [timespan]$ExecutionTimeLimit)
    [pscustomobject]@{
        MultipleInstances = $MultipleInstances
        StartWhenAvailable = $StartWhenAvailable.IsPresent
        ExecutionTimeLimit = $ExecutionTimeLimit
    }
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}
function Register-ScheduledTask {
    param([string]$TaskName, $Action, $Trigger, $Settings, $Principal,
          [string]$Description, [switch]$Force)
    $script:captured += [pscustomobject]@{
        TaskName = $TaskName; Action = $Action; Trigger = $Trigger
        Settings = $Settings; Principal = $Principal
        Description = $Description; Force = $Force.IsPresent
    }
    return [pscustomobject]@{ Name = $TaskName }
}
function Start-ScheduledTask {
    throw 'Installer must NEVER launch media during registration'
}
function Write-MockBinary([string]$name, [string]$body = 'exit /b 0') {
    $path = Join-Path $bin ($name + '.cmd')
    [System.IO.File]::WriteAllText(
        $path, ("@echo off`r`n" + $body + "`r`n"), ([System.Text.Encoding]::ASCII)
    )
}
try {
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $env:PATH = ($bin, (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    $installer = Join-Path $PSScriptRoot 'install-source-pattern-task.ps1'
    $repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $runner = Join-Path $PSScriptRoot 'run-source-pattern-worker.ps1'
    Write-MockBinary 'uv'
    # Mandatory tool missing: no task shall be registered.
    try {
        . $installer -TaskName 'Holoclip Source Patterns' -At '04:30'
        throw 'Expected preflight to fail when ffmpeg is absent'
    }
    catch [System.Management.Automation.CommandNotFoundException] {
        Assert-Task ($script:captured.Count -eq 0) 'Registered despite missing ffmpeg'
    }
    Write-MockBinary 'ffmpeg'
    Write-MockBinary 'ffprobe'
    # Do not create a recurring task when JS is unavailable or too old.
    try {
        . $installer -TaskName 'Holoclip Source Patterns' -At '04:30'
        throw 'Expected missing JS runtime to abort registration'
    }
    catch {
        Assert-Task ($_.Exception.Message -match 'Deno >= 2.3') 'Wrong JS preflight error'
        Assert-Task ($script:captured.Count -eq 0) 'Registered despite missing JS runtime'
    }
    Write-MockBinary 'deno' 'echo deno 2.2.9'
    try {
        . $installer -TaskName 'Holoclip Source Patterns' -At '04:30'
        throw 'Expected too-old Deno runtime to abort registration'
    }
    catch {
        Assert-Task ($_.Exception.Message -match 'Deno >= 2.3') 'Old Deno incorrectly accepted'
        Assert-Task ($script:captured.Count -eq 0) 'Registered despite old Deno'
    }
    Write-MockBinary 'node' 'echo v22.3.0'
    . $installer -TaskName 'Holoclip Source Patterns' -At '04:30'
    Assert-Task ($script:captured.Count -eq 1) 'Expected exactly one mock registration'
    $task = $script:captured[0]
    $uvPath = Join-Path $bin 'uv.cmd'
    Assert-Task ($task.Action.Argument.Contains(('-UvExecutable "{0}"' -f $uvPath))) 'Absolute uv path must be pinned'
    Assert-Task ($task.Action.Argument.Contains(('-FFmpegExecutable "{0}"' -f (Join-Path $bin 'ffmpeg.cmd')))) 'FFmpeg path not pinned'
    Assert-Task ($task.Action.Argument.Contains(('-FFprobeExecutable "{0}"' -f (Join-Path $bin 'ffprobe.cmd')))) 'FFprobe path not pinned'
    Assert-Task ($task.Action.Argument.Contains(('-JsRuntimeExecutable "{0}"' -f (Join-Path $bin 'node.cmd')))) 'Supported Node fallback not pinned'
    Assert-Task ($task.Action.Argument.Contains(('-File "{0}"' -f $runner))) 'Task action must invoke repo runner'
    Assert-Task ($task.Action.Execute -eq (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')) 'Wrong engine'
    Assert-Task ($task.Action.WorkingDirectory -eq $repo) 'Wrong working dir'
    Assert-Task ($task.Trigger.Daily) 'Task must run daily'
    Assert-Task ($task.Trigger.At.Hour -eq 4 -and $task.Trigger.At.Minute -eq 30) 'Custom schedule lost'
    Assert-Task ($task.Settings.MultipleInstances -eq 'IgnoreNew') 'Overlapping starts must be skipped'
    Assert-Task ($task.Settings.StartWhenAvailable) 'Task must start when PC is available'
    Assert-Task ($task.Settings.ExecutionTimeLimit -eq [timespan]::Zero) 'Task can hard stop ongoing work'
    Assert-Task ($task.Principal.LogonType -eq 'Interactive') 'Task may request unattended credential'
    Assert-Task ($task.Principal.RunLevel -eq 'Limited') 'Task must not elevate'
    Assert-Task ($task.Principal.UserId -eq [Security.Principal.WindowsIdentity]::GetCurrent().Name) 'Wrong user'
    Assert-Task ($task.Force) 'Task registration must be repeatable'
    # Deno is preferred when upgraded to the supported version.
    Write-MockBinary 'deno' 'echo deno 2.3.1'
    # Rerun with default time, never start collector; no cumulative task names.
    . $installer
    Assert-Task ($script:captured.Count -eq 2) 'Expected one registration per explicit invocation'
    Assert-Task ($script:captured[1].TaskName -eq 'Holoclip Source Patterns') 'Default name changed'
    Assert-Task ($script:captured[1].Trigger.At.Hour -eq 3) 'Default schedule changed'
    Assert-Task ($script:captured[1].Action.Argument.Contains(('-JsRuntimeExecutable "{0}"' -f (Join-Path $bin 'deno.cmd')))) 'Supported Deno not preferred'
    Write-Host 'Windows PowerShell installer: missing-tool + custom/default schedule mocked tests passed; zero tasks registered'
}
finally {
    $env:PATH = $originalPath
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
