"""Safe Windows PC-A firewall preparation with explicit opt-in application.

Only invokes the pinned offline Windows script. Caller data never becomes
PowerShell source text, and diagnostic output is a strict fixed-code contract.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import _private_ipv4

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare-ytdlp-lan-firewall.ps1"
_STATUSES = frozenset({
    "LAN_FIREWALL_PREVIEW_BLOCKED", "LAN_FIREWALL_PREVIEW_ELIGIBLE",
    "LAN_FIREWALL_APPLY_BLOCKED", "LAN_FIREWALL_ALREADY_SCOPED",
    "LAN_FIREWALL_RULE_ADDED", "LAN_FIREWALL_ADDED_UNVERIFIED",
    "LAN_FIREWALL_APPLY_UNCONFIRMED",
})
_CHECKS = frozenset({
    "LAN_FIREWALL_UNSAFE_ADDRESS", "LAN_FIREWALL_INSPECTION_UNAVAILABLE",
    "LAN_FIREWALL_BIND_NOT_UNIQUE", "LAN_FIREWALL_PRIVATE_NETWORK_REQUIRED",
    "LAN_FIREWALL_PRIVATE_PROFILE_UNVERIFIED", "LAN_FIREWALL_INVENTORY_UNVERIFIED",
    "LAN_FIREWALL_RULE_NAME_CONFLICT", "LAN_FIREWALL_SCOPE_BLOCKED",
    "LAN_FIREWALL_ALREADY_SCOPED", "LAN_FIREWALL_CAN_ADD_EXACT_RULE",
    "LAN_FIREWALL_EXACT_SCOPE_OBSERVED", "LAN_FIREWALL_CMDLET_FAILURE",
})
_KEYS = frozenset({
    "status", "check", "rule_name", "rule_added", "changes_may_be_partial",
    "existing_rules_replaced", "existing_rules_deleted", "firewall_disabled",
    "task_modified", "youtube_contacted", "media_downloaded",
    "firewall_effective_policy_verified", "two_host_verified", "ready",
})
_FALSE = frozenset({
    "existing_rules_replaced", "existing_rules_deleted", "firewall_disabled",
    "task_modified", "youtube_contacted", "media_downloaded",
    "firewall_effective_policy_verified", "two_host_verified", "ready",
})
_ADDED = frozenset({"LAN_FIREWALL_RULE_ADDED", "LAN_FIREWALL_ADDED_UNVERIFIED"})


def _blocked(status: str, *, apply: bool) -> dict[str, object]:
    return {
        "status": status,
        "rule_added": False,
        "changes_may_be_partial": apply,
        "firewall_effective_policy_verified": False,
        "two_host_verified": False,
        "ready": False,
    }


def prepare_lan_firewall(
    *, bind_ip: str, peer_ip: str, port: int = 8878, apply: bool = False,
) -> dict[str, object]:
    """Preview an exact PC-B allow rule; mutate *only* with explicit apply."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("LAN_FIREWALL_INVALID_PORT")
    try:
        bind = _private_ipv4(bind_ip)
        peer = _private_ipv4(peer_ip)
        if bind.startswith("127.") or peer.startswith("127.") or bind == peer:
            raise ValueError("LAN_FIREWALL_UNSAFE_ADDRESS")
        if bind != bind_ip or peer != peer_ip:
            raise ValueError("LAN_FIREWALL_UNSAFE_ADDRESS")
    except (ValueError, TypeError) as exc:
        raise ValueError("LAN_FIREWALL_UNSAFE_ADDRESS") from exc
    if os.name != "nt":
        return _blocked("LAN_FIREWALL_WINDOWS_REQUIRED", apply=apply)
    ps = shutil.which("powershell.exe")
    if not ps or not _SCRIPT.is_file():
        return _blocked("LAN_FIREWALL_TOOL_UNAVAILABLE", apply=apply)
    cmd = [
        ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(_SCRIPT), "-BindIp", bind, "-PeerIp", peer,
        "-Port", str(port),
    ]
    if apply:
        cmd.append("-Apply")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=45, check=False)
        if result.returncode != 0 or not 1 <= len(result.stdout) <= 4096:
            return _blocked("LAN_FIREWALL_PROBE_UNTRUSTED", apply=apply)
        data = json.loads(result.stdout)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return _blocked("LAN_FIREWALL_PROBE_UNTRUSTED", apply=apply)
    if (
        not isinstance(data, dict)
        or set(data) != _KEYS
        or data.get("status") not in _STATUSES
        or data.get("check") not in _CHECKS
        or data.get("rule_name") != f"HoloclipYtDlpLanTcp-{port}"
        or any(data.get(key) is not False for key in _FALSE)
        or type(data.get("rule_added")) is not bool
        or type(data.get("changes_may_be_partial")) is not bool
    ):
        return _blocked("LAN_FIREWALL_PROBE_UNTRUSTED", apply=apply)
    status = data["status"]
    if (
        data["rule_added"] != (status in _ADDED)
        or data["changes_may_be_partial"] != (
            apply and status == "LAN_FIREWALL_APPLY_UNCONFIRMED"
        )
        or (not apply and status in _ADDED | {
            "LAN_FIREWALL_APPLY_UNCONFIRMED", "LAN_FIREWALL_APPLY_BLOCKED"
        })
        or (apply and status == "LAN_FIREWALL_PREVIEW_ELIGIBLE")
    ):
        return _blocked("LAN_FIREWALL_PROBE_UNTRUSTED", apply=apply)
    return data
