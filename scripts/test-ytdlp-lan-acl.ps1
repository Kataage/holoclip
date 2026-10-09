# Windows isolated NTFS ACL smoke tests; only disposable temp files/dirs.
# Never touches the user's real Holoclip credentials or downloads media.
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-lan-acl-test-' + [guid]::NewGuid().ToString('N'))
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$tool = Join-Path $PSScriptRoot 'secure-ytdlp-lan-acl.ps1'
function Assert-LocalAcl([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Run-Acl([string]$path, [bool]$apply) {
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $tool, '-Path', $path)
    if ($apply) { $arguments += '-Apply' }
    $out = & $psExe @arguments | Out-String
    return [pscustomobject]@{ ExitCode = [int]$LASTEXITCODE; Raw = $out; Data = ($out | ConvertFrom-Json) }
}
try {
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    $tokens = $null; $parseErrors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile(
        $tool, [ref]$tokens, [ref]$parseErrors)
    Assert-LocalAcl ($parseErrors.Count -eq 0) 'ACL script has a PowerShell 5.1 syntax error'
    $secret = Join-Path $testRoot 'my temporary secret file.txt'
    [IO.File]::WriteAllText($secret, ('a' * 64) + [Environment]::NewLine)
    $hardened = Run-Acl $secret $true
    Write-Host ('ACL smoke result: ' + $hardened.Data.status)
    Assert-LocalAcl ($hardened.ExitCode -eq 0) 'Lockdown must succeed on current-user-owned temp file'
    Assert-LocalAcl ($hardened.Data.status -eq 'ACL_SECURE') 'Lockdown failed NTFS DACL audit'
    Assert-LocalAcl ($hardened.Data.acl_verified -eq $true) 'Lockdown did not verify ACL'
    Assert-LocalAcl ($hardened.Data.ready -eq $false) 'ACL alone must never promote production readiness'
    Assert-LocalAcl (-not $hardened.Raw.Contains(('a' * 64))) 'Lockdown leaked secret bytes'
    $readOnly = Run-Acl $secret $false
    Assert-LocalAcl ($readOnly.ExitCode -eq 0) 'Read-only check must preserve secured ACL'
    $directory = Join-Path $testRoot 'private-stage'
    New-Item -ItemType Directory -Path $directory | Out-Null
    $securedDir = Run-Acl $directory $true
    Assert-LocalAcl ($securedDir.ExitCode -eq 0) 'Directory ACL lockdown failed'
    $missing = Run-Acl (Join-Path $testRoot 'not-there') $false
    Assert-LocalAcl ($missing.ExitCode -eq 6) 'Missing target must fail closed'
    Assert-LocalAcl ($missing.Data.acl_verified -eq $false) 'Missing ACL target falsely secure'
    Assert-LocalAcl (-not $missing.Raw.Contains($testRoot)) 'Missing-path check leaked machine path'
    Write-Host 'Windows NTFS ACL smoke passed on isolated temporary path; no real Holoclip secret used'
}
finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
