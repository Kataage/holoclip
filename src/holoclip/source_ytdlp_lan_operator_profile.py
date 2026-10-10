"""PC-A non-secret operator profile and preview-first monitor enrollment.

A single explicitly confirmed setup can store reusable LAN arguments and
optionally register the preexisting safe monitor task. Never stores shared
credentials or host-key bytes, never edits SSH trust, starts tasks, leases
the coordinator, or accesses YouTube. Existing tasks are not overwritten.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_COORDINATOR_STATE, DEFAULT_SECRET_PATH,
    _private_ipv4, _reject_linked_path,
)
from holoclip.source_ytdlp_lan_evidence_history import (
    DEFAULT_EVIDENCE_DB, _path as _evidence_path,
    read_lan_evidence_history,
)
from holoclip.source_ytdlp_lan_local_bootstrap import _safe_local_storage
from holoclip.source_ytdlp_lan_monitor import (
    DEFAULT_MONITOR_ALERT, _alert_path, prepare_lan_monitor_task,
    read_lan_monitor_alert,
)
from holoclip.source_ytdlp_lan_pair_collect import (
    _pinned_ssh_command, _windows_remote_repo,
)
from holoclip.source_ytdlp_lan_setup_doctor import _host_pin_state
from holoclip.source_ytdlp_lan_ssh_transfer import _SSH_USER, _secure_windows_path
from holoclip.source_ytdlp_lan_task_prepare import _TASK_NAME, _task_state, _windows

DEFAULT_OPERATOR_PROFILE = Path("data/runtime/yt-dlp-lan-operator.json")
_VERSION = "holoclip-lan-operator-v1"
_ARGS = (
    "host", "user", "known_hosts", "remote_repo", "ssh_port",
    "coordinator_port", "interval_minutes", "task_name",
    "evidence_db", "alert_file",
)
_FIXED_FIELDS = frozenset((*_ARGS, "version", "ready"))
_FORBIDDEN = (
    DEFAULT_SECRET_PATH, DEFAULT_CLIENT_CONFIG, DEFAULT_COORDINATOR_STATE,
    DEFAULT_CLIENT_CONFIG.with_name(DEFAULT_CLIENT_CONFIG.name + ".required"),
)


def _operator_path(path: Path) -> Path:
    try:
        result = _safe_local_storage(path)
        if (
            result.suffix.lower() != ".json"
            or result in (p.absolute() for p in _FORBIDDEN)
            or any(result in p.absolute().parents or p.absolute() in result.parents
                   for p in _FORBIDDEN)
            or any(ch in str(result) for ch in ('"', "\r", "\n", "\x00"))
        ):
            raise ValueError("LAN_OPERATOR_UNSAFE_PROFILE_PATH")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("LAN_OPERATOR_UNSAFE_PROFILE_PATH") from exc
    return result


def _validated_config(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int, coordinator_port: int, interval_minutes: int,
    task_name: str, evidence_db: Path, alert_file: Path,
    profile_file: Path,
) -> dict:
    try:
        ip = _private_ipv4(host)
        if ip != host or ip.startswith("127."):
            raise ValueError("LAN_OPERATOR_UNSAFE_PC_B")
    except (ValueError, TypeError) as exc:
        raise ValueError("LAN_OPERATOR_UNSAFE_PC_B") from exc
    if not isinstance(user, str) or not _SSH_USER.fullmatch(user):
        raise ValueError("LAN_OPERATOR_UNSAFE_SSH_USER")
    repo = _windows_remote_repo(remote_repo)
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError("LAN_OPERATOR_INVALID_SSH_PORT")
    if type(coordinator_port) is not int or not 1024 <= coordinator_port <= 65535:
        raise ValueError("LAN_OPERATOR_INVALID_COORDINATOR_PORT")
    if type(interval_minutes) is not int or not 15 <= interval_minutes <= 1440:
        raise ValueError("LAN_OPERATOR_INVALID_MONITOR_INTERVAL")
    if not isinstance(task_name, str) or not _TASK_NAME.fullmatch(task_name):
        raise ValueError("LAN_OPERATOR_INVALID_TASK_NAME")
    profile = _operator_path(profile_file)
    db = _evidence_path(evidence_db)
    alert = _alert_path(alert_file, db)
    pinned = known_hosts.absolute()
    _reject_linked_path(pinned)
    if (
        len({pinned, profile, db, alert}) != 4
        or any(a in b.parents for a in (pinned, profile, db, alert)
               for b in (pinned, profile, db, alert) if a != b)
        or any(c in str(pinned) for c in ('"', "\r", "\n", "\x00"))
    ):
        raise ValueError("LAN_OPERATOR_OVERLAPPING_PATHS")
    return {
        "version": _VERSION,
        "host": ip, "user": user, "known_hosts": str(pinned),
        "remote_repo": repo,
        "ssh_port": ssh_port, "coordinator_port": coordinator_port,
        "interval_minutes": interval_minutes, "task_name": task_name,
        "evidence_db": str(db), "alert_file": str(alert),
        "ready": False,
    }


def _read(profile: Path) -> dict | None:
    if not profile.exists():
        return None
    if not profile.is_file() or not 1 <= profile.stat().st_size <= 4096:
        raise ValueError("LAN_OPERATOR_PROFILE_CORRUPT")
    try:
        parsed = json.loads(profile.read_text(encoding="utf-8"))
        if (
            not isinstance(parsed, dict)
            or set(parsed) != _FIXED_FIELDS
            or parsed.get("version") != _VERSION
            or parsed.get("ready") is not False
        ):
            raise ValueError("LAN_OPERATOR_PROFILE_CORRUPT")
        verified = _validated_config(
            **{key: Path(parsed[key]) if key in {
                "known_hosts", "evidence_db", "alert_file",
            } else parsed[key] for key in _ARGS},
            profile_file=profile,
        )
        if parsed != verified:
            raise ValueError("LAN_OPERATOR_PROFILE_CORRUPT")
    except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
        raise ValueError("LAN_OPERATOR_PROFILE_CORRUPT") from exc
    return verified


def read_operator_profile(profile_file: Path = DEFAULT_OPERATOR_PROFILE) -> dict | None:
    """Strict offline load: do not expose private profile fields in reports."""
    return _read(_operator_path(profile_file))


def _kwargs(doc: dict) -> dict:
    return {
        "host": doc["host"], "user": doc["user"],
        "known_hosts": Path(doc["known_hosts"]),
        "remote_repo": doc["remote_repo"],
        "ssh_port": doc["ssh_port"],
        "coordinator_port": doc["coordinator_port"],
        "interval_minutes": doc["interval_minutes"],
        "task_name": doc["task_name"],
        "evidence_db": Path(doc["evidence_db"]),
        "alert_file": Path(doc["alert_file"]),
    }


def _saved(profile: Path, new: dict, *, update: bool) -> str:
    old = _read(profile)
    if old is None:
        return "LAN_OPERATOR_PROFILE_ABSENT"
    if old == new:
        return "LAN_OPERATOR_PROFILE_MATCHES"
    if not update:
        return "LAN_OPERATOR_PROFILE_DIFFERENT"
    # Never silently orphan an existing Windows scheduled task with stale
    # pinned endpoint, credentials-path or interval arguments.
    if not _windows():
        return "LAN_OPERATOR_WINDOWS_REQUIRED"
    for name in {old["task_name"], new["task_name"]}:
        if _task_state(name) != "LAN_TASK_ABSENT_OBSERVED":
            return "LAN_OPERATOR_EXISTING_MONITOR_TASK_BLOCKS_UPDATE"
    return "LAN_OPERATOR_PROFILE_UPDATE_ELIGIBLE"


def _write(profile: Path, doc: dict, *, update: bool) -> None:
    body = (json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")
    if len(body) > 4096:
        raise ValueError("LAN_OPERATOR_PROFILE_TOO_LARGE")
    profile.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(profile)
    fd, tmp = tempfile.mkstemp(prefix=".operator-", dir=profile.parent)
    temporary = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as writer:
            writer.write(body)
        # ACL hardening is checked before committing the new profile.
        if _secure_windows_path(temporary, apply=True) is not True:
            raise ValueError("LAN_OPERATOR_PROFILE_ACL_UNVERIFIED")
        _reject_linked_path(profile)
        if update:
            os.replace(temporary, profile)
        else:
            # Create-only semantics even against races; no overwrite by default.
            os.link(temporary, profile)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def prepare_operator_profile(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    interval_minutes: int = 60,
    task_name: str = "Holoclip yt-dlp LAN Monitor",
    evidence_db: Path = DEFAULT_EVIDENCE_DB,
    alert_file: Path = DEFAULT_MONITOR_ALERT,
    profile_file: Path = DEFAULT_OPERATOR_PROFILE,
    apply: bool = False, update: bool = False,
    register_monitor: bool = False,
) -> dict[str, object]:
    """Safe one-command config preview; optional save and monitor registration."""
    if update and not apply:
        raise ValueError("LAN_OPERATOR_UPDATE_REQUIRES_APPLY")
    if register_monitor and not apply:
        raise ValueError("LAN_OPERATOR_REGISTER_REQUIRES_APPLY")
    profile = _operator_path(profile_file)
    doc = _validated_config(
        host=host, user=user, known_hosts=known_hosts, remote_repo=remote_repo,
        ssh_port=ssh_port, coordinator_port=coordinator_port,
        interval_minutes=interval_minutes, task_name=task_name,
        evidence_db=evidence_db, alert_file=alert_file, profile_file=profile,
    )
    # These are *local*, no-network pin/command checks only.
    _pinned_ssh_command(
        host=doc["host"], user=doc["user"],
        known_hosts=Path(doc["known_hosts"]),
        remote_repo=doc["remote_repo"], port=doc["ssh_port"],
        remote_probe="onboard",
    )
    if _host_pin_state(host, Path(doc["known_hosts"]), ssh_port) != "HOST_PIN_ENTRY_OBSERVED":
        raise ValueError("LAN_OPERATOR_PINNED_HOST_KEY_REQUIRED")
    state = _saved(profile, doc, update=update)
    report = {
        "status": "LAN_OPERATOR_PREVIEW_BLOCKED",
        "profile": state,
        "monitor": "NOT_REQUESTED",
        "profile_saved": False, "monitor_task_registered": False,
        "monitor_task_started": False,
        "changes_may_be_partial": False,
        "known_hosts_entry_observed": True,
        "ssh_fingerprint_independently_verified": False,
        "credentials_stored_in_profile": False,
        "youtube_contacted": False, "lease_requested": False,
        "two_host_verified": False, "ready": False,
    }
    if state not in {
        "LAN_OPERATOR_PROFILE_ABSENT", "LAN_OPERATOR_PROFILE_MATCHES",
        "LAN_OPERATOR_PROFILE_UPDATE_ELIGIBLE",
    }:
        return report
    if register_monitor:
        # Full secondary preflight BEFORE any profile mutation.
        preview = prepare_lan_monitor_task(**_kwargs(doc), apply=False)
        report["monitor"] = str(preview["status"])
        if preview["status"] != "LAN_MONITOR_TASK_PREVIEW_ELIGIBLE":
            return report
    if not apply:
        report["status"] = "LAN_OPERATOR_PREVIEW_ELIGIBLE"
        return report
    if not _windows():
        report["status"] = "LAN_OPERATOR_WINDOWS_REQUIRED"
        return report
    report["status"] = "LAN_OPERATOR_APPLY_INCOMPLETE"
    try:
        if state != "LAN_OPERATOR_PROFILE_MATCHES":
            _write(profile, doc, update=state == "LAN_OPERATOR_PROFILE_UPDATE_ELIGIBLE")
            report["profile_saved"] = True
        report["profile"] = "LAN_OPERATOR_PROFILE_SAVED"
        if register_monitor:
            report["changes_may_be_partial"] = True
            result = prepare_lan_monitor_task(**_kwargs(doc), apply=True)
            report["monitor"] = str(result["status"])
            if result["status"] != "LAN_MONITOR_TASK_REGISTERED_NOT_STARTED":
                return report
            report["monitor_task_registered"] = True
        report["changes_may_be_partial"] = False
        report["status"] = "LAN_OPERATOR_PREPARED_NOT_STARTED"
    except (OSError, ValueError, TypeError, UnicodeError):
        report["status"] = "LAN_OPERATOR_APPLY_UNCONFIRMED"
        report["changes_may_be_partial"] = True
    return report


def operator_status(
    *, profile_file: Path = DEFAULT_OPERATOR_PROFILE,
    diagnose: bool = False,
    register_monitor: bool = False,
) -> dict[str, object]:
    """No arguments repeated. Default offline; opt-in signed diagnosis/monitor."""
    if diagnose and register_monitor:
        raise ValueError("LAN_OPERATOR_ONE_ACTION_AT_A_TIME")
    doc = read_operator_profile(profile_file)
    base: dict[str, object] = {
        "status": "LAN_OPERATOR_PROFILE_MISSING",
        "profile_validated": doc is not None,
        "monitor": "NOT_INSPECTED",
        "diagnosis": "NOT_REQUESTED",
        "history": "NOT_OBSERVED", "alert": "NOT_OBSERVED",
        "monitor_task_registered": False,
        "lease_requested": False, "youtube_contacted": False,
        "credentials_disclosed": False, "two_host_verified": False,
        "ready": False,
    }
    if doc is None:
        return base
    args = _kwargs(doc)
    from holoclip.source_ytdlp_lan_deployment_diagnosis import diagnose_lan_deployment
    base["history"] = read_lan_evidence_history(args["evidence_db"])["status"]
    base["alert"] = read_lan_monitor_alert(
        evidence_db=args["evidence_db"], alert_file=args["alert_file"],
    )["alert"]
    if diagnose:
        diag = diagnose_lan_deployment(**{
            key: val for key, val in args.items() if key not in {
                "interval_minutes", "task_name", "evidence_db", "alert_file",
            }
        })
        base["diagnosis"] = diag["status"]
        base["status"] = "LAN_OPERATOR_DIAGNOSIS_OBSERVED"
    elif register_monitor:
        result = prepare_lan_monitor_task(**args, apply=True)
        base["monitor"] = result["status"]
        base["monitor_task_registered"] = result["task_registered"] is True
        base["status"] = (
            "LAN_OPERATOR_MONITOR_REGISTERED_NOT_STARTED"
            if base["monitor_task_registered"] else "LAN_OPERATOR_MONITOR_BLOCKED"
        )
    else:
        # Enumeration is local only and never changes the task.
        base["monitor"] = (
            _task_state(args["task_name"]) if _windows()
            else "LAN_MONITOR_WINDOWS_REQUIRED"
        )
        base["status"] = "LAN_OPERATOR_LOCAL_STATUS"
    return base
