# Windows PowerShell 5.1 offline runner regression; synthetic uv.cmd, no media.
$ErrorActionPreference = 'Stop'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-postrender-' + [guid]::NewGuid().ToString('N'))
function Assert-Run([bool]$ok,[string]$reason) { if (-not $ok) { throw $reason } }
try {
  $scripts = Join-Path $tmp 'scripts'
  New-Item -ItemType Directory -Force -Path $scripts | Out-Null
  $runner = Join-Path $scripts 'run-render-source-queue.ps1'
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-render-source-queue.ps1') $runner
  $queue = Join-Path $tmp 'queue.sqlite3'
  [IO.File]::WriteAllText($queue,'fake queue bytes')
  $uv = Join-Path $tmp 'uv.cmd'
  $calls = Join-Path $tmp 'calls.txt'
  $mock = @'
@echo off
echo %* >> "%~dp0calls.txt"
echo %* | findstr /C:"render-source-queue-run" >nul
if not errorlevel 1 (
  if "%MOCK_SOURCE_MODE%"=="all" (
    echo {"status":"LOCAL_RENDER_QUEUE_ALL_COMPLETE","acquired_count":0}
  ) else if "%MOCK_SOURCE_MODE%"=="acquired" (
    echo {"status":"LOCAL_RENDER_QUEUE_JOB_COMPLETE","acquired_count":1}
  ) else if "%MOCK_SOURCE_MODE%"=="blocked" (
    echo {"status":"LOCAL_RENDER_QUEUE_PAUSED_VALIDATION","acquired_count":0}
    exit /b 6
  ) else (
    echo invalid-json
  )
  exit /b 0
)
echo {"status":"LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY","rendered_count":1}
exit /b 0
'@
  [IO.File]::WriteAllText($uv, $mock, [Text.Encoding]::ASCII)
  $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  foreach ($mode in @('all','acquired','blocked','badjson')) {
    $env:MOCK_SOURCE_MODE=$mode
    if (Test-Path -LiteralPath $calls) { Remove-Item -LiteralPath $calls -Force }
    $null = & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -UvExecutable $uv -QueuePath $queue 2>&1
    $exitCode=$LASTEXITCODE
    $history=@(Get-Content -LiteralPath $calls)
    if ($mode -eq 'all') {
      Assert-Run ($exitCode -eq 0) 'Render stage should run after sources complete'
      Assert-Run ($history.Count -eq 2) 'Expected source and one local render invocation'
      Assert-Run ($history[1] -like '*render-ready-preview*') 'Missing render worker command'
    } elseif ($mode -eq 'acquired') {
      Assert-Run ($exitCode -eq 0) 'Source acquired but skip render this tick'
      Assert-Run ($history.Count -eq 1) 'Never immediately encode after download'
    } else {
      Assert-Run ($exitCode -eq 6) 'Unsafe source status must fail closed'
      Assert-Run ($history.Count -eq 1) 'Never render after unsafe source status'
    }
  }
  Write-Output 'Offline Windows post-acquisition source/render runner passed'
} finally {
  Remove-Item Env:MOCK_SOURCE_MODE -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $tmp -Force -Recurse -ErrorAction SilentlyContinue
}
