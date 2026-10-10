# Synthetic Windows PowerShell 5.1 queue task enrollment; no real tasks used.
$ErrorActionPreference = 'Stop'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('holoclip-queue-task-' + [guid]::NewGuid().ToString('N'))
function Assert-Task([bool]$ok,[string]$msg) { if (-not $ok) { throw $msg } }
try {
  $scripts = Join-Path $tmp 'scripts'
  $venv = Join-Path $tmp '.venv\Scripts'
  New-Item -ItemType Directory -Force -Path $scripts,$venv | Out-Null
  $installer = Join-Path $scripts 'install-render-source-queue-task.ps1'
  $runner = Join-Path $scripts 'run-render-source-queue.ps1'
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-render-source-queue-task.ps1') $installer
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-render-source-queue.ps1') $runner
  $queue = Join-Path $tmp 'queue.sqlite3'
  $uv = Join-Path $tmp 'uv.cmd'
  [IO.File]::WriteAllText($queue, 'offline mock queue')
  [IO.File]::WriteAllText($uv, '@echo off')
  [IO.File]::WriteAllText((Join-Path $venv 'python.exe'), 'mock python')
  foreach ($path in @($installer,$runner)) {
    $tokens=$null; $errors=$null
    $null=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
    Assert-Task ($errors.Count -eq 0) ('PowerShell syntax invalid: ' + $path)
  }
  $global:queueTaskExists=$false
  $global:queueRegistrationCount=0
  $global:queueLastAction=$null
  function Get-ScheduledTask {
    [CmdletBinding()]
    param()
    if ($global:queueTaskExists) { [pscustomobject]@{TaskName='Holoclip render source queue'} }
  }
  function Get-Command {
    [CmdletBinding()]
    param([string]$Name,[string]$CommandType)
    if ($Name -ne 'uv') { throw 'Unexpected command' }
    [pscustomobject]@{Source=$uv}
  }
  function New-ScheduledTaskAction {
    param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
    $global:queueLastAction=[pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}
    return $global:queueLastAction
  }
  function New-ScheduledTaskTrigger {
    param([switch]$Once,[datetime]$At,[timespan]$RepetitionInterval,[timespan]$RepetitionDuration)
    Assert-Task $Once 'Expected once-repeating trigger'
    Assert-Task ($RepetitionInterval.TotalMinutes -eq 60) 'Interval mismatch'
    [pscustomobject]@{Once=$Once;At=$At}
  }
  function New-ScheduledTaskSettingsSet {
    param([string]$MultipleInstances,[switch]$StartWhenAvailable,
          [timespan]$ExecutionTimeLimit,[switch]$AllowStartIfOnBatteries,
          [switch]$DontStopIfGoingOnBatteries)
    Assert-Task ($MultipleInstances -eq 'IgnoreNew') 'Queue task may overlap'
    Assert-Task ($ExecutionTimeLimit.TotalMinutes -eq 55) 'Task deadline wrong'
    [pscustomobject]@{MultipleInstances=$MultipleInstances}
  }
  function New-ScheduledTaskPrincipal {
    param([string]$UserId,[string]$LogonType,[string]$RunLevel)
    Assert-Task ($LogonType -eq 'Interactive') 'Should use current user only'
    Assert-Task ($RunLevel -eq 'Limited') 'Must be non-elevated'
    [pscustomobject]@{UserId=$UserId}
  }
  function Register-ScheduledTask {
    param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description)
    $global:queueRegistrationCount++
    $global:queueTaskExists=$true
    [pscustomobject]@{TaskName=$TaskName}
  }
  $result = & $installer -QueuePath $queue -TaskName 'Holoclip render source queue' -IntervalMinutes 60
  Assert-Task ($result -contains 'LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED') 'Installer did not report guarded registration'
  Assert-Task ($global:queueRegistrationCount -eq 1) 'Registered more than one task'
  Assert-Task ($global:queueLastAction.Arguments -like '*run-render-source-queue.ps1*') 'Wrong runner'
  Assert-Task ($global:queueLastAction.Arguments -notmatch 'http|youtube') 'Direct network task installed'
  $failed=$false
  try { $null=& $installer -QueuePath $queue -TaskName 'Holoclip render source queue' -IntervalMinutes 60 }
  catch { $failed=$true }
  Assert-Task $failed 'Installer overwrote existing task'
  Assert-Task ($global:queueRegistrationCount -eq 1) 'Collision registered additional task'
  Write-Output 'Render source queue scheduler PowerShell mock passed, no real tasks changed'
} finally {
  Remove-Item -LiteralPath $tmp -Force -Recurse -ErrorAction SilentlyContinue
}
