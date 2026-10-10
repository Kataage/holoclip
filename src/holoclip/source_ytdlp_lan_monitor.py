"""Opt-in, no-media Windows LAN observation monitor and repeated-alert marker.

A run records exactly one paired read-only diagnosis. It NEVER requests a
broker lease, even if an older operator workflow supports optional lease tests.
Only an allowlisted fixed-code alert is persisted. Windows popup is best effort;
the local marker survives unavailable/locked desktop sessions.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_COORDINATOR_STATE, DEFAULT_SECRET_PATH,
    _reject_linked_path, _private_ipv4,
)
from holoclip.source_ytdlp_lan_evidence_history import (
    DEFAULT_EVIDENCE_DB, _ALLOWED_ISSUES, _path as _evidence_path,
    capture_lan_evidence,
)
from holoclip.source_ytdlp_lan_local_bootstrap import _safe_local_storage
from holoclip.source_ytdlp_lan_pair_collect import _pinned_ssh_command
from holoclip.source_ytdlp_lan_setup_doctor import _host_pin_state
from holoclip.source_ytdlp_lan_task_prepare import _TASK_NAME, _task_state, _windows

DEFAULT_MONITOR_ALERT = Path("data/runtime/yt-dlp-lan-monitor-alert.json")
_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _ROOT / "scripts" / "install-ytdlp-lan-monitor-task.ps1"
_ALLOWED_ALERTS = frozenset({
    "NONE", "REPEATED_SAME_LAN_DEFICITS",
    "REPEATED_INCOMPLETE_LAN_DIAGNOSIS",
})
_VERSION = "holoclip-lan-monitor-alert-v1"


def _alert_path(path: Path, db: Path) -> Path:
    try:
        target = _safe_local_storage(path)
        forbidden = (
            _evidence_path(db),
            DEFAULT_SECRET_PATH.absolute(), DEFAULT_COORDINATOR_STATE.absolute(),
            DEFAULT_CLIENT_CONFIG.absolute(),
            DEFAULT_CLIENT_CONFIG.with_name(DEFAULT_CLIENT_CONFIG.name + ".required").absolute(),
        )
        if (
            target.suffix.lower() != ".json" or target in forbidden
            or any(target in item.parents or item in target.parents for item in forbidden)
            or any(char in str(target) for char in ('"', "\r", "\n", "\x00"))
        ):
            raise ValueError("LAN_MONITOR_UNSAFE_ALERT_PATH")
        _reject_linked_path(target)
    except (TypeError, OSError, ValueError) as exc:
        raise ValueError("LAN_MONITOR_UNSAFE_ALERT_PATH") from exc
    return target


def _read_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    if not path.is_file() or not 1 <= path.stat().st_size <= 2048:
        raise ValueError("LAN_MONITOR_ALERT_CORRUPT")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, OSError, ValueError) as exc:
        raise ValueError("LAN_MONITOR_ALERT_CORRUPT") from exc
    if (
        not isinstance(marker, dict)
        or set(marker) != {"version", "alert", "issue_keys", "observed_at_unix", "ready"}
        or marker.get("version") != _VERSION
        or marker.get("alert") not in _ALLOWED_ALERTS
        or marker.get("ready") is not False
        or type(marker.get("observed_at_unix")) is not int
        or marker["observed_at_unix"] < 0
        or not isinstance(marker.get("issue_keys"), list)
        or len(marker["issue_keys"]) > len(_ALLOWED_ISSUES)
        or any(type(item) is not str or item not in _ALLOWED_ISSUES
               for item in marker["issue_keys"])
        or marker["issue_keys"] != sorted(set(marker["issue_keys"]))
        or (marker["alert"] == "NONE" and marker["issue_keys"])
    ):
        raise ValueError("LAN_MONITOR_ALERT_CORRUPT")
    return marker


def _write_marker(path: Path, marker: dict) -> None:
    raw = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > 2048:
        raise ValueError("LAN_MONITOR_ALERT_CORRUPT")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(path)
    handle, temporary = tempfile.mkstemp(prefix=".lan-monitor-alert-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(raw)
        _reject_linked_path(path)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _notify_windows() -> bool:
    """Best-effort current-user msg.exe alert; never broadcast or log raw stderr."""
    if not _windows():
        return False
    executable = shutil.which("msg.exe")
    if not executable:
        return False
    user = getpass.getuser()
    if not user or len(user) > 128 or any(ord(c) < 32 or c in '"/\\' for c in user):
        return False
    try:
        result = subprocess.run(
            [executable, user, "/time:10",
             "Holoclip LAN: repeated setup or connection problems. "
             "Check examples-ytdlp-lan-monitor-alert."],
            capture_output=True, timeout=8, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_lan_monitor_alert(
    *, alert_file: Path = DEFAULT_MONITOR_ALERT,
    evidence_db: Path = DEFAULT_EVIDENCE_DB,
) -> dict:
    marker = _read_marker(_alert_path(alert_file, evidence_db))
    return {
        "status": ("LAN_MONITOR_ALERT_PRESENT" if marker and marker["alert"] != "NONE"
                   else "LAN_MONITOR_NO_ALERT_RECORDED" if marker
                   else "LAN_MONITOR_NEVER_RECORDED"),
        "alert": marker["alert"] if marker else "NONE",
        "issue_keys": marker["issue_keys"] if marker else [],
        "observed_at_unix": marker["observed_at_unix"] if marker else None,
        "desktop_notification_delivered": False,
        "continuous_monitoring_verified": False,
        "ready": False,
    }


def run_lan_monitor_once(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
    evidence_db: Path = DEFAULT_EVIDENCE_DB,
    alert_file: Path = DEFAULT_MONITOR_ALERT,
) -> dict:
    """One scheduled observation; no live lease, no YouTube, no auto-repair."""
    alert_path = _alert_path(alert_file, evidence_db)
    previous_marker = _read_marker(alert_path)  # Fail before any SSH on corruption.
    result = capture_lan_evidence(
        host=host, user=user, known_hosts=known_hosts,
        remote_repo=remote_repo, ssh_port=ssh_port,
        coordinator_port=coordinator_port, task_name=task_name,
        evidence_db=evidence_db, record=True, include_pacing_trial=False,
    )
    history = result["history"]
    if not isinstance(history, dict) or history.get("alert") not in _ALLOWED_ALERTS:
        raise ValueError("LAN_MONITOR_UNTRUSTED_HISTORY")
    alert = history["alert"]
    issues = history.get("repeated_issue_keys", []) if alert == "REPEATED_SAME_LAN_DEFICITS" else []
    if (
        not isinstance(issues, list)
        or issues != sorted(set(issues))
        or any(code not in _ALLOWED_ISSUES for code in issues)
    ):
        raise ValueError("LAN_MONITOR_UNTRUSTED_HISTORY")
    observed = history["latest_observation"]
    if (not isinstance(observed, dict)
            or type(observed.get("observed_at_unix")) is not int):
        raise ValueError("LAN_MONITOR_UNTRUSTED_HISTORY")
    marker = {
        "version": _VERSION, "alert": alert, "issue_keys": issues,
        "observed_at_unix": observed["observed_at_unix"], "ready": False,
    }
    old_key = (
        previous_marker["alert"], tuple(previous_marker["issue_keys"])
    ) if previous_marker else None
    new_key = (alert, tuple(issues))
    changed = old_key != new_key
    if changed:
        _write_marker(alert_path, marker)
    delivered = _notify_windows() if changed and alert != "NONE" else False
    return {
        "status": "LAN_MONITOR_OBSERVATION_RECORDED",
        "diagnosis": result["diagnosis"],
        "alert": alert,
        "issue_keys": issues,
        "alert_state_changed": changed,
        "desktop_notification_attempted": changed and alert != "NONE",
        "desktop_notification_delivered": delivered,
        "durable_alert_marker_updated": changed,
        "history_observations": history["retained_observations"],
        "lease_requested": False,
        "youtube_contacted": False,
        "task_modified": False,
        "firewall_modified": False,
        "continuous_monitoring_verified": False,
        "production_ready": False,
        "two_host_verified": False,
        "ready": False,
    }


def prepare_lan_monitor_task(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    interval_minutes: int = 60,
    task_name: str = "Holoclip yt-dlp LAN Monitor",
    evidence_db: Path = DEFAULT_EVIDENCE_DB,
    alert_file: Path = DEFAULT_MONITOR_ALERT,
    apply: bool = False,
) -> dict:
    """Only explicitly register current-user task if name is absent."""
    if type(interval_minutes) is not int or not 15 <= interval_minutes <= 1440:
        raise ValueError("LAN_MONITOR_INVALID_INTERVAL")
    if not isinstance(task_name, str) or not _TASK_NAME.fullmatch(task_name):
        raise ValueError("LAN_MONITOR_INVALID_TASK_NAME")
    peer = _private_ipv4(host)
    if peer != host or peer.startswith("127."):
        raise ValueError("LAN_MONITOR_UNSAFE_PEER")
    if type(coordinator_port) is not int or not 1024 <= coordinator_port <= 65535:
        raise ValueError("LAN_MONITOR_INVALID_COORDINATOR_PORT")
    _evidence_path(evidence_db)
    _alert_path(alert_file, evidence_db)
    # The existing strict pinned SSH command constructor validates
    # user, host, port, known_hosts and remote repository without connecting.
    _pinned_ssh_command(
        host=host, user=user, known_hosts=known_hosts,
        remote_repo=remote_repo, port=ssh_port, remote_probe="onboard",
    )
    # ssh-keygen -F verifies that this peer has a pinned entry in the
    # supplied dedicated file. Fingerprint provenance remains out-of-band.
    if _host_pin_state(peer, known_hosts, ssh_port) != "HOST_PIN_ENTRY_OBSERVED":
        raise ValueError("LAN_MONITOR_PINNED_HOST_KEY_REQUIRED")
    state = _task_state(task_name) if _windows() else "LAN_MONITOR_WINDOWS_REQUIRED"
    report: dict[str, object] = {
        "status": ("LAN_MONITOR_TASK_PREVIEW_ELIGIBLE"
                   if state == "LAN_TASK_ABSENT_OBSERVED"
                   else "LAN_MONITOR_TASK_PREVIEW_BLOCKED"),
        "task_state": state,
        "interval_minutes": interval_minutes,
        "task_registered": False,
        "task_started": False,
        "existing_task_overwritten": False,
        "stored_arguments_contain_shared_secret": False,
        "pacing_trial_automatically_enabled": False,
        "youtube_contacted": False,
        "firewall_modified": False,
        "ready": False,
    }
    if not apply or state != "LAN_TASK_ABSENT_OBSERVED":
        if apply:
            report["status"] = "LAN_MONITOR_TASK_APPLY_BLOCKED"
        return report
    if _task_state(task_name) != "LAN_TASK_ABSENT_OBSERVED":
        report["status"] = "LAN_MONITOR_TASK_APPLY_BLOCKED"
        report["task_state"] = "LAN_MONITOR_TASK_ABSENCE_UNVERIFIED"
        return report
    if not _INSTALL.is_file():
        raise ValueError("LAN_MONITOR_INSTALLER_UNAVAILABLE")
    exe = shutil.which("powershell.exe")
    if not exe:
        raise ValueError("LAN_MONITOR_INSTALLER_UNAVAILABLE")
    # Python passes literals as an argv array; installer independently
    # rejects dangerous quoting, untrusted executable and task collisions.
    args = [
        exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(_INSTALL), "-TaskName", task_name,
        "-HostIp", peer, "-SshUser", user,
        "-KnownHosts", str(known_hosts.absolute()),
        "-RemoteRepo", remote_repo, "-SshPort", str(ssh_port),
        "-CoordinatorPort", str(coordinator_port),
        "-IntervalMinutes", str(interval_minutes),
        "-EvidenceDb", str(evidence_db.absolute()),
        "-AlertFile", str(alert_file.absolute()),
    ]
    try:
        completed = subprocess.run(args, capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LAN_MONITOR_TASK_INSTALL_FAILED") from exc
    if completed.returncode != 0:
        raise ValueError("LAN_MONITOR_TASK_INSTALL_FAILED")
    report["status"] = "LAN_MONITOR_TASK_REGISTERED_NOT_STARTED"
    report["task_registered"] = True
    return report
