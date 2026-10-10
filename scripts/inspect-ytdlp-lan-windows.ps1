# Windows PowerShell 5.1. Read-only setup observations with fixed codes.
# Never changes firewall, task, service, ACL or secret material.
param(
    [ValidateSet('pc-a', 'pc-b')][string]$Role = 'pc-b',
    [ValidateRange(1024, 65535)][int]$CoordinatorPort = 8878,
    [string]$TaskName = 'Holoclip yt-dlp LAN Coordinator',
    [string]$PeerIp = ''
)
$ErrorActionPreference = 'Stop'
function Get-ObservedTask {
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($task.State -eq 'Running') { return 'RUNNING_OBSERVED' }
        return 'NOT_RUNNING'
    } catch {
        return 'NOT_FOUND_OR_INACCESSIBLE'
    }
}
function Get-ObservedListener {
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $CoordinatorPort -State Listen -ErrorAction Stop)
        $private = $false
        foreach ($item in $listeners) {
            if ($item.LocalAddress -in @('0.0.0.0', '::')) {
                return 'WILDCARD_LISTENER_OBSERVED'
            }
            if ($item.LocalAddress -and $item.LocalAddress -notin @('127.0.0.1', '::1')) {
                $private = $true
            }
        }
        if ($private) { return 'LAN_LISTENER_OBSERVED' }
        return 'NO_LAN_LISTENER'
    } catch {
        return 'NOT_OBSERVED'
    }
}
function Get-ObservedFirewall {
    if ([string]::IsNullOrWhiteSpace($PeerIp)) { return 'PEER_IP_REQUIRED' }
    # The PowerShell inspector can also be called directly, without Python's
    # private-IPv4 validation. Reject noncanonical/ambiguous peer addresses.
    $parsedPeer = $null
    if (-not [Net.IPAddress]::TryParse($PeerIp, [ref]$parsedPeer) -or
        $parsedPeer.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $parsedPeer.ToString() -cne $PeerIp) {
        return 'INCOMPLETE_RULE_INSPECTION'
    }
    try {
        # Conservatively inspect *every* active allow rule. Even a second
        # rule belonging to another program can make exact-only proof unsafe.
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -Action Allow -ErrorAction Stop)
        $broad = $false
        $restricted = $false
        foreach ($rule in $rules) {
            if ($null -eq $rule) { return 'INCOMPLETE_RULE_INSPECTION' }
            try {
                $ports = @(Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
                $addresses = @(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
            } catch {
                return 'INCOMPLETE_RULE_INSPECTION'
            }
            if ($ports.Count -eq 0 -or $addresses.Count -eq 0) {
                return 'INCOMPLETE_RULE_INSPECTION'
            }
            foreach ($port in $ports) {
                if ($null -eq $port -or $null -eq $port.LocalPort) {
                    return 'INCOMPLETE_RULE_INSPECTION'
                }
                $protocol = ([string]$port.Protocol).Trim()
                # These protocols cannot match a TCP listener.
                if ($protocol -in @('UDP', '17', 'ICMPv4', '1', 'ICMPv6', '58')) {
                    continue
                }
                if ($protocol -notin @('TCP', '6', 'Any', '256')) {
                    # A new/unrecognized Windows protocol code must never
                    # allow another exact-IP rule to produce a positive result.
                    return 'INCOMPLETE_RULE_INSPECTION'
                }
                $includesPort = $false
                $broadPort = $false
                $entries = @($port.LocalPort)
                if ($entries.Count -eq 0) { return 'INCOMPLETE_RULE_INSPECTION' }
                foreach ($entry in $entries) {
                    if ($null -eq $entry) { return 'INCOMPLETE_RULE_INSPECTION' }
                    foreach ($value in (([string]$entry) -split ',')) {
                        $part = $value.Trim()
                        if ($part -in @('Any', '*')) {
                            $includesPort = $true
                            $broadPort = $true
                        } elseif ($part -match '^[0-9]{1,5}$') {
                            $number = [int]$part
                            if ($number -gt 65535 -or $number -lt 1) {
                                return 'INCOMPLETE_RULE_INSPECTION'
                            }
                            if ($number -eq $CoordinatorPort) {
                                $includesPort = $true
                            } else {
                                # A rule that permits the requested port AND
                                # an additional TCP port is not exact port
                                # scoping. Merely having other unrelated rules
                                # for other ports is fine.
                                $broadPort = $true
                            }
                        } elseif ($part -match '^([0-9]{1,5})-([0-9]{1,5})$') {
                            $startPort = [int]$Matches[1]
                            $endPort = [int]$Matches[2]
                            if ($startPort -lt 1 -or $endPort -gt 65535 -or
                                $startPort -gt $endPort) {
                                return 'INCOMPLETE_RULE_INSPECTION'
                            }
                            if ($CoordinatorPort -ge $startPort -and
                                $CoordinatorPort -le $endPort) {
                                $includesPort = $true
                                if ($startPort -ne $endPort) { $broadPort = $true }
                            }
                        } else {
                            # Dynamic port keywords, RPC, environment tokens,
                            # and unknown ranges cannot prove non-overlap.
                            return 'INCOMPLETE_RULE_INSPECTION'
                        }
                    }
                }
                if (-not $includesPort) { continue }
                if ($protocol -in @('Any', '256') -or $broadPort) {
                    $broad = $true
                }
                foreach ($address in $addresses) {
                    if ($null -eq $address -or $null -eq $address.RemoteAddress) {
                        return 'INCOMPLETE_RULE_INSPECTION'
                    }
                    $peers = @()
                    foreach ($entry in @($address.RemoteAddress)) {
                        if ($null -eq $entry) { return 'INCOMPLETE_RULE_INSPECTION' }
                        $peers += @(([string]$entry) -split ',')
                    }
                    if ($peers.Count -eq 0) { return 'INCOMPLETE_RULE_INSPECTION' }
                    foreach ($entry in $peers) {
                        $candidate = $entry.Trim()
                        if (-not $candidate) { return 'INCOMPLETE_RULE_INSPECTION' }
                        if ($candidate -cne $PeerIp) {
                            # Any other IP, address set, CIDR (including /32),
                            # range, dynamic keyword or IPv6 entry prevents
                            # exact-literal-PC-B-only certification.
                            $broad = $true
                        } elseif ($protocol -in @('TCP', '6') -and -not $broadPort) {
                            $restricted = $true
                        }
                    }
                }
            }
        }
        if ($broad) { return 'BROAD_INBOUND_ALLOW_RULE_OBSERVED' }
        if ($restricted) { return 'EXACT_PEER_ALLOW_RULE_OBSERVED' }
        return 'NO_EXACT_PEER_ALLOW_RULE'
    } catch {
        return 'FIREWALL_INSPECTION_UNAVAILABLE'
    }
}
function Get-ObservedSshd {
    try {
        $service = Get-Service -Name 'sshd' -ErrorAction Stop
        if ($service.Status -eq 'Running') { return 'RUNNING_OBSERVED' }
        return 'NOT_RUNNING'
    } catch {
        return 'NOT_INSTALLED_OR_INACCESSIBLE'
    }
}
try {
    if ($Role -eq 'pc-a') {
        $result = [ordered]@{
            status = 'WINDOWS_OBSERVATIONS'
            platform = 'WINDOWS'
            role = 'pc-a'
            coordinator_task = Get-ObservedTask
            coordinator_listener = Get-ObservedListener
            firewall_rule = Get-ObservedFirewall
            sshd_service = Get-ObservedSshd
            firewall_effective_policy_verified = $false
            two_host_verified = $false
            ready = $false
        }
    } else {
        $result = [ordered]@{
            status = 'WINDOWS_OBSERVATIONS'
            platform = 'WINDOWS'
            role = 'pc-b'
            coordinator_task = 'NOT_APPLICABLE'
            coordinator_listener = 'NOT_APPLICABLE'
            firewall_rule = 'NOT_APPLICABLE'
            sshd_service = 'NOT_APPLICABLE'
            firewall_effective_policy_verified = $false
            two_host_verified = $false
            ready = $false
        }
    }
    Write-Output ($result | ConvertTo-Json -Compress -Depth 3)
} catch {
    Write-Output '{"status":"WINDOWS_INSPECTION_FAILED","ready":false}'
    exit 6
}
