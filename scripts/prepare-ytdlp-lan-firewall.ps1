# Windows PowerShell 5.1. Safe PC-A firewall onboarding; no default mutations.
# Never disables a firewall, removes/replaces rules, starts services,
# edits credentials, trusts SSH hosts, or contacts YouTube.
param(
    [Parameter(Mandatory=$true)][string]$BindIp,
    [Parameter(Mandatory=$true)][string]$PeerIp,
    [ValidateRange(1024, 65535)][int]$Port = 8878,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$inspector = Join-Path $PSScriptRoot 'inspect-ytdlp-lan-windows.ps1'
$ruleName = 'HoloclipYtDlpLanTcp-' + $Port

function Write-FirewallResult([string]$status, [string]$check, [bool]$added) {
    [ordered]@{
        status = $status
        check = $check
        rule_name = $ruleName
        rule_added = $added
        changes_may_be_partial = ($Apply.IsPresent -and $status -eq 'LAN_FIREWALL_APPLY_UNCONFIRMED')
        existing_rules_replaced = $false
        existing_rules_deleted = $false
        firewall_disabled = $false
        task_modified = $false
        youtube_contacted = $false
        media_downloaded = $false
        firewall_effective_policy_verified = $false
        two_host_verified = $false
        ready = $false
    } | ConvertTo-Json -Compress
}

function Test-LiteralPrivateIp([string]$ip) {
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($ip, [ref]$parsed) -or
        $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $parsed.ToString() -cne $ip) {
        return $false
    }
    $bytes = $parsed.GetAddressBytes()
    return ($bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168))
}

function Get-NarrowScopeObservation {
    try {
        $raw = @(& $inspector -Role 'pc-a' -PeerIp $PeerIp -CoordinatorPort $Port)
        if ($raw.Count -ne 1) { return 'LAN_FIREWALL_INSPECTION_UNAVAILABLE' }
        $doc = $raw[0] | ConvertFrom-Json
        if ($doc.status -cne 'WINDOWS_OBSERVATIONS' -or $doc.role -cne 'pc-a' -or
            $doc.ready -cne $false -or $doc.two_host_verified -cne $false -or
            $doc.firewall_effective_policy_verified -cne $false) {
            return 'LAN_FIREWALL_INSPECTION_UNAVAILABLE'
        }
        if ($doc.firewall_rule -eq 'EXACT_PEER_ALLOW_RULE_OBSERVED') {
            return 'LAN_FIREWALL_EXACT_SCOPE_OBSERVED'
        }
        if ($doc.firewall_rule -eq 'NO_EXACT_PEER_ALLOW_RULE') {
            return 'LAN_FIREWALL_EXACT_SCOPE_MISSING'
        }
    } catch { }
    return 'LAN_FIREWALL_INSPECTION_UNAVAILABLE'
}

function Get-Preflight {
    if (-not (Test-LiteralPrivateIp $BindIp) -or
        -not (Test-LiteralPrivateIp $PeerIp) -or $BindIp -eq $PeerIp) {
        return 'LAN_FIREWALL_UNSAFE_ADDRESS'
    }
    if (-not (Test-Path -LiteralPath $inspector -PathType Leaf)) {
        return 'LAN_FIREWALL_INSPECTION_UNAVAILABLE'
    }
    try {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $BindIp -ErrorAction Stop)
        if ($addresses.Count -ne 1 -or $null -eq $addresses[0].InterfaceIndex) {
            return 'LAN_FIREWALL_BIND_NOT_UNIQUE'
        }
        $networks = @(Get-NetConnectionProfile -InterfaceIndex $addresses[0].InterfaceIndex -ErrorAction Stop)
        if ($networks.Count -ne 1 -or $networks[0].NetworkCategory -ne 'Private') {
            return 'LAN_FIREWALL_PRIVATE_NETWORK_REQUIRED'
        }
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -Name Private -ErrorAction Stop)
        if ($profiles.Count -ne 1 -or [string]$profiles[0].Enabled -ne 'True') {
            return 'LAN_FIREWALL_PRIVATE_PROFILE_UNVERIFIED'
        }
        # Reserve a fixed name and never clobber a rule, even if disabled.
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop)
        foreach ($rule in $rules) {
            if ($null -eq $rule -or $null -eq $rule.Name) {
                return 'LAN_FIREWALL_INVENTORY_UNVERIFIED'
            }
            if ([string]$rule.Name -ieq $ruleName) {
                return 'LAN_FIREWALL_RULE_NAME_CONFLICT'
            }
        }
    } catch {
        return 'LAN_FIREWALL_INVENTORY_UNVERIFIED'
    }
    $scope = Get-NarrowScopeObservation
    if ($scope -eq 'LAN_FIREWALL_EXACT_SCOPE_OBSERVED') {
        return 'LAN_FIREWALL_ALREADY_SCOPED'
    }
    if ($scope -ne 'LAN_FIREWALL_EXACT_SCOPE_MISSING') {
        return 'LAN_FIREWALL_SCOPE_BLOCKED'
    }
    return 'LAN_FIREWALL_CAN_ADD_EXACT_RULE'
}

$added = $false
try {
    $preflight = Get-Preflight
    if ($preflight -eq 'LAN_FIREWALL_ALREADY_SCOPED') {
        Write-FirewallResult 'LAN_FIREWALL_ALREADY_SCOPED' $preflight $false
    } elseif ($preflight -ne 'LAN_FIREWALL_CAN_ADD_EXACT_RULE') {
        Write-FirewallResult 'LAN_FIREWALL_PREVIEW_BLOCKED' $preflight $false
    } elseif (-not $Apply) {
        Write-FirewallResult 'LAN_FIREWALL_PREVIEW_ELIGIBLE' $preflight $false
    } else {
        # Re-inspect immediately before addition; never auto-fix broad rules.
        $again = Get-Preflight
        if ($again -ne 'LAN_FIREWALL_CAN_ADD_EXACT_RULE') {
            Write-FirewallResult 'LAN_FIREWALL_APPLY_BLOCKED' $again $false
        } else {
            $newRule = @{
                Name = $ruleName
                DisplayName = $ruleName
                PolicyStore = 'PersistentStore'
                Direction = 'Inbound'
                Action = 'Allow'
                Enabled = 'True'
                Profile = 'Private'
                Protocol = 'TCP'
                LocalPort = $Port
                LocalAddress = $BindIp
                RemoteAddress = $PeerIp
                EdgeTraversalPolicy = 'Block'
                ErrorAction = 'Stop'
            }
            New-NetFirewallRule @newRule | Out-Null
            $added = $true
            if ((Get-NarrowScopeObservation) -eq 'LAN_FIREWALL_EXACT_SCOPE_OBSERVED') {
                Write-FirewallResult 'LAN_FIREWALL_RULE_ADDED' 'LAN_FIREWALL_EXACT_SCOPE_OBSERVED' $true
            } else {
                # No rollback of a rule that might now be used by a service.
                Write-FirewallResult 'LAN_FIREWALL_ADDED_UNVERIFIED' 'LAN_FIREWALL_INSPECTION_UNAVAILABLE' $true
            }
        }
    }
} catch {
    # Suppress raw Windows errors, addresses, usernames and machine-specific paths.
    Write-FirewallResult 'LAN_FIREWALL_APPLY_UNCONFIRMED' 'LAN_FIREWALL_CMDLET_FAILURE' $added
}
