"""Preview-first, no-overwrite Windows PC-A coordinator logon-task registration.

Never starts a task, creates a secret, modifies Windows Firewall, or contacts
YouTube. Apply requires a valid pinned local secret/NTFS ACL, a narrowly
scoped observed firewall rule, verified absence of a similarly named task,
and a specifically selected LAN interface address.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_SECRET_PATH, DEFAULT_COORDINATOR_STATE, _private_ipv4,
    _reject_linked_path, _secret,
)
from holoclip.source_ytdlp_lan_local_bootstrap import _safe_local_storage
from holoclip.source_ytdlp_lan_setup_doctor import _observe_windows
from holoclip.source_ytdlp_lan_ssh_transfer import _secure_windows_path

_ROOT = Path(__file__).resolve().parents[2]
_PROBE = _ROOT / "scripts" / "probe-ytdlp-lan-task.ps1"
_INSTALL = _ROOT / "scripts" / "install-ytdlp-lan-task.ps1"
_TASK_NAME = re.compile(r"[A-Za-z0-9 _-]{1,90}\Z")


def _windows() -> bool:
    return os.name == "nt"


def _task_state(name: str) -> str:
    exe = shutil.which("powershell.exe")
    if not exe or not _PROBE.is_file():
        return "LAN_TASK_ENUMERATION_UNAVAILABLE"
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(_PROBE), "-TaskName", name],
            capture_output=True, timeout=20, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 512:
            return "LAN_TASK_ENUMERATION_UNAVAILABLE"
        decoded = json.loads(result.stdout)
        if (not isinstance(decoded, dict) or
                set(decoded) != {"status", "ready"} or
                decoded["ready"] is not False or
                decoded["status"] not in {
                    "LAN_TASK_ABSENT_OBSERVED", "LAN_TASK_ALREADY_PRESENT",
                }):
            return "LAN_TASK_ENUMERATION_UNAVAILABLE"
        return decoded["status"]
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return "LAN_TASK_ENUMERATION_UNAVAILABLE"


def inspect_lan_task_registration(
    *, bind_ip: str, peer_ip: str, port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
    secret_file: Path = DEFAULT_SECRET_PATH,
    state_path: Path = DEFAULT_COORDINATOR_STATE,
    apply: bool = False,
) -> dict[str, object]:
    """Return safe fixed-code preview, optionally register *one* logon task."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("LAN_TASK_INVALID_PORT")
    bind = _private_ipv4(bind_ip)
    peer = _private_ipv4(peer_ip)
    if bind.startswith("127.") or peer.startswith("127.") or bind == peer:
        raise ValueError("LAN_TASK_INVALID_PRIVATE_PEER")
    if not isinstance(task_name, str) or not _TASK_NAME.fullmatch(task_name):
        raise ValueError("LAN_TASK_INVALID_NAME")
    secret = _safe_local_storage(secret_file)
    state = _safe_local_storage(state_path)
    if secret == state or secret in state.parents or state in secret.parents:
        raise ValueError("LAN_TASK_OVERLAPPING_STORAGE")
    for target in (secret, state):
        _reject_linked_path(target)
        if any(x in str(target) for x in ('"', "\r", "\n", "\x00")):
            raise ValueError("LAN_TASK_UNSAFE_STORAGE_PATH")
    flags = {
        "credential": "LAN_TASK_CREDENTIAL_UNVERIFIED",
        "credential_acl": "LAN_TASK_ACL_UNVERIFIED",
        "firewall": "LAN_TASK_FIREWALL_UNVERIFIED",
        "task": "LAN_TASK_ENUMERATION_UNAVAILABLE",
    }
    if not _windows():
        flags["platform"] = "LAN_TASK_WINDOWS_REQUIRED"
    else:
        flags["platform"] = "LAN_TASK_WINDOWS_OBSERVED"
        try:
            _secret(secret)
            flags["credential"] = "LAN_TASK_CREDENTIAL_VALID"
            if _secure_windows_path(secret, apply=False) is True:
                flags["credential_acl"] = "LAN_TASK_ACL_SECURE"
        except (OSError, ValueError, UnicodeError):
            pass
        observations = _observe_windows(
            role="pc-a", port=port, task_name=task_name, peer_ip=peer,
        )
        if observations is not None:
            flags["firewall"] = (
                "LAN_TASK_EXACT_PEER_RULE_OBSERVED"
                if observations["firewall_rule"] == "EXACT_PEER_ALLOW_RULE_OBSERVED"
                else "LAN_TASK_FIREWALL_UNVERIFIED"
            )
        flags["task"] = _task_state(task_name)
    blockers = {
        key: value for key, value in flags.items()
        if value != {
            "credential": "LAN_TASK_CREDENTIAL_VALID",
            "credential_acl": "LAN_TASK_ACL_SECURE",
            "firewall": "LAN_TASK_EXACT_PEER_RULE_OBSERVED",
            "task": "LAN_TASK_ABSENT_OBSERVED",
            "platform": "LAN_TASK_WINDOWS_OBSERVED",
        }[key]
    }
    report: dict[str, object] = {
        "status": "LAN_TASK_PREVIEW_BLOCKED" if blockers else "LAN_TASK_PREVIEW_ELIGIBLE",
        "checks": flags,
        "blockers": blockers,
        "action": "REGISTER_NON_ELEVATED_CURRENT_USER_LOGON_TASK",
        "task_started": False,
        "task_registered": False,
        "existing_task_overwritten": False,
        "firewall_modified": False,
        "firewall_effective_policy_verified": False,
        "ssh_trust_modified": False,
        "credential_transferred": False,
        "youtube_contacted": False,
        "media_downloaded": False,
        "two_host_verified": False,
        "ready": False,
    }
    if not apply:
        return report
    if blockers:
        report["status"] = "LAN_TASK_APPLY_BLOCKED"
        return report
    if not _INSTALL.is_file():
        raise ValueError("LAN_TASK_INSTALLER_UNAVAILABLE")
    ps = shutil.which("powershell.exe")
    if not ps:
        raise ValueError("LAN_TASK_INSTALLER_UNAVAILABLE")
    # Reprobe immediately before registration. The installer intentionally
    # does NOT use -Force, so concurrent name creation cannot be overwritten.
    if _task_state(task_name) != "LAN_TASK_ABSENT_OBSERVED":
        report["status"] = "LAN_TASK_APPLY_BLOCKED"
        report["blockers"] = {"task": "LAN_TASK_ALREADY_PRESENT_OR_UNVERIFIED"}
        return report
    try:
        completed = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(_INSTALL), "-TaskName", task_name,
             "-BindIp", bind, "-Port", str(port),
             "-SecretFile", str(secret), "-StatePath", str(state)],
            capture_output=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LAN_TASK_INSTALL_FAILED") from exc
    if completed.returncode != 0:
        # Never echo raw PowerShell errors, machine usernames or file paths.
        raise ValueError("LAN_TASK_INSTALL_FAILED")
    report["status"] = "LAN_TASK_REGISTERED_NOT_STARTED"
    report["task_registered"] = True
    return report
