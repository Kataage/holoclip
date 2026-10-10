# Offline Windows Task Scheduler monitor contract, zero real registrations.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-monitor-test-' + [guid]::NewGuid().ToString('N'))
$oldPath = $env:PATH
$script:registered = @()
$script:found = @()
function Assert-Monitor([bool]$cond,[string]$reason) { if (-not $cond) { throw $reason } }
function New-ScheduledTaskAction {
    param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
    [pscustomobject]@{Execute=$Execute;Argument=$Argument;WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger {
    param([switch]$Once,[datetime]$At,[timespan]$RepetitionInterval,[timespan]$RepetitionDuration)
    [pscustomobject]@{Once=$Once.IsPresent;RepetitionInterval=$RepetitionInterval;RepetitionDuration=$RepetitionDuration}
}
function New-ScheduledTaskSettingsSet {
    param([string]$MultipleInstances,[switch]$StartWhenAvailable,[timespan]$ExecutionTimeLimit,
          [switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries)
    [pscustomobject]@{MultipleInstances=$MultipleInstances;ExecutionTimeLimit=$ExecutionTimeLimit}
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId,[string]$LogonType,[string]$RunLevel)
    [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}
}
function Get-ScheduledTask { return $script:found }
function Register-ScheduledTask {
    param([string]$TaskName,$Action,$Trigger,$Settings,$Principal,[string]$Description,[switch]$Force)
    $script:registered += [pscustomobject]@{
        TaskName=$TaskName;Action=$Action;Trigger=$Trigger
        Settings=$Settings;Principal=$Principal;Force=$Force.IsPresent
    }
    [pscustomobject]@{TaskName=$TaskName}
}
function Start-ScheduledTask { throw 'Must never auto-start' }
try {
    $scripts = Join-Path $root 'scripts'
    $venv = Join-Path $root '.venv\Scripts'
    $bin = Join-Path $root 'bin'
    New-Item -ItemType Directory -Force -Path $scripts, $venv, $bin | Out-Null
    $installer = Join-Path $scripts 'install-ytdlp-lan-monitor-task.ps1'
    $runner = Join-Path $scripts 'run-ytdlp-lan-monitor.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-ytdlp-lan-monitor-task.ps1') -Destination $installer
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-ytdlp-lan-monitor.ps1') -Destination $runner
    foreach ($scriptFile in @($installer,$runner)) {
        $tokens = $null; $errors = $null
        $null = [Management.Automation.Language.Parser]::ParseFile($scriptFile,[ref]$tokens,[ref]$errors)
        Assert-Monitor ($errors.Count -eq 0) ('Parser error: ' + $scriptFile)
    }
    [IO.File]::WriteAllText((Join-Path $venv 'python.exe'),'', [Text.Encoding]::ASCII)
    $fakeUv = Join-Path $bin 'uv.cmd'
    [IO.File]::WriteAllText($fakeUv,('@echo off' + [Environment]::NewLine + 'exit /b 7'),[Text.Encoding]::ASCII)
    $env:PATH = ($bin, (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot,
                 (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')) -join ';'
    $hosts = Join-Path $root 'pc-b-known-hosts'
    [IO.File]::WriteAllText($hosts,'192.168.1.21 ssh-ed25519 MOCK_HOST_KEY',[Text.Encoding]::ASCII)
    $db = Join-Path $root 'data\runtime\monitor.sqlite3'
    $alert = Join-Path $root 'data\runtime\alert.json'
    $params = @{
        TaskName='Holoclip yt-dlp LAN Monitor'; HostIp='192.168.1.21'
        SshUser='pc_a_account'; KnownHosts=$hosts; RemoteRepo='D:\AI\holoclip'
        SshPort=22; CoordinatorPort=8878; IntervalMinutes=60
        EvidenceDb=$db; AlertFile=$alert
    }
    foreach ($invalid in @('8.8.8.8','127.0.0.1','0.0.0.0','192.168.001.021','localhost')) {
        $params.HostIp = $invalid
        try { . $installer @params; throw 'Invalid host accepted' }
        catch { Assert-Monitor ($script:registered.Count -eq 0) ('Registered invalid IP: ' + $invalid) }
    }
    $params.HostIp = '192.168.1.21'
    $params.KnownHosts = Join-Path $root 'missing-hosts'
    try { . $installer @params; throw 'Missing pin accepted' }
    catch { Assert-Monitor ($script:registered.Count -eq 0) 'Missing host key registered' }
    $params.KnownHosts = $hosts
    $script:found = @([pscustomobject]@{ TaskName=$params.TaskName })
    try { . $installer @params; throw 'Overwrote task' }
    catch { Assert-Monitor ($script:registered.Count -eq 0) 'Task collision registered' }
    $script:found = @()
    . $installer @params | Out-Null
    Assert-Monitor ($script:registered.Count -eq 1) 'Expected single task'
    $task = $script:registered[0]
    Assert-Monitor (-not $task.Force) 'Must not force overwrite'
    Assert-Monitor ($task.Principal.LogonType -eq 'Interactive') 'Passwordless logon required'
    Assert-Monitor ($task.Principal.RunLevel -eq 'Limited') 'Must not elevate'
    Assert-Monitor ($task.Settings.MultipleInstances -eq 'IgnoreNew') 'Overlapping runs allowed'
    Assert-Monitor ($task.Settings.ExecutionTimeLimit -eq [timespan]::FromMinutes(3)) 'No execution limit'
    Assert-Monitor ($task.Trigger.Once) 'Missing one-shot repeating trigger'
    Assert-Monitor ($task.Trigger.RepetitionInterval -eq [timespan]::FromMinutes(60)) 'Interval mismatch'
    Assert-Monitor ($task.Action.WorkingDirectory -eq $root) 'Wrong working directory'
    Assert-Monitor ($task.Action.Argument.Contains(('-File "{0}"' -f $runner))) 'Task runner mismatch'
    Assert-Monitor ($task.Action.Argument.Contains(('-UvExecutable "{0}"' -f $fakeUv))) 'uv path mismatch'
    Assert-Monitor (-not $task.Action.Argument.Contains('--include-pacing-trial')) 'Unexpected lease trial'
    Assert-Monitor (-not (Test-Path -LiteralPath $db)) 'Installer wrote evidence history'
    Assert-Monitor (-not (Test-Path -LiteralPath $alert)) 'Installer wrote alert'
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $childArgs = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$runner,
        '-UvExecutable',$fakeUv,'-HostIp',$params.HostIp,'-SshUser',$params.SshUser,
        '-KnownHosts',$hosts,'-RemoteRepo',$params.RemoteRepo,
        '-EvidenceDb',$db,'-AlertFile',$alert)
    $output = & $psExe @childArgs 2>&1 | Out-String
    Assert-Monitor ($LASTEXITCODE -eq 6) 'Runner did not return bounded failure'
    Assert-Monitor ($output.Contains('LAN_MONITOR_RUN_FAILED')) 'Safe failed status absent'
    Assert-Monitor (-not $output.Contains($hosts)) 'Runner leaked private path'
    Assert-Monitor (-not (Test-Path -LiteralPath $db)) 'Fake runner touched real data'
    Write-Output 'LAN monitor offline Windows Task Scheduler checks passed'
} finally {
    $env:PATH = $oldPath
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
