# Opt-in one-time local installation; does NOT start the collector.
param(
    [string]$TaskName = 'Holoclip Source Patterns',
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '03:00',
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'Windows Task Scheduler is required'
}
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run-source-pattern-worker.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'Source-pattern task runner is missing'
}
# Refuse to register a task that cannot currently find essential local tools.
# Resolve uv to an absolute executable because scheduled user sessions may
# have a different PATH than the interactive installing PowerShell.
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    throw 'Scheduled task name must not be blank'
}
$uv = Get-Command 'uv' -CommandType Application -ErrorAction Stop
$ffmpeg = Get-Command 'ffmpeg' -CommandType Application -ErrorAction Stop
$ffprobe = Get-Command 'ffprobe' -CommandType Application -ErrorAction Stop

# The extractor requires Deno >= 2.3 or Node >= 22. Prefer supported Deno
# but allow modern Node when Deno is absent or too old. No downloads/installs.
$jsPath = $null
foreach ($candidate in @(
    @{ Name = 'deno'; Minimum = [version]'2.3.0' },
    @{ Name = 'node'; Minimum = [version]'22.0.0' }
)) {
    $binary = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $binary) { continue }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $versionOutput = & $binary.Source --version 2>$null
        $versionExitCode = $LASTEXITCODE
    }
    catch {
        continue
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($versionExitCode -ne 0) { continue }
    $firstLine = [string]($versionOutput | Select-Object -First 1)
    $match = [regex]::Match($firstLine, '(?<!\d)(\d+)\.(\d+)\.(\d+)')
    if (-not $match.Success) { continue }
    $candidateVersion = [version]::new(
        [int]$match.Groups[1].Value,
        [int]$match.Groups[2].Value,
        [int]$match.Groups[3].Value
    )
    if ($candidateVersion -ge $candidate.Minimum) {
        $jsPath = $binary.Source
        break
    }
}
if (-not $jsPath) {
    throw 'Deno >= 2.3 or Node >= 22 must be installed before registering the task'
}
$uvPath = $uv.Source
$ffmpegPath = $ffmpeg.Source
$ffprobePath = $ffprobe.Source
foreach ($path in @($uvPath, $ffmpegPath, $ffprobePath, $jsPath)) {
    if (-not [System.IO.Path]::IsPathRooted($path) -or
            -not (Test-Path -LiteralPath $path -PathType Leaf) -or
            $path.Contains('"')) {
        throw 'Invalid executable path for scheduled media tool'
    }
}
# Resolve once; Windows Task Scheduler only stores an action and does NOT
# inherit the launching PowerShell's functions or command aliases.
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskArgs = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
    '-UvExecutable "{1}" -FFmpegExecutable "{2}" -FFprobeExecutable "{3}" ' +
    '-JsRuntimeExecutable "{4}"') -f $runner, $uvPath, $ffmpegPath, $ffprobePath, $jsPath
# An explicit config path is pinned in the task; the default JSON is
# automatically discovered by the runner and can be edited later without
# reinstalling the task. Refuse missing or unsafe external config paths.
if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
    $configAbsolute = if ([System.IO.Path]::IsPathRooted($ConfigPath)) {
        [System.IO.Path]::GetFullPath($ConfigPath)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ConfigPath))
    }
    if (-not (Test-Path -LiteralPath $configAbsolute -PathType Leaf) -or
            $configAbsolute.Contains('"')) {
        throw 'Invalid custom source-pattern config file'
    }
    $configFile = Get-Item -LiteralPath $configAbsolute
    if (($configFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Source-pattern config cannot be a reparse point'
    }
    $taskArgs += (' -ConfigPath "{0}"' -f $configAbsolute)
}
$action = New-ScheduledTaskAction -Execute $psExe -Argument $taskArgs -WorkingDirectory $repoRoot
$startAt = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
# Zero execution limit avoids scheduler hard-terminating a partially written
# media/measurement step. Overlapping task instances are skipped.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Bounded unreviewed public Holoclip source-video controls (CPU-only)' -Force | Out-Null
Write-Host "Registered local daily source-pattern collector: $TaskName at $At"
Write-Host 'The task has not been started. Disable it to stop future automatic runs.'
