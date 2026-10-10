# Windows PowerShell 5.1. Synthetic active-store rule matrix. No actual firewall,
# registry, task modification, coordinator network, or YouTube operations.
$ErrorActionPreference = 'Stop'
$inspector = Join-Path $PSScriptRoot 'inspect-ytdlp-lan-windows.ps1'
function Assert-Scope([bool]$ok, [string]$context) {
    if (-not $ok) { throw $context }
}
$script:mockRules = @()
$script:denyEnumeration = $false
function Get-ScheduledTask {
    param([string]$TaskName, [string]$ErrorAction)
    [pscustomobject]@{ State = 'Running' }
}
function Get-NetTCPConnection {
    param([int]$LocalPort, [string]$State, [string]$ErrorAction)
    [pscustomobject]@{ LocalAddress = '192.168.1.10' }
}
function Get-NetFirewallRule {
    param([string]$PolicyStore, [string]$Enabled, [string]$Direction,
          [string]$Action, [string]$ErrorAction)
    if ($script:denyEnumeration) { throw 'Synthetic access failure' }
    foreach ($item in $script:mockRules) { $item }
}
function Get-NetFirewallPortFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction)
    [pscustomobject]@{
        Protocol = $AssociatedNetFirewallRule.Protocol
        LocalPort = $AssociatedNetFirewallRule.Port
    }
}
function Get-NetFirewallAddressFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction)
    [pscustomobject]@{ RemoteAddress = $AssociatedNetFirewallRule.Remote }
}
function Get-Service {
    param([string]$Name, [string]$ErrorAction)
    [pscustomobject]@{ Status = 'Running' }
}
function New-Rule([string]$protocol, $port, $remote) {
    [pscustomobject]@{ Name = 'mock'; Protocol = $protocol; Port = $port; Remote = $remote }
}
$peer = '192.168.1.21'
$exact = New-Rule 'TCP' '8878' $peer
$cases = @(
    @{ Name = 'single exact rule'; Rules = @($exact); Want = 'EXACT_PEER_ALLOW_RULE_OBSERVED' },
    @{ Name = 'TCP numeric protocol'; Rules = @(New-Rule '6' '8878' $peer); Want = 'EXACT_PEER_ALLOW_RULE_OBSERVED' },
    @{ Name = 'TCP unrelated different port'; Rules = @($exact, (New-Rule 'TCP' '443' 'Any')); Want = 'EXACT_PEER_ALLOW_RULE_OBSERVED' },
    @{ Name = 'UDP port unrelated'; Rules = @($exact, (New-Rule 'UDP' '8878' 'Any')); Want = 'EXACT_PEER_ALLOW_RULE_OBSERVED' },
    @{ Name = 'two exact peers'; Rules = @($exact, $exact); Want = 'EXACT_PEER_ALLOW_RULE_OBSERVED' },
    @{ Name = 'mixed IPv4 exact and other'; Rules = @(New-Rule 'TCP' '8878' @($peer,'192.168.1.22')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'second wider rule'; Rules = @($exact, (New-Rule 'TCP' '8878' 'Any')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'CIDR subnet'; Rules = @($exact, (New-Rule 'TCP' '8878' '192.168.1.0/24')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'CIDR single-host unverified'; Rules = @($exact, (New-Rule 'TCP' '8878' '192.168.1.21/32')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'IPv4 inclusive address range'; Rules = @($exact, (New-Rule 'TCP' '8878' '192.168.1.10-192.168.1.30')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'IPv4 netmask notation'; Rules = @($exact, (New-Rule 'TCP' '8878' '192.168.1.0/255.255.255.0')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'LocalSubnet'; Rules = @($exact, (New-Rule 'TCP' '8878' 'LocalSubnet')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'Internet keyword'; Rules = @($exact, (New-Rule 'TCP' '8878' 'Internet')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'different exact host'; Rules = @($exact, (New-Rule 'TCP' '8878' '192.168.1.22')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'IPv6 host'; Rules = @($exact, (New-Rule 'TCP' '8878' 'fe80::1')); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'comma separated address values'; Rules = @(New-Rule 'TCP' '8878' '192.168.1.21, 192.168.1.22'); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'any port'; Rules = @(New-Rule 'TCP' 'Any' $peer); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'numeric range containing port'; Rules = @(New-Rule 'TCP' '8877-8879' $peer); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'multiple ports one rule'; Rules = @(New-Rule 'TCP' '8878,443' $peer); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'Any protocol TCP included'; Rules = @(New-Rule 'Any' '8878' $peer); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = '256 protocol Any'; Rules = @(New-Rule '256' '8878' $peer); Want = 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' },
    @{ Name = 'invalid port range'; Rules = @($exact, (New-Rule 'TCP' '8879-8877' $peer)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'RPC dynamic ports'; Rules = @($exact, (New-Rule 'TCP' 'RPC Dynamic Ports' $peer)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'unknown port identifier'; Rules = @($exact, (New-Rule 'TCP' 'IPHTTPS' $peer)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'unknown protocol'; Rules = @($exact, (New-Rule 'FutureProtocol' '8878' $peer)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'null remote scope'; Rules = @($exact, (New-Rule 'TCP' '8878' $null)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'null local port'; Rules = @($exact, (New-Rule 'TCP' $null $peer)); Want = 'INCOMPLETE_RULE_INSPECTION' },
    @{ Name = 'unrelated only'; Rules = @(New-Rule 'TCP' '443' 'Any'); Want = 'NO_EXACT_PEER_ALLOW_RULE' },
    @{ Name = 'UDP only'; Rules = @(New-Rule 'UDP' '8878' 'Any'); Want = 'NO_EXACT_PEER_ALLOW_RULE' },
    @{ Name = 'no active allow rules'; Rules = @(); Want = 'NO_EXACT_PEER_ALLOW_RULE' }
)
foreach ($case in $cases) {
    $script:mockRules = @($case.Rules)
    $result = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp $peer | ConvertFrom-Json)
    Assert-Scope ($result.firewall_rule -eq $case.Want) (
        'Firewall scope mismatch: ' + $case.Name + ', got=' + $result.firewall_rule +
        ', expected=' + $case.Want
    )
    Assert-Scope ($result.ready -eq $false) ('False readiness from ' + $case.Name)
    Assert-Scope ($result.firewall_effective_policy_verified -eq $false) (
        'Claimed effective firewall policy: ' + $case.Name
    )
}
$script:denyEnumeration = $true
$failure = (. $inspector -Role pc-a -CoordinatorPort 8878 -PeerIp $peer | ConvertFrom-Json)
Assert-Scope ($failure.firewall_rule -eq 'FIREWALL_INSPECTION_UNAVAILABLE') 'Enumeration failure promoted to ready'
$script:denyEnumeration = $false
Write-Host ('Windows Firewall active-rule scope: {0} synthetic scenarios passed; zero changes' -f ($cases.Count + 1))
