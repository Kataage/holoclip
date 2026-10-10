# Windows PowerShell 5.1 offline contracts; no real Task Scheduler changes or LAN.
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-lan-task-test-' + [guid]::NewGuid().ToString('N'))
$bin = Join-Path $testRoot 'fake tools with spaces'
$fakeScripts = Join-Path $testRoot 'scripts'
$originalPath = $env:PATH
$script:captured = @()
function Assert-Lan([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    [pscustomobject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}
function New-ScheduledTaskTrigger {
    param([switch]$AtLogOn, [string]$User)
    [pscustomobject]@{ AtLogOn = $AtLogOn.IsPresent; User = $User }
}
function New-ScheduledTaskSettingsSet {
    param([string]$MultipleInstances, [switch]$StartWhenAvailable,
          [timespan]$ExecutionTimeLimit, [int]$RestartCount,
          [timespan]$RestartInterval, [switch]$AllowStartIfOnBatteries,
          [switch]$DontStopIfGoingOnBatteries)
    [pscustomobject]@{
        MultipleInstances = $MultipleInstances; StartWhenAvailable = $StartWhenAvailable.IsPresent
        ExecutionTimeLimit = $ExecutionTimeLimit; RestartCount = $RestartCount
        RestartInterval = $RestartInterval
        AllowStartIfOnBatteries = $AllowStartIfOnBatteries.IsPresent
        DontStopIfGoingOnBatteries = $DontStopIfGoingOnBatteries.IsPresent
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
        Settings = $Settings; Principal = $Principal; Force = $Force.IsPresent
    }
    [pscustomobject]@{ TaskName = $TaskName }
}
function Start-ScheduledTask { throw 'Installer must never auto-start a task' }
try {
    New-Item -ItemType Directory -Force -Path $fakeScripts, $bin, (Join-Path $testRoot '.venv\Scripts'), (Join-Path $testRoot 'data\runtime') | Out-Null
    $installer = Join-Path $fakeScripts 'install-ytdlp-lan-task.ps1'
    $runner = Join-Path $fakeScripts 'run-ytdlp-lan-coordinator.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-ytdlp-lan-task.ps1') -Destination $installer
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-ytdlp-lan-coordinator.ps1') -Destination $runner
    foreach ($path in @($installer, $runner)) {
        $tokens = $null; $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
        Assert-Lan ($errors.Count -eq 0) ('PowerShell 5.1 parse error: ' + $path)
    }
    [IO.File]::WriteAllText((Join-Path $testRoot '.venv\Scripts\python.exe'), '', [Text.Encoding]::ASCII)
    $uvPath = Join-Path $bin 'uv.cmd'
    $marker = Join-Path $testRoot 'fake-uv-tries.txt'
    $cmd = @('@echo off', ('echo invoked >> "{0}"' -f $marker), 'exit /b 7') -join [Environment]::NewLine
    [IO.File]::WriteAllText($uvPath, ($cmd + [Environment]::NewLine), [Text.Encoding]::ASCII)
    $env:PATH = ($bin, (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot,
                 (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')) -join ';'
    $secret = Join-Path $testRoot 'data\runtime\yt-dlp-lan-secret.txt'
    $state = Join-Path $testRoot 'data\runtime\yt-dlp-lan-coordinator.sqlite3'
    try { . $installer -BindIp '192.168.1.20'; throw 'Expected missing secret rejection' }
    catch { Assert-Lan ($script:captured.Count -eq 0) 'Registered with missing secret' }
    [IO.File]::WriteAllText($secret, ('a' * 64) + [Environment]::NewLine, [Text.Encoding]::ASCII)
    foreach ($badIp in @('0.0.0.0', '8.8.8.8', '192.168.1.20;bad', 'localhost',
                          '192.168.001.020', '169.254.1.2')) {
        try { . $installer -BindIp $badIp; throw 'Expected invalid address rejection' }
        catch { Assert-Lan ($script:captured.Count -eq 0) ('Registered unsafe IP: ' + $badIp) }
    }
    . $installer -BindIp '192.168.1.20'
    Assert-Lan ($script:captured.Count -eq 1) 'Expected exactly one task registration'
    $task = $script:captured[0]
    Assert-Lan ($task.Trigger.AtLogOn) 'Must trigger on current-user logon'
    Assert-Lan ($task.Trigger.User -eq [Security.Principal.WindowsIdentity]::GetCurrent().Name) 'Wrong logon user'
    Assert-Lan ($task.Principal.LogonType -eq 'Interactive') 'Must not store account password'
    Assert-Lan ($task.Principal.RunLevel -eq 'Limited') 'Must not elevate server'
    Assert-Lan ($task.Settings.MultipleInstances -eq 'IgnoreNew') 'Duplicate task execution possible'
    Assert-Lan ($task.Settings.ExecutionTimeLimit -eq [timespan]::Zero) 'Daemon time-limited'
    Assert-Lan ($task.Settings.RestartCount -eq 3) 'Scheduler crash recovery not configured'
    Assert-Lan ($task.Settings.RestartInterval -eq [timespan]::FromMinutes(1)) 'Wrong scheduler retry interval'
    Assert-Lan ($task.Settings.AllowStartIfOnBatteries -and $task.Settings.DontStopIfGoingOnBatteries) 'Battery stop risk'
    Assert-Lan ($task.Action.WorkingDirectory -eq $testRoot) 'Task working directory not pinned'
    Assert-Lan ($task.Action.Argument.Contains(('-UvExecutable "{0}"' -f $uvPath))) 'uv path not pinned'
    Assert-Lan ($task.Action.Argument.Contains(('-SecretFile "{0}"' -f $secret))) 'Secret path not pinned'
    Assert-Lan ($task.Action.Argument.Contains(('-StatePath "{0}"' -f $state))) 'SQLite path not pinned'
    Assert-Lan ($task.Action.Argument.Contains(('-File "{0}"' -f $runner))) 'Runner path missing'
    Assert-Lan (-not $task.Force) 'Existing tasks must never be overwritten by a forced install'
    Assert-Lan (-not (Test-Path -LiteralPath $marker)) 'Installer unexpectedly ran uv'
    [IO.File]::WriteAllText($secret, 'bad', [Text.Encoding]::ASCII)
    try { . $installer -BindIp '192.168.1.20'; throw 'Invalid secret accepted' }
    catch { Assert-Lan ($script:captured.Count -eq 1) 'Bad secret registered task' }
    [IO.File]::WriteAllText($secret, ('a' * 64) + [Environment]::NewLine, [Text.Encoding]::ASCII)
    try { . $installer -TaskName 'Invalid\Name' -BindIp '192.168.1.20'; throw 'Invalid task name accepted' }
    catch { Assert-Lan ($script:captured.Count -eq 1) 'Unsafe task name registered' }
    # Real Windows process launches only the fake uv.cmd, which exits 7.
    # Runner retries twice, with zero delay, then exits; no real server starts.
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $runner, '-UvExecutable', $uvPath, '-BindIp', '127.0.0.1',
        '-Port', '8878', '-SecretFile', $secret, '-StatePath', $state,
        '-MaxRestarts', '2', '-RetryDelaySeconds', '0')
    $output = & $psExe @arguments 2>&1 | Out-String
    Assert-Lan ($LASTEXITCODE -eq 7) 'Runner did not propagate fake terminal exit code'
    $tries = @(Get-Content -LiteralPath $marker)
    Assert-Lan ($tries.Count -eq 3) 'Runner did not execute exactly three attempts'
    Assert-Lan (-not $output.Contains(('a' * 64))) 'Runner leaked secret bytes'
    Assert-Lan ($output -match 'restart_count=2') 'Missing bounded restart event'
    Assert-Lan (-not (Test-Path -LiteralPath $state)) 'Fake runner opened a real SQLite store'
    # Exercise the exact Task Scheduler inventory probe with a fake cmdlet
    # in a separate Windows PowerShell process. Never inspect real user tasks.
    $probe = (Join-Path $PSScriptRoot 'probe-ytdlp-lan-task.ps1').Replace("'", "''")
    $cases = @(
        @{ Body = '[pscustomobject]@{ TaskName = "Unrelated" }'; Expected = 'LAN_TASK_ABSENT_OBSERVED'; Exit = 0 },
        @{ Body = '[pscustomobject]@{ TaskName = "Holoclip yt-dlp LAN Coordinator" }'; Expected = 'LAN_TASK_ALREADY_PRESENT'; Exit = 0 },
        @{ Body = '[pscustomobject]@{ TaskName = "holoclip yt-dlp lan coordinator" }'; Expected = 'LAN_TASK_ALREADY_PRESENT'; Exit = 0 },
        # The outer mock scenario is another script. A nested 'exit 6'
        # sets the called-script status, but the enclosing -File mock may
        # terminate 0. Status-code rejection is the invariant exercised here.
        @{ Body = 'throw "denied"'; Expected = 'LAN_TASK_ENUMERATION_UNAVAILABLE'; Exit = 0 }
    )
    $scenarioPath = Join-Path $testRoot 'task-probe-mock-case.ps1'
    foreach ($case in $cases) {
        # -File avoids WindowsPowerShell 5.1 -Command string-quoting
        # differences. The temp script defines a fake cmdlet *only*
        # in this disposable child process (never the actual scheduler).
        $scenario = @'
function Get-ScheduledTask {
    [CmdletBinding()]
    param()
    __CASE_BODY__
}
& '__PROBE_PATH__' -TaskName 'Holoclip yt-dlp LAN Coordinator'
'@
        $scenario = $scenario.Replace('__CASE_BODY__', $case.Body).Replace('__PROBE_PATH__', $probe)
        [IO.File]::WriteAllText($scenarioPath, $scenario, [Text.Encoding]::UTF8)
        $text = & $psExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scenarioPath 2>&1 | Out-String
        Assert-Lan ($LASTEXITCODE -eq $case.Exit) (
            'Task inventory probe exit code mismatch: ' + $LASTEXITCODE +
            ' expected=' + $case.Exit + ' output=' + $text
        )
        $doc = $text | ConvertFrom-Json
        Assert-Lan ($doc.status -eq $case.Expected) 'Task inventory probe failed closed incorrectly'
        Assert-Lan ($doc.ready -eq $false) 'Task inventory probe claimed production readiness'
    }
    Write-Host 'Windows LAN task and restart tests passed; zero real tasks registered'
}
finally {
    $env:PATH = $originalPath
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
