# Long-lived offline-only LAN coordinator. No yt-dlp, YouTube or downloads.
param(
    [Parameter(Mandatory=$true)][string]$UvExecutable,
    [Parameter(Mandatory=$true)][string]$BindIp,
    [ValidateRange(1024, 65535)][int]$Port = 8878,
    [Parameter(Mandatory=$true)][string]$SecretFile,
    [Parameter(Mandatory=$true)][string]$StatePath,
    [ValidateRange(0, 100)][int]$MaxRestarts = 0,
    [ValidateRange(0, 300)][int]$RetryDelaySeconds = 15
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot
if (-not [IO.Path]::IsPathRooted($UvExecutable) -or
        -not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) {
    throw 'Pinned uv executable missing; coordinator stays offline'
}
if (-not [IO.Path]::IsPathRooted($SecretFile) -or
        -not [IO.Path]::IsPathRooted($StatePath)) {
    throw 'Coordinator credential and state paths must be absolute'
}
# Mutex prevents duplicate watchers in this logon session. Port bind
# independently prevents concurrent servers, including manual launches.
$key = [Text.Encoding]::UTF8.GetBytes(($repoRoot + '|' + $BindIp + '|' + $Port))
$hash = [Security.Cryptography.SHA256]::Create()
try { $id = ([BitConverter]::ToString($hash.ComputeHash($key))).Replace('-', '').Substring(0, 24) }
finally { $hash.Dispose() }
$mutex = New-Object System.Threading.Mutex($false, ('Local\HoloclipYtdlpLan-' + $id))
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Host 'LAN coordinator already supervised by another local task'
        exit 4
    }
    $env:PATH = (Split-Path -Parent $UvExecutable) + [IO.Path]::PathSeparator + $env:PATH
    $restarts = 0
    while ($true) {
        $exitCode = 1
        try {
            # Both flags prevent scheduled dependency updates or downloads.
            $arguments = @('run', '--offline', '--no-sync', 'holoclip',
                'examples-ytdlp-lan-serve', '--bind-ip', $BindIp, '--port', [string]$Port,
                '--secret-file', $SecretFile, '--state-path', $StatePath)
            & $UvExecutable @arguments
            $exitCode = [int]$LASTEXITCODE
        }
        catch {
            # Never print raw exception text or credential file contents.
            $exitCode = 1
        }
        Write-Host ('LAN coordinator exited; code={0}; restart_count={1}' -f $exitCode, $restarts)
        if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) {
            if ($exitCode -eq 0) { $exitCode = 1 }
            exit $exitCode
        }
        $delay = [int][Math]::Min(300, $RetryDelaySeconds * [Math]::Pow(2, [Math]::Min($restarts, 5)))
        $restarts++
        if ($delay -gt 0) { Start-Sleep -Seconds $delay }
    }
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
