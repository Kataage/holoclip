# Opt-in Windows Task Scheduler entry point; no credentials, labels or GPU.
param(
    [ValidateRange(1, 20)]
    [int]$Cycles = 3,
    [ValidateRange(1, 2)]
    [int]$MaxClips = 2,
    [string]$UvExecutable = '',
    [string]$FFmpegExecutable = '',
    [string]$FFprobeExecutable = '',
    [string]$JsRuntimeExecutable = '',
    [string]$ConfigPath = ''
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
    # Optional local-only configuration. A missing default preserves existing
    # settings; a user-specified missing/invalid path must fail prelaunch.
    # Nothing in config is evaluated as PowerShell or passed to a shell.
    $configExplicit = -not [string]::IsNullOrWhiteSpace($ConfigPath)
    if (-not $configExplicit) {
        $ConfigPath = Join-Path $stateRoot 'source-pattern-local-settings.json'
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        if ($configExplicit) { throw 'Explicit source-pattern config file does not exist' }
    } else {
        $configFile = Get-Item -LiteralPath $ConfigPath -ErrorAction Stop
        if (($configFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Source-pattern config must not be a symlink or reparse point'
        }
        if ($configFile.Length -gt 32768) {
            throw 'Source-pattern config exceeds 32 KiB'
        }
        $settings = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $settings -or -not ($settings -is [pscustomobject])) {
            throw 'Source-pattern config must be a JSON object'
        }
        $allowed = @(
            'version', 'store', 'state', 'output', 'policy_file',
            'report_path', 'launcher_audit_path', 'cycles', 'max_clips',
            'max_candidates', 'scan_limit', 'target_distinct_streams',
            'discover'
        )
        foreach ($name in $settings.PSObject.Properties.Name) {
            if ($name -cnotin $allowed) {
                throw 'Source-pattern config contains an unsupported key'
            }
        }
        if ($settings.version -cne 'source-pattern-local-settings-v1') {
            throw 'Unsupported source-pattern config version'
        }
        $configuration = @{}
        foreach ($name in @('store', 'state', 'output', 'policy_file',
                             'report_path', 'launcher_audit_path')) {
            if ($settings.PSObject.Properties.Name -notcontains $name) { continue }
            $value = $settings.$name
            if (-not ($value -is [string]) -or
                    [string]::IsNullOrWhiteSpace($value) -or
                    $value.IndexOfAny([char[]]@([char]10, [char]13, [char]0)) -ge 0 -or
                    $value.Contains('"')) {
                throw 'Invalid source-pattern config path'
            }
            $absolute = if ([System.IO.Path]::IsPathRooted($value)) {
                [System.IO.Path]::GetFullPath($value)
            } else {
                [System.IO.Path]::GetFullPath((Join-Path $root $value))
            }
            $configuration[$name] = $absolute
        }
        $integerBounds = @{
            cycles = @(1, 20); max_clips = @(1, 2)
            max_candidates = @(1, 2); scan_limit = @(1, 100)
            target_distinct_streams = @(1, 50)
        }
        foreach ($name in $integerBounds.Keys) {
            if ($settings.PSObject.Properties.Name -notcontains $name) { continue }
            $value = $settings.$name
            if (($value -isnot [int] -and $value -isnot [long]) -or
                    $value -lt $integerBounds[$name][0] -or
                    $value -gt $integerBounds[$name][1]) {
                throw 'Invalid bounded source-pattern config number'
            }
            $configuration[$name] = [int]$value
        }
        if ($settings.PSObject.Properties.Name -contains 'discover') {
            if ($settings.discover -isnot [bool]) {
                throw 'Source-pattern discover must be a JSON boolean'
            }
            $configuration['discover'] = [bool]$settings.discover
        }
        if ($configuration.ContainsKey('launcher_audit_path')) {
            $auditPath = $configuration['launcher_audit_path']
            $auditDir = Split-Path -Parent $auditPath
            New-Item -ItemType Directory -Force -Path $auditDir | Out-Null
        }
    }

    # Identify predictable failures BEFORE running yt-dlp or the collector.
    # This launcher audit is separate from the Python collector's measured audit.
    if ($UvExecutable) {
        if (-not [System.IO.Path]::IsPathRooted($UvExecutable) -or
                -not (Test-Path -LiteralPath $UvExecutable -PathType Leaf)) {
            throw 'Invalid pinned uv executable'
        }
        $uvPath = (Resolve-Path -LiteralPath $UvExecutable).Path
    } else {
        $uv = Get-Command 'uv' -CommandType Application -ErrorAction Stop
        $uvPath = $uv.Source
    }
    # Validate the entire installed media toolchain before starting Python.
    # The old launcher found only uv from a pinned path. ffmpeg/ffprobe and
    # the JS runtime could still disappear in a noninteractive scheduled PATH.
    $toolDirectories = New-Object 'System.Collections.Generic.List[string]'
    foreach ($pinned in @($UvExecutable, $FFmpegExecutable, $FFprobeExecutable,
                           $JsRuntimeExecutable)) {
        if (-not $pinned) { continue }
        if (-not [System.IO.Path]::IsPathRooted($pinned) -or
                -not (Test-Path -LiteralPath $pinned -PathType Leaf)) {
            throw 'Invalid pinned media executable'
        }
        $toolDirectories.Add((Split-Path -Parent (Resolve-Path -LiteralPath $pinned).Path))
    }
    # Preserve uv's directory first for managed uvx/uv subprocess discovery.
    foreach ($directory in $toolDirectories) {
        $env:PATH = $directory + [System.IO.Path]::PathSeparator + $env:PATH
    }
    foreach ($binary in @('ffmpeg', 'ffprobe')) {
        $null = Get-Command $binary -CommandType Application -ErrorAction Stop
    }
    # A pinned Deno/Node is also discoverable by Python's yt-dlp runtime
    # even when Task Scheduler inherited no interactive-user PATH.
    if ($JsRuntimeExecutable) {
        $jsName = [System.IO.Path]::GetFileNameWithoutExtension($JsRuntimeExecutable)
        if ($jsName -notin @('deno', 'node')) {
            throw 'Invalid pinned JavaScript runtime name'
        }
    }
    $ranCollector = $true
    $status = 'FAILED_COLLECTOR_PROCESS'
    # Powershell 5.1 can treat native stderr as terminating errors with
    # ErrorActionPreference=Stop even if the child succeeds; use exit code.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $argsForCollector = @(
            'run', '--extra', 'media', 'holoclip',
            'examples-collect-source-patterns', '--cycles', [string]$Cycles,
            '--max-clips', [string]$MaxClips
        )
        if ($null -ne $configuration) {
            foreach ($name in @('cycles', 'max_clips', 'max_candidates',
                                'scan_limit', 'target_distinct_streams')) {
                if (-not $configuration.ContainsKey($name)) { continue }
                $flag = '--' + $name.Replace('_', '-')
                $index = [array]::IndexOf($argsForCollector, $flag)
                if ($index -ge 0) {
                    $argsForCollector[$index + 1] = [string]$configuration[$name]
                } else {
                    $argsForCollector += @($flag, [string]$configuration[$name])
                }
            }
            foreach ($name in @('store', 'state', 'output', 'policy_file',
                                'report_path')) {
                if (-not $configuration.ContainsKey($name)) { continue }
                $argsForCollector += @(
                    ('--' + $name.Replace('_', '-')),
                    [string]$configuration[$name]
                )
            }
            if ($configuration.ContainsKey('discover') -and
                    -not $configuration['discover']) {
                $argsForCollector += '--no-discover'
            }
        }
        # Array invocation preserves argument boundaries for paths with spaces.
        & $uvPath @argsForCollector *>> $logPath
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
    $backup = $auditPath + '.' + $PID + '.bak'
    try {
        $json = $audit | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText(
            $temp, $json, ([System.Text.UTF8Encoding]::new($false))
        )
        if ([System.IO.File]::Exists($auditPath)) {
            # Windows PowerShell 5.1 uses .NET Framework, where a null
            # backup path can throw ArgumentException ("not a legal form").
            # A same-volume named backup preserves atomic replace semantics.
            [System.IO.File]::Replace($temp, $auditPath, $backup)
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
        Remove-Item -LiteralPath $temp, $backup -ErrorAction SilentlyContinue
    }
}
# Task Scheduler must see real process/preflight errors, not an optimistic 0.
exit $code
