# Enumerate, do not mutate, Task Scheduler registrations with this exact name.
# Never treat permissions/COM failures as "not found".
param(
    [Parameter(Mandatory=$true)][string]$TaskName
)
$ErrorActionPreference = 'Stop'
function Emit([string]$status, [int]$exitCode) {
    Write-Output ('{"status":"' + $status + '","ready":false}')
    exit $exitCode
}
try {
    if ($TaskName -cnotmatch '^[A-Za-z0-9 _-]{1,90}$') {
        Emit 'LAN_TASK_PROBE_INVALID_NAME' 6
    }
    $foundTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
        $_.TaskName -eq $TaskName
    })
    if ($foundTasks.Count -gt 0) { Emit 'LAN_TASK_ALREADY_PRESENT' 0 }
    Emit 'LAN_TASK_ABSENT_OBSERVED' 0
} catch {
    Emit 'LAN_TASK_ENUMERATION_UNAVAILABLE' 6
}
