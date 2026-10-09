# Windows PowerShell 5.1, READ ONLY. No Task Scheduler/firewall/sshd changes.
# Emits fixed diagnostic codes only. No secret contents, user paths or host keys.
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
        foreach ($item in $listeners) {
            if ($item.LocalAddress -and $item.LocalAddress -ne '127.0.0.1' -and
                $item.LocalAddress -ne '::1' -and $item.LocalAddress -ne '::') {
                return 'LAN_LISTENER_OBSERVED'
            }
        }
        return 'NO_LAN_LISTENER'
    } catch {
        return 'NOT_OBSERVED'
    }
}
function Get-ObservedFirewall {
    if ([string]::IsNullOrWhiteSpace($PeerIp)) { return 'PEER_IP_REQUIRED' }
    try {
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -Action Allow -ErrorAction Stop)
        $broad = $false
        $restricted = $false
        foreach ($rule in $rules) {
            try {
                $ports = @(Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
                $addresses = @(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
            } catch {
                return 'INCOMPLETE_RULE_INSPECTION'
            }
            foreach ($port in $ports) {
                if ([string]$port.Protocol -notin @('TCP', '6')) { continue }
                $ranges = @(([string]$port.LocalPort) -split ',')
                if ([string]$CoordinatorPort -notin $ranges -and 'Any' -notin $ranges) {
                    continue
                }
                foreach ($address in $addresses) {
                    $peers = @(([string]$address.RemoteAddress) -split ',')
                    if ($peers -contains 'Any' -or $peers -contains 'LocalSubnet' -or
                        $peers -contains '*' -or $ranges -contains 'Any') {
                        $broad = $true
                    } elseif ($peers.Count -eq 1 -and $peers[0] -eq $PeerIp) {
                        $restricted = $true
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
    [Console]::Out.WriteLine(($result | ConvertTo-Json -Compress -Depth 3))
} catch {
    [Console]::Out.WriteLine('{"status":"WINDOWS_INSPECTION_FAILED","ready":false}')
    exit 6
}
