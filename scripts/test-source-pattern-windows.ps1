# Windows PowerShell 5.1 integration smoke tests, run by GitHub Actions only.
# No real task registration, yt-dlp, internet/video, or local user state changes.
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ('holoclip-win-runner-test-' + [guid]::NewGuid().ToString('N'))
$fakeBin = Join-Path $temporary 'fake-bin'
$fakeScripts = Join-Path $temporary 'scripts'
$originalPath = $env:PATH

function Assert-Condition([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Create-Mock([string]$name, [string]$body) {
    $path = Join-Path $fakeBin ($name + '.cmd')
    [System.IO.File]::WriteAllText($path, ("@echo off`r`n" + $body + "`r`n"),
        ([System.Text.Encoding]::ASCII))
}
function Run-Launcher([string]$expectedStatus, [int]$expectedCode, [bool]$expectedInvocation,
                      [string]$UvOverride = '') {
    $marker = Join-Path $temporary 'was-invoked.txt'
    Remove-Item -LiteralPath $marker -ErrorAction SilentlyContinue
    $args = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', (Join-Path $fakeScripts 'run-source-pattern-worker.ps1'),
        '-Cycles', '1', '-MaxClips', '1')
    if ($UvOverride) { $args += @('-UvExecutable', $UvOverride) }
    & $psExe @args
    $actualCode = [int]$LASTEXITCODE
    $auditFile = Join-Path $temporary 'data\examples\source-pattern-launcher-last-run.json'
    if ($actualCode -ne $expectedCode) {
        if (Test-Path -LiteralPath $auditFile) {
            Write-Host ('Actual launcher audit: ' + (Get-Content -LiteralPath $auditFile -Raw -Encoding UTF8))
        }
        Get-ChildItem -LiteralPath (Join-Path $temporary 'data\logs') -Filter 'source-pattern-*.log' -File -ErrorAction SilentlyContinue |
            ForEach-Object {
                Write-Host ('Launcher log ' + $_.Name)
                Get-Content -LiteralPath $_.FullName -Tail 20 | ForEach-Object { Write-Host $_ }
            }
    }
    Assert-Condition ($actualCode -eq $expectedCode) "Wrong Windows launcher exit code: $actualCode, expected $expectedCode"
    Assert-Condition (Test-Path -LiteralPath $auditFile -PathType Leaf) 'Launcher audit was not created'
    $audit = Get-Content -LiteralPath $auditFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Condition ($audit.version -eq 'source-pattern-windows-launcher-v1') 'Wrong audit schema'
    Assert-Condition ($audit.status -eq $expectedStatus) "Wrong launcher state: $($audit.status), expected $expectedStatus"
    Assert-Condition ($audit.exit_code -eq $expectedCode) 'Audit exit code differs from process'
    Assert-Condition ($audit.ran_collector -eq $expectedInvocation) 'Audit invocation provenance is wrong'
    Assert-Condition ((Test-Path -LiteralPath $marker) -eq $expectedInvocation) 'Mock uv execution mismatch'
    foreach ($flag in @('source_identity_verified', 'reviewed_gold_available',
                        'qualification_allowed', 'ready', 'production_ready')) {
        Assert-Condition ($audit.$flag -eq $false) "Forbidden positive audit flag: $flag"
    }
    $started = [DateTimeOffset]::Parse($audit.started_at_utc)
    $finished = [DateTimeOffset]::Parse($audit.finished_at_utc)
    Assert-Condition ($finished -ge $started) 'Invalid audit time ordering'
    Assert-Condition (-not (Test-Path -LiteralPath ($auditFile + '.' + $PID + '.tmp'))) 'Stale audit tempfile'
    return $audit
}

try {
    New-Item -ItemType Directory -Force -Path $fakeBin, $fakeScripts | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-source-pattern-worker.ps1') -Destination $fakeScripts
    # Syntactic check only; never execute task registration in CI.
    foreach ($scriptName in @('run-source-pattern-worker.ps1', 'install-source-pattern-task.ps1')) {
        $tokens = $null
        $parseErrors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $PSScriptRoot $scriptName),
            [ref]$tokens, [ref]$parseErrors)
        Assert-Condition ($parseErrors.Count -eq 0) "Windows PS 5.1 parse failed for $scriptName"
    }
    # Windows builtin directories needed for PowerShell process startup.
    # Isolating PATH ensures the test never accidentally invokes a real uv or ffmpeg.
    $env:PATH = ($fakeBin, (Join-Path $env:SystemRoot 'System32'),
                 $env:SystemRoot, (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')) -join ';'
    $env:HOLOCLIP_FAKE_UV_MARKER = Join-Path $temporary 'was-invoked.txt'
    Create-Mock 'uv' 'echo invoked >> "%HOLOCLIP_FAKE_UV_MARKER%"' 
    $first = Run-Launcher 'FAILED_PRELAUNCH' 1 $false
    Assert-Condition ($first.error_class -ne $null) 'Expected prelaunch exception class'
    # Verify the same audit file is replaced safely on subsequent attempts.
    Create-Mock 'ffmpeg' 'exit /b 0'
    Create-Mock 'ffprobe' 'exit /b 0'
    Create-Mock 'uv' "echo invoked >> `"%HOLOCLIP_FAKE_UV_MARKER%`"`r`necho fake nonfatal stderr 1>&2`r`nexit /b 7"
    $second = Run-Launcher 'FAILED_COLLECTOR_PROCESS' 7 $true
    Assert-Condition ($second.error_class -eq $null) 'Nonzero native exit should not be a PS preflight exception'
    Create-Mock 'uv' "echo invoked >> `"%HOLOCLIP_FAKE_UV_MARKER%`"`r`necho fake nonfatal stderr 1>&2`r`nexit /b 0"
    $third = Run-Launcher 'COMPLETED_COLLECTOR_PROCESS' 0 $true
    Assert-Condition ($third.error_class -eq $null) 'Success must not contain an exception'
    # A task-installed absolute uv path must work even when uv is NOT in PATH
    # and its parent directory contains spaces.
    $pinnedDir = Join-Path $temporary 'Pinned Executables With Spaces'
    New-Item -ItemType Directory -Force -Path $pinnedDir | Out-Null
    $pinnedUv = Join-Path $pinnedDir 'uv.cmd'
    Copy-Item -LiteralPath (Join-Path $fakeBin 'uv.cmd') -Destination $pinnedUv
    Remove-Item -LiteralPath (Join-Path $fakeBin 'uv.cmd') -Force
    $fourth = Run-Launcher 'COMPLETED_COLLECTOR_PROCESS' 0 $true $pinnedUv
    Assert-Condition ($fourth.error_class -eq $null) 'Pinned uv path must start'
    $fifth = Run-Launcher 'FAILED_PRELAUNCH' 1 $false (Join-Path $pinnedDir 'missing-uv.exe')
    Assert-Condition ($fifth.error_class -ne $null) 'Missing pinned uv must fail closed'
    # Runner only: no user task should be registered.
    Write-Host 'WindowsPowerShell 5.1 launcher: 5 sandboxed scenarios passed (no video download)'
}
finally {
    $env:PATH = $originalPath
    Remove-Item Env:\HOLOCLIP_FAKE_UV_MARKER -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporary -Force -Recurse -ErrorAction SilentlyContinue
}
