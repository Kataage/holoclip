# Windows PowerShell 5.1 mocked read-only LAN setup inspection contracts.
# No real firewall, SSH service, Task Scheduler, LAN or YouTube work.
$ErrorActionPreference = 'Stop'
$inspector = Join-Path $PSScriptRoot 'inspect-ytdlp-lan-windows.ps1'
function Assert-Setup([bool]$ok, [string]$why) { if (-not $ok) { throw $why } }
$tokens = $null
$errors = $null
$null = [Management.Automation.Language.Parser]::ParseFile(
    $inspector, [ref]$tokens, [ref]$errors)
Assert-Setup ($errors.Count -eq 0) 'PowerShell 5.1 inspector syntax invalid'
$script:observedTask = 'Running'
$script:listener = '192.168.1.20'
$script:remote = '192.168.1.21'
function Get-ScheduledTask {
    param([string]$TaskName, [string]$ErrorAction)
    [pscustomobject]@{ State = $script:observedTask }
}
function Get-NetTCPConnection {
    param([int]$LocalPort, [string]$State, [string]$ErrorAction)
    [pscustomobject]@{ LocalAddress = $script:listener }
}
function Get-NetFirewallRule {
    param([string]$PolicyStore, [string]$Enabled, [string]$Direction,
          [string]$Action, [string]$ErrorAction)
    [pscustomobject]@{ Name = 'mock-rule' }
}
function Get-NetFirewallPortFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction)
    [pscustomobject]@{ Protocol = 'TCP'; LocalPort = '8878' }
}
function Get-NetFirewallAddressFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction)
    [pscustomobject]@{ RemoteAddress = $script:remote }
}
function Get-Service {
    param([string]$Name, [string]$ErrorAction)
    [pscustomobject]@{ Status = 'Running' }
}
$good = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp 192.168.1.21 | ConvertFrom-Json)
Assert-Setup ($good.status -eq 'WINDOWS_OBSERVATIONS') 'Inspector did not report recognized status'
Assert-Setup ($good.coordinator_task -eq 'RUNNING_OBSERVED') 'Task not observed'
Assert-Setup ($good.coordinator_listener -eq 'LAN_LISTENER_OBSERVED') 'Listener not observed'
Assert-Setup ($good.firewall_rule -eq 'EXACT_PEER_ALLOW_RULE_OBSERVED') 'Exact IP scope not observed'
Assert-Setup ($good.sshd_service -eq 'RUNNING_OBSERVED') 'SSH service not observed'
Assert-Setup (-not $good.firewall_effective_policy_verified) 'Inspected firewall rule is not policy proof'
Assert-Setup (-not $good.two_host_verified) 'Mock must not claim two host qualification'
Assert-Setup (-not $good.ready) 'Local mock must not promote readiness'
$script:remote = 'Any'
$broad = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp 192.168.1.21 | ConvertFrom-Json)
Assert-Setup ($broad.firewall_rule -eq 'BROAD_INBOUND_ALLOW_RULE_OBSERVED') 'Broad rule must fail closed'
$script:remote = @('192.168.1.21', 'Any')
$multi = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp 192.168.1.21 | ConvertFrom-Json)
Assert-Setup ($multi.firewall_rule -eq 'BROAD_INBOUND_ALLOW_RULE_OBSERVED') 'Mixed address list must not bypass broad detection'
$script:remote = '192.168.1.21'
$script:listener = '0.0.0.0'
$wildcard = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp 192.168.1.21 | ConvertFrom-Json)
Assert-Setup ($wildcard.coordinator_listener -eq 'WILDCARD_LISTENER_OBSERVED') 'Wildcard listener must be flagged'
$noPeer = (. $inspector -Role pc-a -CoordinatorPort 8878 | ConvertFrom-Json)
Assert-Setup ($noPeer.firewall_rule -eq 'PEER_IP_REQUIRED') 'Peer required for firewall inspection'
$client = (. $inspector -Role pc-b | ConvertFrom-Json)
Assert-Setup ($client.coordinator_task -eq 'NOT_APPLICABLE') 'PC-B task is not required'
Assert-Setup ($client.firewall_rule -eq 'NOT_APPLICABLE') 'PC-B inbound firewall not required'
Assert-Setup ($client.ready -eq $false) 'PC-B must not promote readiness'
Write-Host 'Read-only Windows LAN setup inspector: mocked task, listener, firewall, sshd contracts passed'
