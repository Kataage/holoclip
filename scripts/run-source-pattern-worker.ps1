# Task Scheduler entry point: local, CPU-only bounded public-video diagnostic.
# Repository state and video caches remain on this machine, never in GitHub CI.
param(
    [int]$Cycles = 3,
    [int]$MaxClips = 2
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$logRoot = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logPath = Join-Path $logRoot ('source-pattern-{0}-{1}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID)
# Keep a finite number of task logs. No arbitrary user files are removed.
Get-ChildItem -LiteralPath $logRoot -Filter 'source-pattern-*.log' -File |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -Skip 29 | Remove-Item -Force

$uv = Get-Command 'uv' -ErrorAction Stop
& $uv.Source run --extra media holoclip examples-collect-source-patterns --cycles $Cycles --max-clips $MaxClips *>> $logPath
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
