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
# Retain a bounded local launcher timeline even when uv/Python never started.
# It is strictly an operational receipt, NOT verified footage or editor gold.
try {
    $historyPath = Join-Path (Split-Path -Parent $auditPath) 'source-pattern-launcher-history.json'
    $historyLockPath = $historyPath + '.lock'
    $historyDir = Split-Path -Parent $historyPath
    New-Item -ItemType Directory -Force -Path $historyDir | Out-Null
    foreach ($protected in @($historyPath, $historyLockPath)) {
        if (Test-Path -LiteralPath $protected) {
            $item = Get-Item -LiteralPath $protected -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Unsafe launcher history symlink or reparse point'
            }
        }
    }
    # A separate OS-held exclusive file prevents two manual launchers from
    # reading and replacing the same history concurrently.
    $lock = [System.IO.FileStream]::new(
        $historyLockPath, [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
    )
    try {
        $history = @()
        if (Test-Path -LiteralPath $historyPath -PathType Leaf) {
            $current = Get-Item -LiteralPath $historyPath
            if ($current.Length -gt 262144) { throw 'Launcher history exceeds bound' }
            $existing = Get-Content -LiteralPath $historyPath -Raw -Encoding UTF8 |
                ConvertFrom-Json -ErrorAction Stop
            if ($null -eq $existing -or
                    $existing.version -cne 'source-pattern-launcher-history-v1' -or
                    $existing.events -isnot [array] -or
                    $existing.events.Count -gt 90) {
                throw 'Malformed existing launcher history'
            }
            foreach ($prior in $existing.events) {
                if ($null -eq $prior -or
                        $prior.status -cnotin @('FAILED_PRELAUNCH',
                            'FAILED_COLLECTOR_PROCESS', 'COMPLETED_COLLECTOR_PROCESS') -or
                        $prior.ready -ne $false -or
                        $prior.production_ready -ne $false) {
                    throw 'Unsafe existing launcher history event'
                }
            }
            $history = @($existing.events)
        }
        $entry = @{
            started_at_utc = $startedAt
            finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
            status = $status
            exit_code = [int]$audit.exit_code
            ran_collector = [bool]$ranCollector
            error_class = $errorClass
            new_unique_video_samples = $null
            new_distinct_candidate_streams = $null
            collector_report_correlated = $false
            # Legacy events have no stage measurements. New metrics stay null
            # unless this same launch has independently bounded collector
            # stage diagnostics; no retrospective synthetic reconstruction.
            stage_metrics_correlated = $false
            last_run_visual_pair_attempts = $null
            last_run_source_window_records = $null
            last_run_sampled_visual_correspondences = $null
            ready = $false
            production_ready = $false
        }
        # Only attach per-run counts if the finished collector's audit is
        # actually within THIS launch's bounds. Never reuse yesterday's success.
        if ($ranCollector -and $status -eq 'COMPLETED_COLLECTOR_PROCESS') {
            $collectorPath = Join-Path $stateRoot 'source-pattern-last-run.json'
            if ($null -ne $configuration -and
                    $configuration.ContainsKey('report_path')) {
                $collectorPath = $configuration['report_path']
            }
            if (Test-Path -LiteralPath $collectorPath -PathType Leaf) {
                try {
                    $colFile = Get-Item -LiteralPath $collectorPath
                    if (($colFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                            $colFile.Length -gt 4194304) {
                        throw 'Unsafe collector report for launcher history'
                    }
                    $col = Get-Content -LiteralPath $collectorPath -Raw -Encoding UTF8 |
                        ConvertFrom-Json -ErrorAction Stop
                    $began = [DateTimeOffset]::Parse($col.started_at_utc)
                    $ended = [DateTimeOffset]::Parse($col.finished_at_utc)
                    $start = [DateTimeOffset]::Parse($startedAt)
                    $finish = [DateTimeOffset]::Parse($entry.finished_at_utc)
                    $newSamples = $col.result.new_unique_video_samples_this_run
                    $newStreams = $col.result.new_distinct_candidate_streams_this_run
                    $collectorStatusValid = (
                        ($col.status -ceq 'MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS' -and
                         $newSamples -gt 0) -or
                        ($col.status -clike 'UNKNOWN_*' -and $newSamples -eq 0)
                    )
                    $collectorFlagsValid = $true
                    foreach ($flag in @('source_identity_verified',
                                        'reviewed_gold_available',
                                        'qualification_allowed',
                                        'ready', 'production_ready')) {
                        if ($col.$flag -ne $false -or
                                $col.result.$flag -ne $false) {
                            $collectorFlagsValid = $false
                            break
                        }
                    }
                    if ($col.version -ceq 'source-pattern-unattended-run-v1' -and
                            $col.result.status -ceq $col.status -and
                            $collectorStatusValid -and
                            $collectorFlagsValid -and
                            $began -ge $start -and $ended -ge $began -and
                            $ended -le $finish) {
                        if (($newSamples -is [int] -or $newSamples -is [long]) -and
                                ($newStreams -is [int] -or $newStreams -is [long]) -and
                                $newSamples -ge 0 -and $newSamples -le 100000 -and
                                $newStreams -ge 0 -and $newStreams -le 100000) {
                            $entry.new_unique_video_samples = [int]$newSamples
                            $entry.new_distinct_candidate_streams = [int]$newStreams
                            $entry.collector_report_correlated = $true
                            # Only record stage counts if EVERY cycle has the
                            # current exact non-gold stage schema and bounded
                            # numeric observations. Older report versions
                            # preserve unknown/null per-run pairing counts.
                            $rows = $col.result.cycle_diagnostics
                            if ($rows -is [array] -and $rows.Count -gt 0) {
                                $validStages = $true
                                $pairs = 0
                                $sections = 0
                                $corroborations = 0
                                foreach ($row in $rows) {
                                    $stage = $row.stage_diagnostics
                                    if ($null -eq $stage -or
                                            $stage.measured_original_match_gold -ne $false -or
                                            $stage.record_counts_are_verified_public_media -ne $false -or
                                            $stage.candidate_visual_status_counts -isnot [pscustomobject]) {
                                        $validStages = $false
                                        break
                                    }
                                    $p = $stage.candidate_observation_records
                                    $w = $stage.source_window_section_records
                                    if ((($p -isnot [int]) -and ($p -isnot [long])) -or
                                            (($w -isnot [int]) -and ($w -isnot [long])) -or
                                            $p -lt 0 -or $p -gt 100000 -or
                                            $w -lt 0 -or $w -gt 100000 -or
                                            $w -gt ($p * 3)) {
                                        $validStages = $false
                                        break
                                    }
                                    $matched = 0
                                    $property = $stage.candidate_visual_status_counts.PSObject.Properties[
                                        'VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD'
                                    ]
                                    if ($null -ne $property) {
                                        $matched = $property.Value
                                    }
                                    if ((($matched -isnot [int]) -and ($matched -isnot [long])) -or
                                            $matched -lt 0 -or $matched -gt $p) {
                                        $validStages = $false
                                        break
                                    }
                                    $pairs += [int]$p
                                    $sections += [int]$w
                                    $corroborations += [int]$matched
                                    if ($pairs -gt 100000 -or $sections -gt 100000 -or
                                            $corroborations -gt 100000) {
                                        $validStages = $false
                                        break
                                    }
                                }
                                if ($validStages) {
                                    $entry.last_run_visual_pair_attempts = [int]$pairs
                                    $entry.last_run_source_window_records = [int]$sections
                                    $entry.last_run_sampled_visual_correspondences = [int]$corroborations
                                    $entry.stage_metrics_correlated = $true
                                }
                            }
                        }
                    }
                }
                catch {
                    # A stale or corrupted optional collector report can NEVER
                    # promote the launcher event. It remains an unknown count.
                    $entry.collector_report_correlated = $false
                }
            }
        }
        $history = @($history + @([pscustomobject]$entry) | Select-Object -Last 90)
        $document = @{
            version = 'source-pattern-launcher-history-v1'
            events = @($history)
            ready = $false
            production_ready = $false
        }
        $historyTemp = $historyPath + '.' + $PID + '.tmp'
        $historyBackup = $historyPath + '.' + $PID + '.bak'
        try {
            [IO.File]::WriteAllText(
                $historyTemp, ($document | ConvertTo-Json -Depth 8),
                ([System.Text.UTF8Encoding]::new($false))
            )
            if ([IO.File]::Exists($historyPath)) {
                [IO.File]::Replace($historyTemp, $historyPath, $historyBackup)
            } else {
                [IO.File]::Move($historyTemp, $historyPath)
            }
        }
        finally {
            Remove-Item -LiteralPath $historyTemp, $historyBackup -ErrorAction SilentlyContinue
        }
    }
    finally {
        $lock.Dispose()
    }
}
catch {
    # Retain the primary launcher audit. A history write failure makes the
    # Task Scheduler status nonzero, not a silently-successful diagnosis.
    $code = 1
    ('Windows launcher history failure: ' + $_.Exception.GetType().Name) |
        Out-File -LiteralPath $logPath -Append -Encoding UTF8
}
# Task Scheduler must see real process/preflight errors, not an optimistic 0.
exit $code
