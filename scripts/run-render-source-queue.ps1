# Scheduled worker: one source maximum per invocation, persisted crash fail-closed.
param(
  [Parameter(Mandatory=$true)][string]$UvExecutable,
  [Parameter(Mandatory=$true)][string]$QueuePath
)
$ErrorActionPreference = 'Stop'
$repo=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) { exit 6 }
if (-not (Test-Path -LiteralPath $QueuePath -PathType Leaf)) { exit 6 }
Set-Location -LiteralPath $repo
# Offline here refers to uv's Python dependency sync, NOT to the explicitly
# authorized yt-dlp source request inside the queue worker.
$null = & $UvExecutable run --offline --no-sync holoclip render-source-queue-run --queue $QueuePath --execute 2>&1
if ($LASTEXITCODE -ne 0) { exit 6 }
exit 0
