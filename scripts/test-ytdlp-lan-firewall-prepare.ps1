# Windows PowerShell 5.1 synthetic tests; NEVER edit the real Windows Firewall.
$ErrorActionPreference = 'Stop'
$prepare = Join-Path $PSScriptRoot 'prepare-ytdlp-lan-firewall.ps1'
$script:mockRules = @()
$script:created = @()
$script:category = 'Private'
$script:profileEnabled = $true
$script:failCreation = $false
$script:scopeChecks = 0
$script:raceBroad = $false

function Assert-Prepare([bool]$ok, [string]$description) {
    if (-not $ok) { throw $description }
}
function New-MockRule([string]$name, [string]$remote, [string]$port = '8878') {
    [pscustomobject]@{ Name = $name; Protocol = 'TCP'; Port = $port; Remote = $remote }
}
function Reset-Prepare {
    $script:mockRules = @()
    $script:created = @()
    $script:category = 'Private'
    $script:profileEnabled = $true
    $script:failCreation = $false
    $script:scopeChecks = 0
    $script:raceBroad = $false
}
function Get-NetIPAddress {
    param([string]$AddressFamily, [string]$IPAddress, [string]$ErrorAction)
    if ($IPAddress -cne '192.168.1.10') { throw 'not local' }
    [pscustomobject]@{ InterfaceIndex = 10 }
}
function Get-NetConnectionProfile {
    param([int]$InterfaceIndex, [string]$ErrorAction)
    [pscustomobject]@{ NetworkCategory = $script:category }
}
function Get-NetFirewallProfile {
    param([string]$PolicyStore, [string]$Name, [string]$ErrorAction)
    [pscustomobject]@{ Enabled = $script:profileEnabled }
}
function Get-NetFirewallRule {
    param([string]$PolicyStore, [string]$Enabled, [string]$Direction,
          [string]$Action, [string]$ErrorAction)
    if ($Enabled -eq 'True' -and $Direction -eq 'Inbound' -and $Action -eq 'Allow') {
        $script:scopeChecks++
        if ($script:raceBroad -and $script:scopeChecks -eq 2) {
            $script:mockRules += (New-MockRule 'other-broad' 'Any')
        }
    }
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
function Get-ScheduledTask {
    param([string]$TaskName, [string]$ErrorAction)
    [pscustomobject]@{ State = 'Running' }
}
function Get-NetTCPConnection {
    param([int]$LocalPort, [string]$State, [string]$ErrorAction)
    [pscustomobject]@{ LocalAddress = '192.168.1.10' }
}
function Get-Service {
    param([string]$Name, [string]$ErrorAction)
    [pscustomobject]@{ Status = 'Running' }
}
function New-NetFirewallRule {
    param([string]$Name, [string]$DisplayName, [string]$PolicyStore,
          [string]$Direction, [string]$Action, [string]$Enabled,
          [string]$Profile, [string]$Protocol, [int]$LocalPort,
          [string]$LocalAddress, [string]$RemoteAddress,
          [string]$EdgeTraversalPolicy, [string]$ErrorAction)
    if ($script:failCreation) { throw 'Synthetic secret path or identity error' }
    if (@($script:mockRules | Where-Object { $_.Name -eq $Name }).Count -gt 0) {
        throw 'Duplicate name rejected'
    }
    $script:created += [pscustomobject]@{
        Name = $Name; DisplayName = $DisplayName; PolicyStore = $PolicyStore
        Direction = $Direction; Action = $Action; Enabled = $Enabled
        Profile = $Profile; Protocol = $Protocol; LocalPort = $LocalPort
        LocalAddress = $LocalAddress; RemoteAddress = $RemoteAddress
        EdgeTraversalPolicy = $EdgeTraversalPolicy
    }
    $script:mockRules += (New-MockRule $Name $RemoteAddress ([string]$LocalPort))
}
function Invoke-Prepare([bool]$apply = $false,
                        [string]$bind = '192.168.1.10',
                        [string]$peer = '192.168.1.21') {
    $raw = @(& $prepare -BindIp $bind -PeerIp $peer -Port 8878 -Apply:$apply)
    Assert-Prepare ($raw.Count -eq 1) 'Expected one sanitized JSON record'
    $doc = $raw[0] | ConvertFrom-Json
    Assert-Prepare ($doc.ready -eq $false -and $doc.two_host_verified -eq $false) 'Readiness was forged'
    Assert-Prepare ($doc.firewall_effective_policy_verified -eq $false) 'Effective OS policy was forged'
    Assert-Prepare (-not $doc.youtube_contacted -and -not $doc.media_downloaded) 'Claimed media access'
    Assert-Prepare (-not $doc.existing_rules_replaced -and -not $doc.existing_rules_deleted) 'Existing rule mutation'
    return $doc
}

Reset-Prepare
$preview = Invoke-Prepare
Assert-Prepare ($preview.status -eq 'LAN_FIREWALL_PREVIEW_ELIGIBLE') ('Default preview not eligible: ' + ($preview | ConvertTo-Json -Compress))
Assert-Prepare ($script:created.Count -eq 0) 'Preview wrote a firewall rule'
$applied = Invoke-Prepare $true
Assert-Prepare ($applied.status -eq 'LAN_FIREWALL_RULE_ADDED') 'Apply did not re-observe exact rule'
Assert-Prepare ($applied.rule_added -and $script:created.Count -eq 1) 'Expected one guarded creation'
$rule = $script:created[0]
Assert-Prepare ($rule.Name -eq 'HoloclipYtDlpLanTcp-8878') 'Unstable rule identity'
Assert-Prepare ($rule.PolicyStore -eq 'PersistentStore' -and $rule.Direction -eq 'Inbound') 'Wrong rule store/direction'
Assert-Prepare ($rule.Profile -eq 'Private' -and $rule.Action -eq 'Allow') 'Firewall scope/profile mismatch'
Assert-Prepare ($rule.Protocol -eq 'TCP' -and $rule.LocalPort -eq 8878) 'Wrong TCP port'
Assert-Prepare ($rule.LocalAddress -eq '192.168.1.10') 'Destination bind not pinned'
Assert-Prepare ($rule.RemoteAddress -eq '192.168.1.21') 'Remote PC-B not pinned'
Assert-Prepare ($rule.EdgeTraversalPolicy -eq 'Block') 'Edge traversal enabled'
$repeated = Invoke-Prepare $true
Assert-Prepare ($repeated.status -eq 'LAN_FIREWALL_PREVIEW_BLOCKED') 'Name collision was accepted'
Assert-Prepare ($script:created.Count -eq 1) 'Repeat apply wrote again'

Reset-Prepare
$script:mockRules = @(New-MockRule 'existing-exact' '192.168.1.21')
$existing = Invoke-Prepare $true
Assert-Prepare ($existing.status -eq 'LAN_FIREWALL_ALREADY_SCOPED') 'Exact existing rule not no-op'
Assert-Prepare ($script:created.Count -eq 0) 'Existing rule was replaced'

Reset-Prepare
$script:mockRules = @(New-MockRule 'existing-broad' 'Any')
$broad = Invoke-Prepare $true
Assert-Prepare ($broad.status -eq 'LAN_FIREWALL_PREVIEW_BLOCKED') 'Broad rule allowed apply'
Assert-Prepare ($script:created.Count -eq 0) 'Broad rule permitted new write'

Reset-Prepare
$script:mockRules = @(New-MockRule 'HoloclipYtDlpLanTcp-8878' 'Any')
$collision = Invoke-Prepare $true
Assert-Prepare ($collision.check -eq 'LAN_FIREWALL_RULE_NAME_CONFLICT') 'Reserved name collision not blocked'

Reset-Prepare
$script:category = 'Public'
$public = Invoke-Prepare $true
Assert-Prepare ($public.check -eq 'LAN_FIREWALL_PRIVATE_NETWORK_REQUIRED') 'Public network accepted'
Assert-Prepare ($script:created.Count -eq 0) 'Public network wrote a rule'

Reset-Prepare
$script:profileEnabled = $false
$disabled = Invoke-Prepare $true
Assert-Prepare ($disabled.check -eq 'LAN_FIREWALL_PRIVATE_PROFILE_UNVERIFIED') 'Disabled profile accepted'

Reset-Prepare
foreach ($invalid in @('8.8.8.8', '127.0.0.1', '192.168.001.021', '192.168.1.10', '192.168.1.21;bad')) {
    $bad = Invoke-Prepare $true '192.168.1.10' $invalid
    Assert-Prepare ($bad.check -eq 'LAN_FIREWALL_UNSAFE_ADDRESS') ('Unsafe IP accepted: ' + $invalid)
}
Assert-Prepare ($script:created.Count -eq 0) 'Unsafe addresses wrote a rule'

Reset-Prepare
$script:raceBroad = $true
$raced = Invoke-Prepare $true
Assert-Prepare ($raced.status -eq 'LAN_FIREWALL_APPLY_BLOCKED') 'Race was not checked'
Assert-Prepare ($script:created.Count -eq 0) 'Race created rule'

Reset-Prepare
$script:failCreation = $true
$failure = Invoke-Prepare $true
Assert-Prepare ($failure.status -eq 'LAN_FIREWALL_APPLY_UNCONFIRMED') 'Cmdlet failure claimed success'
Assert-Prepare ($failure.changes_may_be_partial) 'Uncertain write not reported'
Assert-Prepare ($script:created.Count -eq 0) 'Failed write changed mock rule'
Assert-Prepare (-not (($failure | ConvertTo-Json -Compress) -match 'Synthetic secret')) 'Raw error leaked'

Write-Host 'Windows LAN firewall prepare: preview/apply/mock failure tests passed; zero real rules modified'
