# Offline Windows PowerShell 5.1 Task Scheduler read-only mock suite.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-first-run-probe-' + [guid]::NewGuid().ToString('N'))
$oldPath = $env:PATH
function Assert-FirstRun([bool]$yes,[string]$why) { if (-not $yes) { throw $why } }
try {
    $scripts = Join-Path $root 'scripts'
    $bin = Join-Path $root 'bin'
    New-Item -ItemType Directory -Force -Path $scripts,$bin | Out-Null
    $probe = Join-Path $scripts 'probe-ytdlp-lan-monitor-first-run.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'probe-ytdlp-lan-monitor-first-run.ps1') -Destination $probe
    $tokens = $null; $parseErrors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($probe,[ref]$tokens,[ref]$parseErrors)
    Assert-FirstRun ($parseErrors.Count -eq 0) 'PowerShell 5.1 first-run probe parser failed'
    $fakeUv = Join-Path $bin 'uv.cmd'
    [IO.File]::WriteAllText($fakeUv,('@echo off' + [Environment]::NewLine + 'exit /b 7'),[Text.Encoding]::ASCII)
    $env:PATH = ($bin,(Join-Path $env:SystemRoot 'System32'),$env:SystemRoot,
        (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')) -join ';'
    $scenarioFile = Join-Path $root 'synthetic-scheduler.ps1'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $template = @'
$ErrorActionPreference = 'Stop'
$script:scenario = '__CASE__'
function Get-ScheduledTask {
    if ($script:scenario -eq 'absent') { return }
    if ($script:scenario -eq 'unavailable') { throw 'permission denied with secret' }
    $repoRoot = (Resolve-Path -LiteralPath (Join-Path '__SCRIPTS__' '..')).Path
    $runner = Join-Path '__SCRIPTS__' 'run-ytdlp-lan-monitor.ps1'
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $uv = (Get-Command 'uv' -CommandType Application).Source
    $expected = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
        '-UvExecutable "{1}" -HostIp "{2}" -SshUser "{3}" -KnownHosts "{4}" ' +
        '-RemoteRepo "{5}" -SshPort {6} -CoordinatorPort {7} ' +
        '-EvidenceDb "{8}" -AlertFile "{9}"') -f @(
        $runner,$uv,'192.168.1.21','pc_b_account','C:\Work\hosts',
        'D:\AI\holoclip',22,8878,'C:\Work\evidence.sqlite3','C:\Work\alert.json'
    )
    if ($script:scenario -eq 'mismatch') { $expected += ' --include-pacing-trial' }
    $state = 'Ready'
    if ($script:scenario -eq 'disabled') { $state = 'Disabled' }
    [pscustomobject]@{
        TaskName='Holoclip yt-dlp LAN Monitor';State=$state
        Actions=@([pscustomobject]@{
            Execute=$psExe;Arguments=$expected;WorkingDirectory=$repoRoot
        })
        Triggers=@([pscustomobject]@{Repetition=[pscustomobject]@{Interval='PT1H'}})
        Settings=[pscustomobject]@{MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT3M'}
        Principal=[pscustomobject]@{LogonType='Interactive';RunLevel='Limited'}
    }
}
function Get-ScheduledTaskInfo {
    param([string]$TaskName)
    if ($script:scenario -eq 'unavailable-info') { throw 'Task read denied' }
    $last = (Get-Date).AddMinutes(-1)
    if ($script:scenario -eq 'never') { $last = [datetime]::MinValue }
    $code = 0
    if ($script:scenario -eq 'failed') { $code = 6 }
    [pscustomobject]@{LastRunTime=$last;LastTaskResult=$code}
}
& '__PROBE__' -TaskName 'Holoclip yt-dlp LAN Monitor' -HostIp '192.168.1.21' -SshUser 'pc_b_account' -KnownHosts 'C:\Work\hosts' -RemoteRepo 'D:\AI\holoclip' -SshPort 22 -CoordinatorPort 8878 -IntervalMinutes 60 -EvidenceDb 'C:\Work\evidence.sqlite3' -AlertFile 'C:\Work\alert.json'
'@
    $tests = @(
        @{Case='absent';Code='LAN_FIRST_RUN_TASK_ABSENT';Execution='LAN_FIRST_RUN_NEVER_RUN';Exit=0},
        @{Case='disabled';Code='LAN_FIRST_RUN_TASK_DISABLED';Execution='LAN_FIRST_RUN_LAST_RUN_UNVERIFIED';Exit=0},
        @{Case='mismatch';Code='LAN_FIRST_RUN_TASK_CONFIG_MISMATCH';Execution='LAN_FIRST_RUN_LAST_RUN_UNVERIFIED';Exit=0},
        @{Case='never';Code='LAN_FIRST_RUN_TASK_CONFIG_MATCHED';Execution='LAN_FIRST_RUN_NEVER_RUN';Exit=0},
        @{Case='failed';Code='LAN_FIRST_RUN_TASK_CONFIG_MATCHED';Execution='LAN_FIRST_RUN_LAST_RUN_FAILED';Exit=0},
        @{Case='success';Code='LAN_FIRST_RUN_TASK_CONFIG_MATCHED';Execution='LAN_FIRST_RUN_LAST_RUN_SUCCEEDED';Exit=0},
        @{Case='unavailable';Code='LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE';Execution='LAN_FIRST_RUN_LAST_RUN_UNVERIFIED';Exit=6},
        @{Case='unavailable-info';Code='LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE';Execution='LAN_FIRST_RUN_LAST_RUN_UNVERIFIED';Exit=6}
    )
    foreach ($test in $tests) {
        $body = $template.Replace('__CASE__',$test.Case).Replace('__SCRIPTS__',$scripts.Replace("'","''")).Replace('__PROBE__',$probe.Replace("'","''"))
        [IO.File]::WriteAllText($scenarioFile,$body,[Text.Encoding]::UTF8)
        $raw = & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scenarioFile 2>&1 | Out-String
        Assert-FirstRun ($LASTEXITCODE -eq $test.Exit) ('Probe exit code wrong: ' + $test.Case)
        $result = $raw | ConvertFrom-Json
        Assert-FirstRun ($result.status -eq $test.Code) ('Probe status wrong: ' + $test.Case)
        Assert-FirstRun ($result.task_execution -eq $test.Execution) ('Probe run state wrong: ' + $test.Case)
        Assert-FirstRun ($result.ready -eq $false) 'No task result may claim ready'
        Assert-FirstRun (-not $raw.Contains('permission denied with secret')) 'Raw errors leaked'
        if ($test.Case -eq 'success' -or $test.Case -eq 'failed') {
            Assert-FirstRun ($result.last_run_unix -gt 0) 'Task timestamp lost'
        } else {
            Assert-FirstRun ($null -eq $result.last_run_unix) 'Untrusted timestamp returned'
        }
    }
    Write-Output 'Windows first-run task mock checks passed: no real scheduler access'
} finally {
    $env:PATH = $oldPath
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
