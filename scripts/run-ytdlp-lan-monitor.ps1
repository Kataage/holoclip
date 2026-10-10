# Current-user Task Scheduler entrypoint. Never auto-test leases or contact YouTube.
param(
    [Parameter(Mandatory=$true)][string]$UvExecutable,
    [Parameter(Mandatory=$true)][string]$HostIp,
    [Parameter(Mandatory=$true)][string]$SshUser,
    [Parameter(Mandatory=$true)][string]$KnownHosts,
    [Parameter(Mandatory=$true)][string]$RemoteRepo,
    [ValidateRange(1,65535)][int]$SshPort = 22,
    [ValidateRange(1024,65535)][int]$CoordinatorPort = 8878,
    [Parameter(Mandatory=$true)][string]$EvidenceDb,
    [Parameter(Mandatory=$true)][string]$AlertFile
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
try {
    Set-Location -LiteralPath $repo
    $argsList = @('run', '--offline', '--no-sync', 'holoclip',
        'examples-ytdlp-lan-monitor-run',
        '--host', $HostIp, '--user', $SshUser,
        '--known-hosts', $KnownHosts, '--remote-repo', $RemoteRepo,
        '--ssh-port', [string]$SshPort, '--coordinator-port', [string]$CoordinatorPort,
        '--evidence-db', $EvidenceDb, '--alert-file', $AlertFile)
    # Avoid stdout, stderr and paths in unattended Task Scheduler history.
    & $UvExecutable @argsList 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output 'LAN_MONITOR_RUN_FAILED'
        exit 6
    }
    Write-Output 'LAN_MONITOR_RUN_FINISHED'
    exit 0
} catch {
    Write-Output 'LAN_MONITOR_RUN_FAILED'
    exit 6
}
