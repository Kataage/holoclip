# Windows PowerShell 5.1: lock down or inspect a Holoclip LAN credential/staging path.
# A real path is required. Never displays secret bytes or changes unrelated files.
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
function Fail-Acl([string]$code) {
    Write-Output ('{"status":"' + $code + '","acl_verified":false,"ready":false}')
    exit 6
}
try {
    if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
        Fail-Acl 'WINDOWS_REQUIRED'
    }
    $full = [IO.Path]::GetFullPath($Path)
    $walk = $full
    while ($walk) {
        if (Test-Path -LiteralPath $walk) {
            $item = Get-Item -LiteralPath $walk -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail-Acl 'ACL_UNSAFE_REPARSE_POINT'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($walk)
        if (-not $parent -or $parent -eq $walk) { break }
        $walk = $parent
    }
    if (-not (Test-Path -LiteralPath $full)) {
        Fail-Acl 'ACL_TARGET_MISSING'
    }
    $entry = Get-Item -LiteralPath $full -Force
    $isDirectory = $entry.PSIsContainer
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $allowed = @($current.Value, 'S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $full
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    # Never take ownership or change an unrelated owner's ACL.
    if ($owner -ne $current.Value) {
        Fail-Acl 'ACL_UNEXPECTED_OWNER'
    }
    if ($Apply) {
        $acl.SetAccessRuleProtection($true, $false)
        $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
        foreach ($rule in $rules) {
            $acl.RemoveAccessRuleSpecific($rule)
        }
        foreach ($id in $allowed) {
            $sid = [Security.Principal.SecurityIdentifier]::new($id)
            if ($isDirectory) {
                $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                    $sid, [Security.AccessControl.FileSystemRights]::FullControl,
                    ([Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'),
                    [Security.AccessControl.PropagationFlags]::None,
                    [Security.AccessControl.AccessControlType]::Allow)
            } else {
                $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                    $sid, [Security.AccessControl.FileSystemRights]::FullControl,
                    [Security.AccessControl.AccessControlType]::Allow)
            }
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $full -AclObject $acl
        $acl = Get-Acl -LiteralPath $full
    }
    if (-not $acl.AreAccessRulesProtected) {
        Fail-Acl 'ACL_INHERITANCE_ENABLED'
    }
    $hasCurrentAccess = $false
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IsInherited) {
            Fail-Acl 'ACL_INHERITED_RULE'
        }
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            if ($rule.IdentityReference.Value -notin $allowed) {
                Fail-Acl 'ACL_UNEXPECTED_ACCESS'
            }
            if ($rule.IdentityReference.Value -eq $current.Value) {
                $hasCurrentAccess = $true
            }
        }
    }
    if (-not $hasCurrentAccess) {
        Fail-Acl 'ACL_CURRENT_USER_NO_ACCESS'
    }
    Write-Output '{"status":"ACL_SECURE","acl_verified":true,"ready":false}'
} catch {
    # Avoid emitting path, username, ACL object or other sensitive exception text.
    Fail-Acl 'ACL_CHECK_FAILED'
}
