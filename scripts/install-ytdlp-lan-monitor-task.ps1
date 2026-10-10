# Explicit current-user bounded periodic LAN diagnosis. No saved password,
# no start, no overwrite, no lease/media command and no network during setup.
param(
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$HostIp,
    [Parameter(Mandatory=$true)][string]$SshUser,
    [Parameter(Mandatory=$true)][string]$KnownHosts,
    [Parameter(Mandatory=$true)][string]$RemoteRepo,
    [ValidateRange(1, 65535)][int]$SshPort = 22,
    [ValidateRange(1024, 65535)][int]$CoordinatorPort = 8878,
    [ValidateRange(15, 1440)][int]$IntervalMinutes = 60,
    [Parameter(Mandatory=$true)][string]$EvidenceDb,
    [Parameter(Mandatory=$true)][string]$AlertFile
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'Windows Task Scheduler required'
}
if ($TaskName -cnotmatch '^[A-Za-z0-9 _-]{1,90}$') {
    throw 'Invalid monitor task name'
}
if ($SshUser -cnotmatch '^[A-Za-z0-9._-]{1,64}$') {
    throw 'Invalid SSH account'
}
try { $ip = [Net.IPAddress]::Parse($HostIp) }
catch { throw 'Invalid private IPv4' }
$b = $ip.GetAddressBytes()
if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $ip.ToString() -cne $HostIp -or
        -not ($b[0] -eq 10 -or
        ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
        ($b[0] -eq 192 -and $b[1] -eq 168))) {
    throw 'PC-B must be a canonical RFC1918 IPv4'
}
foreach ($value in @($KnownHosts, $RemoteRepo, $EvidenceDb, $AlertFile)) {
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Contains('"') -or
            $value.IndexOfAny([char[]]@([char]0, [char]10, [char]13)) -ge 0) {
        throw 'Unsafe monitor arguments'
    }
}
if ($RemoteRepo -notmatch '^[A-Za-z]:[\/\\]') {
    throw 'PC-B repository must be absolute'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath $KnownHosts -PathType Leaf)) {
    throw 'Pinned host key file must already exist'
}
foreach ($file in @($EvidenceDb, $AlertFile, $KnownHosts)) {
    $walk = [IO.Path]::GetFullPath($file)
    while ($walk) {
        if (Test-Path -LiteralPath $walk) {
            $item = Get-Item -LiteralPath $walk -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Monitor paths cannot contain reparse points'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($walk)
        if (-not $parent -or $parent -eq $walk) { break }
        $walk = $parent
    }
}
$runner = Join-Path $PSScriptRoot 'run-ytdlp-lan-monitor.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'LAN monitor runner missing'
}
if (-not (Test-Path -LiteralPath (Join-Path $repo '.venv\Scripts\python.exe') -PathType Leaf)) {
    throw 'Holoclip uv environment missing'
}
$uv = Get-Command 'uv' -CommandType Application -ErrorAction Stop
if (-not [IO.Path]::IsPathRooted($uv.Source) -or
        -not (Test-Path -LiteralPath $uv.Source -PathType Leaf) -or
        $uv.Source.Contains('"')) {
    throw 'Invalid uv executable'
}
$found = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $TaskName })
if ($found.Count -ne 0) {
    throw 'Monitor task already exists; never overwrite'
}
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
    '-UvExecutable "{1}" -HostIp "{2}" -SshUser "{3}" -KnownHosts "{4}" ' +
    '-RemoteRepo "{5}" -SshPort {6} -CoordinatorPort {7} ' +
    '-EvidenceDb "{8}" -AlertFile "{9}"') -f @(
        $runner, $uv.Source, $HostIp, $SshUser, $KnownHosts, $RemoteRepo,
        $SshPort, $CoordinatorPort, $EvidenceDb, $AlertFile
    )
$action = New-ScheduledTaskAction -Execute $psExe -Argument $arguments -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval ([timespan]::FromMinutes($IntervalMinutes)) -RepetitionDuration ([timespan]::FromDays(3650))
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([timespan]::FromMinutes(3)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Offline-media-free Holoclip signed LAN diagnostics; repeated alerts only' | Out-Null
Write-Output 'LAN_MONITOR_TASK_REGISTERED_NOT_STARTED'
