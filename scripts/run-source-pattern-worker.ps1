# Opt-in Windows Task Scheduler entry point; no credentials, labels or GPU.
param(
    [ValidateRange(1, 20)]
    [int]$Cycles = 3,
    [ValidateRange(1, 2)]
    [int]$MaxClips = 2
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$logRoot = Join-Path $root 'data\logs'
$stateRoot = Join-Path $root 'data\examples'
New-Item -ItemType Directory -Force -Path $logRoot, $stateRoot | Out-Null
$logPath = Join-Path $logRoot ('source-pattern-{0}-{1}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID)
$auditPath = Join-Path $stateRoot 'source-pattern-launcher-last-run.json'
$startedAt = (Get-Date).ToUniversalTime().ToString('o')
$status = 'FAILED_PRELAUNCH'
$errorClass = $null
$ranCollector = $false
$code = 1

try {
    # Identify predictable failures BEFORE running yt-dlp or the collector.
    # This launcher audit is separate from the Python collector's measured audit.
    $uv = Get-Command 'uv' -ErrorAction Stop
    foreach ($binary in @('ffmpeg', 'ffprobe')) {
        $null = Get-Command $binary -ErrorAction Stop
    }
    $ranCollector = $true
    $status = 'FAILED_COLLECTOR_PROCESS'
    # Powershell 5.1 can treat native stderr as terminating errors with
    # ErrorActionPreference=Stop even if the child succeeds; use exit code.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $uv.Source run --extra media holoclip examples-collect-source-patterns --cycles $Cycles --max-clips $MaxClips *>> $logPath
        if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($code -eq 0) { $status = 'COMPLETED_COLLECTOR_PROCESS' }
}
catch {
    $errorClass = $_.Exception.GetType().Name
    $code = 1
    ('Windows source-pattern launcher failed: ' + $errorClass) |
        Out-File -LiteralPath $logPath -Append -Encoding UTF8
}
finally {
    # Record even errors happening BEFORE Python can create its collector audit.
    # Never overwrite/claim a new media measurement or successful source match.
    $audit = @{
        version = 'source-pattern-windows-launcher-v1'
        status = $status
        exit_code = [int]$code
        error_class = $errorClass
        ran_collector = [bool]$ranCollector
        started_at_utc = $startedAt
        finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        source_identity_verified = $false
        reviewed_gold_available = $false
        qualification_allowed = $false
        ready = $false
        production_ready = $false
    }
    $temp = $auditPath + '.' + $PID + '.tmp'
    try {
        $json = $audit | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText(
            $temp, $json, ([System.Text.UTF8Encoding]::new($false))
        )
        if ([System.IO.File]::Exists($auditPath)) {
            [System.IO.File]::Replace($temp, $auditPath, $null)
        } else {
            [System.IO.File]::Move($temp, $auditPath)
        }
    }
    catch {
        $code = 1
        $innerClass = if ($_.Exception.InnerException) {
            $_.Exception.InnerException.GetType().Name
        } else { 'None' }
        $diagnostic = ('Windows launcher audit write failed: ' +
            $_.Exception.GetType().Name + ' inner=' + $innerClass +
            ' message=' + $_.Exception.Message)
        $diagnostic | Out-File -LiteralPath $logPath -Append -Encoding UTF8
    }
    finally {
        Remove-Item -LiteralPath $temp -ErrorAction SilentlyContinue
    }
}
# Task Scheduler must see real process/preflight errors, not an optimistic 0.
exit $code
