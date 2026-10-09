# Opt-in non-elevated Windows logon startup. Does not start a server or access YouTube.
param(
    [string]$TaskName = 'Holoclip yt-dlp LAN Coordinator',
    [string]$BindIp = '127.0.0.1',
    [ValidateRange(1024, 65535)][int]$Port = 8878,
    [string]$SecretFile = '',
    [string]$StatePath = ''
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'Windows Task Scheduler is required'
}
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run-ytdlp-lan-coordinator.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'LAN coordinator task runner is missing'
}
if ($TaskName -cnotmatch '^[A-Za-z0-9 _-]{1,90}$') {
    throw 'Invalid LAN coordinator task name'
}
if ($BindIp -cnotmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}$') {
    throw 'LAN bind IP must be a literal IPv4'
}
try { $ip = [Net.IPAddress]::Parse($BindIp) }
catch { throw 'Invalid LAN bind IPv4 address' }
if ($ip.ToString() -cne $BindIp) {
    throw 'Noncanonical LAN bind IPv4 address'
}
$b = $ip.GetAddressBytes()
if (-not ($b[0] -eq 127 -or $b[0] -eq 10 -or
          ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
          ($b[0] -eq 192 -and $b[1] -eq 168))) {
    throw 'LAN bind IP must be loopback or RFC1918'
}
function Resolve-SafeLanPath([string]$value, [string]$defaultRelative) {
    if ([string]::IsNullOrWhiteSpace($value)) { $value = $defaultRelative }
    $full = if ([IO.Path]::IsPathRooted($value)) {
        [IO.Path]::GetFullPath($value)
    } else {
        [IO.Path]::GetFullPath((Join-Path $repoRoot $value))
    }
    if ($full.Contains('"') -or $full.IndexOfAny([char[]]@([char]0, [char]10, [char]13)) -ge 0) {
        throw 'Unsafe LAN storage path'
    }
    $walk = $full
    while ($walk) {
        if (Test-Path -LiteralPath $walk) {
            $entry = Get-Item -LiteralPath $walk -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'LAN storage paths cannot contain reparse points'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($walk)
        if (-not $parent -or $parent -eq $walk) { break }
        $walk = $parent
    }
    return $full
}
$secret = Resolve-SafeLanPath $SecretFile 'data\runtime\yt-dlp-lan-secret.txt'
$state = Resolve-SafeLanPath $StatePath 'data\runtime\yt-dlp-lan-coordinator.sqlite3'
if ($secret -eq $state) { throw 'Secret and coordinator state must be separate files' }
if (-not (Test-Path -LiteralPath $secret -PathType Leaf)) {
    throw 'Run examples-ytdlp-lan-init before installing the task'
}
$secretInfo = Get-Item -LiteralPath $secret
if ($secretInfo.Length -lt 64 -or $secretInfo.Length -gt 128 -or
        ((Get-Content -LiteralPath $secret -Raw -Encoding ASCII).Trim() -cnotmatch '^[a-f0-9]{64}$')) {
    throw 'LAN credential has an invalid format'
}
if (Test-Path -LiteralPath $state -PathType Container) {
    throw 'LAN coordinator SQLite path is a directory'
}
$uv = Get-Command 'uv' -CommandType Application -ErrorAction Stop
$uvPath = $uv.Source
if (-not [IO.Path]::IsPathRooted($uvPath) -or
        -not (Test-Path -LiteralPath $uvPath -PathType Leaf) -or
        $uvPath.Contains('"')) {
    throw 'Invalid uv executable for LAN coordinator task'
}
# --no-sync is used by runner, so the existing environment is mandatory.
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.venv\Scripts\python.exe') -PathType Leaf)) {
    throw 'Holoclip .venv is missing; run uv sync before registering the task'
}
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskArgs = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
    '-UvExecutable "{1}" -BindIp "{2}" -Port {3} ' +
    '-SecretFile "{4}" -StatePath "{5}"') -f $runner, $uvPath, $BindIp, $Port, $secret, $state
$action = New-ScheduledTaskAction -Execute $psExe -Argument $taskArgs -WorkingDirectory $repoRoot
# Current user's logon rather than SYSTEM at boot; no stored password/elevation.
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Trusted-LAN-only authenticated yt-dlp pacing; no media collection' -Force | Out-Null
Write-Host "Registered LAN coordinator for current-user logon: $TaskName"
Write-Host 'Not started. Confirm local firewall scope, then use Start-ScheduledTask.'
