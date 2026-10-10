# Read-only Windows Task Scheduler first-run audit. Never starts or edits tasks.
# Emits ONLY fixed status codes and an optional last-run Unix timestamp.
param(
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$HostIp,
    [Parameter(Mandatory=$true)][string]$SshUser,
    [Parameter(Mandatory=$true)][string]$KnownHosts,
    [Parameter(Mandatory=$true)][string]$RemoteRepo,
    [ValidateRange(1,65535)][int]$SshPort = 22,
    [ValidateRange(1024,65535)][int]$CoordinatorPort = 8878,
    [ValidateRange(15,1440)][int]$IntervalMinutes = 60,
    [Parameter(Mandatory=$true)][string]$EvidenceDb,
    [Parameter(Mandatory=$true)][string]$AlertFile
)
$ErrorActionPreference = 'Stop'
function Emit([string]$status,[string]$execution,[object]$when,[int]$code) {
    [pscustomobject]@{
        status=$status;task_execution=$execution;last_run_unix=$when;ready=$false
    } | ConvertTo-Json -Compress -Depth 2
    exit $code
}
try {
    if ($TaskName -cnotmatch '^[A-Za-z0-9 _-]{1,90}$' -or
        $SshUser -cnotmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,63}$') {
        Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
    }
    foreach ($value in @($HostIp,$KnownHosts,$RemoteRepo,$EvidenceDb,$AlertFile)) {
        if ([string]::IsNullOrWhiteSpace($value) -or $value.Contains('"') -or
            $value.IndexOfAny([char[]]@([char]0,[char]10,[char]13)) -ge 0) {
            Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
        }
    }
    $found = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $TaskName })
    if ($found.Count -eq 0) {
        Emit 'LAN_FIRST_RUN_TASK_ABSENT' 'LAN_FIRST_RUN_NEVER_RUN' $null 0
    }
    if ($found.Count -ne 1) {
        Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
    }
    $task = $found[0]
    if ($task.State -eq 'Disabled') {
        Emit 'LAN_FIRST_RUN_TASK_DISABLED' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 0
    }
    $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $runner = Join-Path $PSScriptRoot 'run-ytdlp-lan-monitor.ps1'
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $uv = (Get-Command 'uv' -CommandType Application -ErrorAction Stop).Source
    $expected = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
        '-UvExecutable "{1}" -HostIp "{2}" -SshUser "{3}" -KnownHosts "{4}" ' +
        '-RemoteRepo "{5}" -SshPort {6} -CoordinatorPort {7} ' +
        '-EvidenceDb "{8}" -AlertFile "{9}"') -f @(
            $runner,$uv,$HostIp,$SshUser,$KnownHosts,$RemoteRepo,
            $SshPort,$CoordinatorPort,$EvidenceDb,$AlertFile
        )
    $actions = @($task.Actions)
    $triggers = @($task.Triggers)
    $good = ($actions.Count -eq 1 -and $triggers.Count -eq 1 -and
        $actions[0].Execute -eq $powerShell -and
        $actions[0].Arguments -ceq $expected -and
        $actions[0].WorkingDirectory -eq $repoRoot -and
        $task.Principal.LogonType -eq 'Interactive' -and
        $task.Principal.RunLevel -eq 'Limited' -and
        $task.Settings.MultipleInstances -eq 'IgnoreNew' -and
        $task.Settings.ExecutionTimeLimit -eq 'PT3M')
    if ($good) {
        try {
            $period = [Xml.XmlConvert]::ToTimeSpan([string]$triggers[0].Repetition.Interval)
            $good = ($period -eq [timespan]::FromMinutes($IntervalMinutes))
        } catch { $good = $false }
    }
    if (-not $good) {
        Emit 'LAN_FIRST_RUN_TASK_CONFIG_MISMATCH' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 0
    }
    if ($task.State -ne 'Ready' -and $task.State -ne 'Running') {
        Emit 'LAN_FIRST_RUN_TASK_STATE_UNVERIFIED' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
    if ($null -eq $info -or $null -eq $info.LastRunTime) {
        Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
    }
    $last = [datetime]$info.LastRunTime
    if ($last.Year -lt 2000) {
        Emit 'LAN_FIRST_RUN_TASK_CONFIG_MATCHED' 'LAN_FIRST_RUN_NEVER_RUN' $null 0
    }
    $epoch = [long]([datetimeoffset]$last.ToUniversalTime()).ToUnixTimeSeconds()
    if ($epoch -lt 0) {
        Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
    }
    if ([int]$info.LastTaskResult -ne 0) {
        Emit 'LAN_FIRST_RUN_TASK_CONFIG_MATCHED' 'LAN_FIRST_RUN_LAST_RUN_FAILED' $epoch 0
    }
    Emit 'LAN_FIRST_RUN_TASK_CONFIG_MATCHED' 'LAN_FIRST_RUN_LAST_RUN_SUCCEEDED' $epoch 0
} catch {
    Emit 'LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE' 'LAN_FIRST_RUN_LAST_RUN_UNVERIFIED' $null 6
}
