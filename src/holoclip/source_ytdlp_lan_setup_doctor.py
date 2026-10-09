"""Read-only LAN deployment audit: local observations are not two-PC evidence.

The doctor never launches yt-dlp, reserves an access lease, registers a task,
changes a firewall, installs sshd, edits ACLs or sends credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_SECRET_PATH, _client_config_path, _private_ipv4,
    _read_client_config, _reject_linked_path, _secret,
)
from holoclip.source_ytdlp_lan_onboarding import diagnose_lan_client
from holoclip.source_ytdlp_lan_ssh_transfer import _secure_windows_path

_SAFE_WINDOWS_CODES = {
    "coordinator_task": frozenset({"RUNNING_OBSERVED", "NOT_RUNNING",
                                   "NOT_FOUND_OR_INACCESSIBLE", "NOT_APPLICABLE"}),
    "coordinator_listener": frozenset({"LAN_LISTENER_OBSERVED", "NO_LAN_LISTENER",
                                      "WILDCARD_LISTENER_OBSERVED", "NOT_OBSERVED",
                                      "NOT_APPLICABLE"}),
    "firewall_rule": frozenset({"PEER_IP_REQUIRED", "BROAD_INBOUND_ALLOW_RULE_OBSERVED",
                                "EXACT_PEER_ALLOW_RULE_OBSERVED", "NO_EXACT_PEER_ALLOW_RULE",
                                "INCOMPLETE_RULE_INSPECTION", "FIREWALL_INSPECTION_UNAVAILABLE",
                                "NOT_APPLICABLE"}),
    "sshd_service": frozenset({"RUNNING_OBSERVED", "NOT_RUNNING",
                               "NOT_INSTALLED_OR_INACCESSIBLE", "NOT_APPLICABLE"}),
}
_ACTIONS = {
    "NOT_CONFIGURED": "Run examples-ytdlp-lan-configure on this PC; local-only pacing is not shared-IP-safe.",
    "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID": "Repair the configured LAN JSON/required marker and secret-file path; never delete the required marker to bypass protection.",
    "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE": "Check PC-A logon task, bind IP, PC-B-to-PC-A firewall and LAN reachability; do not fall back to local pacing.",
    "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED": "Check matching shared secret and authentic PC-A identity; never disable verification.",
    "LAN_CREDENTIAL_MISSING_OR_INVALID": "Initialize on PC-A or securely import the shared secret on PC-B; never paste key bytes into logs.",
    "LAN_CREDENTIAL_ACL_UNVERIFIED": "Run examples-ytdlp-lan-secret-acl --apply on this PC, then rerun the doctor. Review Windows NTFS access rights.",
    "WINDOWS_INSPECTION_UNAVAILABLE": "Run from Windows with PowerShell 5.1, native networking cmdlets and task read access.",
    "NOT_RUNNING": "On PC-A inspect the installed coordinator Task Scheduler task; start it only after firewall scoping.",
    "NOT_FOUND_OR_INACCESSIBLE": "On PC-A register the logon task with install-ytdlp-lan-task.ps1, or correct task name/read access.",
    "NO_LAN_LISTENER": "On PC-A verify the coordinator is listening on its explicit private IPv4 (not loopback).",
    "WILDCARD_LISTENER_OBSERVED": "Avoid wildcard listener addresses; configure an explicit RFC1918 LAN IPv4.",
    "NOT_OBSERVED": "Review PC-A listener/network cmdlet access; local TCP listening was not verified.",
    "PEER_IP_REQUIRED": "Supply --peer-ip with PC-B's private IPv4 to inspect a narrowly scoped TCP firewall rule.",
    "NO_EXACT_PEER_ALLOW_RULE": "Restrict an inbound TCP coordinator-port allow rule to only PC-B's private IP.",
    "BROAD_INBOUND_ALLOW_RULE_OBSERVED": "Remove or restrict broad inbound allow rules for the coordinator TCP port; do not expose it to the internet.",
    "INCOMPLETE_RULE_INSPECTION": "Review Windows Firewall active rules as administrator; scope is not verified.",
    "FIREWALL_INSPECTION_UNAVAILABLE": "Inspect Windows Firewall ActiveStore with sufficient read permission.",
    "HOST_PIN_NOT_SUPPLIED": "If using SSH transfer, pass --ssh-host and --known-hosts with an independently verified PC-A host key.",
    "HOST_PIN_NOT_FOUND": "Verify PC-A's SSH fingerprint independently and store its pinned key in a dedicated known_hosts file.",
    "SSH_KEYGEN_UNAVAILABLE": "Install Windows OpenSSH client to inspect a preverified known_hosts entry.",
}
_WINDOWS_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect-ytdlp-lan-windows.ps1"


def _item(code: str, *, optional: bool = False) -> dict[str, object]:
    return {"code": code, "action": _ACTIONS.get(code, "No action required."),
            "optional": optional}


def _secret_path(selected: Path | None, role: str) -> Path:
    if selected is not None:
        return selected.absolute()
    if role == "pc-a":
        return DEFAULT_SECRET_PATH.absolute()
    legacy = os.environ.get("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", "").strip()
    if legacy:
        return Path(legacy).absolute()
    config, _ = _client_config_path()
    _, secret = _read_client_config(config)
    return secret


def _is_windows() -> bool:
    return os.name == "nt"


def _observe_windows(
    *, role: str, port: int, task_name: str, peer_ip: str | None,
) -> dict[str, str] | None:
    if not _is_windows():
        return None
    ps = shutil.which("powershell.exe")
    if ps is None or not _WINDOWS_SCRIPT.is_file():
        return None
    args = [
        ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(_WINDOWS_SCRIPT), "-Role", role,
        "-CoordinatorPort", str(port), "-TaskName", task_name,
    ]
    if peer_ip is not None:
        args.extend(["-PeerIp", peer_ip])
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=25, check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        if (not isinstance(data, dict) or data.get("status") != "WINDOWS_OBSERVATIONS"
                or data.get("role") != role or data.get("ready") is not False
                or data.get("firewall_effective_policy_verified") is not False
                or data.get("two_host_verified") is not False):
            return None
        for field, allowed in _SAFE_WINDOWS_CODES.items():
            if data.get(field) not in allowed:
                return None
        return {k: data[k] for k in _SAFE_WINDOWS_CODES}
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def _host_pin_state(host: str | None, known_hosts: Path | None, ssh_port: int) -> str:
    if host is None and known_hosts is None:
        return "HOST_PIN_NOT_SUPPLIED"
    if host is None or known_hosts is None:
        return "HOST_PIN_NOT_FOUND"
    _private_ipv4(host)
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError("Invalid SSH port")
    file = known_hosts.absolute()
    _reject_linked_path(file)
    if not file.is_file() or not 1 <= file.stat().st_size <= 1024 * 1024:
        return "HOST_PIN_NOT_FOUND"
    binary = shutil.which("ssh-keygen")
    if binary is None:
        return "SSH_KEYGEN_UNAVAILABLE"
    host_label = host if ssh_port == 22 else f"[{host}]:{ssh_port}"
    try:
        result = subprocess.run([binary, "-F", host_label, "-f", str(file)],
                                capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "HOST_PIN_NOT_FOUND"
    # Presence only; host-key fingerprint MUST have been verified out of band.
    return "HOST_PIN_ENTRY_OBSERVED" if result.returncode == 0 else "HOST_PIN_NOT_FOUND"


def inspect_lan_setup(
    *, role: str, secret_file: Path | None = None,
    peer_ip: str | None = None, known_hosts: Path | None = None,
    ssh_host: str | None = None, ssh_port: int = 22,
    port: int = 8878, task_name: str = "Holoclip yt-dlp LAN Coordinator",
) -> dict[str, object]:
    if role not in {"pc-a", "pc-b"} or not 1024 <= port <= 65535:
        raise ValueError("Invalid role/port")
    if peer_ip is not None:
        _private_ipv4(peer_ip)
    if not isinstance(task_name, str) or not task_name or len(task_name) > 90 or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-" for c in task_name
    ):
        raise ValueError("Invalid task name")
    report: dict[str, object] = {
        "status": "LOCAL_SETUP_INCOMPLETE",
        "role": role,
        "checks": {},
        "youtube_contacted": False,
        "media_downloaded": False,
        "config_modified": False,
        "two_host_verified": False,
        "firewall_effective_policy_verified": False,
        "production_ready": False,
        "ready": False,
    }
    checks: dict[str, dict[str, object]] = report["checks"]
    client = diagnose_lan_client()
    checks["signed_coordinator_status"] = _item(str(client["status"]))
    checks["signed_coordinator_status"]["observed_remote_gate"] = client["remote_gate_status"]
    if client["authenticated"]:
        checks["signed_coordinator_status"] = _item("AUTHENTICATED_LOCAL_LAN_COORDINATOR")
        checks["signed_coordinator_status"]["observed_remote_gate"] = client["remote_gate_status"]
    try:
        path = _secret_path(secret_file, role)
        _reject_linked_path(path)
        _secret(path)
        checks["local_credential"] = _item("LAN_CREDENTIAL_VALID")
        if _is_windows():
            try:
                acl = _secure_windows_path(path, apply=False)
            except (ValueError, OSError):
                acl = False
            checks["credential_acl"] = _item(
                "LAN_CREDENTIAL_ACL_OBSERVED" if acl else "LAN_CREDENTIAL_ACL_UNVERIFIED",
            )
        else:
            checks["credential_acl"] = _item("WINDOWS_INSPECTION_UNAVAILABLE")
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError):
        checks["local_credential"] = _item("LAN_CREDENTIAL_MISSING_OR_INVALID")
        checks["credential_acl"] = _item("LAN_CREDENTIAL_ACL_UNVERIFIED")
    observations = _observe_windows(role=role, port=port, task_name=task_name,
                                    peer_ip=peer_ip)
    if observations is None:
        for field in _SAFE_WINDOWS_CODES:
            checks[field] = _item("WINDOWS_INSPECTION_UNAVAILABLE",
                                  optional=role == "pc-b" or field == "sshd_service")
    else:
        for field in _SAFE_WINDOWS_CODES:
            checks[field] = _item(observations[field],
                                  optional=role == "pc-b" or field == "sshd_service")
    checks["ssh_host_pin"] = _item(_host_pin_state(ssh_host, known_hosts, ssh_port),
                                   optional=ssh_host is None and known_hosts is None)
    # "Local complete" does not assert effective firewall security or
    # validation by both physical machines. It is never production readiness.
    needed = ["signed_coordinator_status", "local_credential", "credential_acl"]
    expected = {
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
    }
    if role == "pc-a":
        needed += ["coordinator_task", "coordinator_listener", "firewall_rule"]
        expected.update({
            "coordinator_task": "RUNNING_OBSERVED",
            "coordinator_listener": "LAN_LISTENER_OBSERVED",
            "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
        })
    if ssh_host is not None or known_hosts is not None:
        needed += ["ssh_host_pin"]
        expected["ssh_host_pin"] = "HOST_PIN_ENTRY_OBSERVED"
    if all(checks[key]["code"] == expected[key] for key in needed):
        report["status"] = "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
    return report
