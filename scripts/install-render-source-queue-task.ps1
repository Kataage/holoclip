# Register one guarded current-user video acquisition task. Explicit apply only.
# No task start, no saved password, no overwriting existing scheduler task.
param(
  [Parameter(Mandatory=$true)][string]$QueuePath,
  [Parameter(Mandatory=$true)][string]$TaskName,
  [ValidateRange(30,1440)][int]$IntervalMinutes=60
)
$ErrorActionPreference = 'Stop'
if ($TaskName -cnotmatch '^[A-Za-z0-9 _-]{1,90}$') { throw 'Invalid task name' }
if (-not [IO.Path]::IsPathRooted($QueuePath)) { throw 'Queue path not rooted' }
if (-not (Test-Path -LiteralPath $QueuePath -PathType Leaf)) { throw 'Queue not enrolled' }
$walk=[IO.Path]::GetFullPath($QueuePath)
while($walk) {
  if (Test-Path -LiteralPath $walk) {
    $item=Get-Item -LiteralPath $walk -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'Linked path not allowed'
    }
  }
  $parent=[IO.Path]::GetDirectoryName($walk)
  if (-not $parent -or $parent -eq $walk) { break }
  $walk=$parent
}
$repo=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner=Join-Path $PSScriptRoot 'run-render-source-queue.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'Runner missing' }
if (-not (Test-Path -LiteralPath (Join-Path $repo '.venv\Scripts\python.exe') -PathType Leaf)) {
  throw 'Holoclip uv environment missing'
}
$uv=(Get-Command 'uv' -CommandType Application -ErrorAction Stop).Source
if (-not [IO.Path]::IsPathRooted($uv) -or
    -not (Test-Path -LiteralPath $uv -PathType Leaf)) { throw 'Invalid uv location' }
foreach ($v in @($QueuePath,$uv,$runner)) {
  if ($v.Contains('"') -or $v.IndexOfAny([char[]]@([char]0,[char]10,[char]13)) -ge 0) {
    throw 'Unsafe scheduler arguments'
  }
}
$found=@(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $TaskName })
if ($found.Count -ne 0) { throw 'Existing task may not be overwritten' }
$exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments=('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
  '-UvExecutable "{1}" -QueuePath "{2}"') -f @($runner,$uv,$QueuePath)
$action=New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $repo
$trigger=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval ([timespan]::FromMinutes($IntervalMinutes)) -RepetitionDuration ([timespan]::FromDays(3650))
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([timespan]::FromMinutes(55)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$who=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Holoclip controlled at-most-one-source queue worker; never starts on install' | Out-Null
Write-Output 'LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED'
