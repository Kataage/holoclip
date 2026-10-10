# Scheduled worker: at most one source acquisition, or (if all sources
# complete on a subsequent tick) one offline A/V preview with technical QA.
param(
  [Parameter(Mandatory=$true)][string]$UvExecutable,
  [Parameter(Mandatory=$true)][string]$QueuePath
)
$ErrorActionPreference = 'Stop'
$repo=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) { exit 6 }
if (-not (Test-Path -LiteralPath $QueuePath -PathType Leaf)) { exit 6 }
Set-Location -LiteralPath $repo
# --offline only disables uv dependency fetching. The acquisition worker
# still uses the authenticated shared-LAN and local yt-dlp pacing gates.
$sourceLines = @( & $UvExecutable run --offline --no-sync holoclip render-source-queue-run --queue $QueuePath --execute 2>&1 )
if ($LASTEXITCODE -ne 0) { exit 6 }
try {
  $sourceReport = ($sourceLines | Select-Object -Last 1 | ConvertFrom-Json -ErrorAction Stop)
} catch { exit 6 }
# Unknown or malformed worker JSON is a hard stop, never a successful skip.
if (-not ($sourceReport -is [pscustomobject])) { exit 6 }
if ($sourceReport.status -notin @(
  'LOCAL_RENDER_QUEUE_ALL_COMPLETE',
  'LOCAL_RENDER_QUEUE_JOB_COMPLETE',
  'LOCAL_RENDER_QUEUE_ONE_SOURCE_ACQUIRED',
  'LOCAL_RENDER_QUEUE_DEFERRED_BY_PACING'
)) { exit 6 }
if ($null -eq $sourceReport.acquired_count -or
    $sourceReport.acquired_count -notin @(0,1)) { exit 6 }
if (($sourceReport.status -eq 'LOCAL_RENDER_QUEUE_ALL_COMPLETE' -and
     $sourceReport.acquired_count -ne 0) -or
    ($sourceReport.status -in @('LOCAL_RENDER_QUEUE_JOB_COMPLETE',
                               'LOCAL_RENDER_QUEUE_ONE_SOURCE_ACQUIRED') -and
     $sourceReport.acquired_count -ne 1)) { exit 6 }
# Rendering is local-only; do not run immediately after a media acquisition:
# preserve this scheduled task's 55-minute deadline and avoid two heavy stages
# in the same run. A next scheduled tick will render after acquisition settles.
if ($sourceReport.status -ne 'LOCAL_RENDER_QUEUE_ALL_COMPLETE' -or
    $sourceReport.acquired_count -ne 0) { exit 0 }
$renderLines = @( & $UvExecutable run --offline --no-sync holoclip render-ready-preview --queue $QueuePath --execute 2>&1 )
if ($LASTEXITCODE -ne 0) { exit 6 }
try {
  $renderReport = ($renderLines | Select-Object -Last 1 | ConvertFrom-Json -ErrorAction Stop)
} catch { exit 6 }
if ($renderReport.status -notin @('LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY',
                                'LOCAL_RENDER_AUTO_QA_NO_READY_JOBS')) { exit 6 }
exit 0
