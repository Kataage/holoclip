"""First-run PC-A LAN monitor verification, fully read-only by default.

Separates exact Task Scheduler action/schedule inspection, last scheduler result,
timestamp freshness, and signed peer diagnosis. A successful task exit is NOT
proof of an authenticated PC-B observation or continuous scheduling.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from holoclip.source_ytdlp_lan_evidence_history import read_lan_evidence_history
from holoclip.source_ytdlp_lan_monitor import read_lan_monitor_alert
from holoclip.source_ytdlp_lan_operator_profile import (
    DEFAULT_OPERATOR_PROFILE, _kwargs, read_operator_profile,
)
from holoclip.source_ytdlp_lan_task_prepare import _windows

_ROOT = Path(__file__).resolve().parents[2]
_PROBE = _ROOT / "scripts" / "probe-ytdlp-lan-monitor-first-run.ps1"
_TASK_CODES = frozenset({
    "LAN_FIRST_RUN_TASK_ABSENT",
    "LAN_FIRST_RUN_TASK_DISABLED",
    "LAN_FIRST_RUN_TASK_CONFIG_MISMATCH",
    "LAN_FIRST_RUN_TASK_CONFIG_MATCHED",
    "LAN_FIRST_RUN_TASK_STATE_UNVERIFIED",
    "LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE",
})
_EXECUTION_CODES = frozenset({
    "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED",
    "LAN_FIRST_RUN_LAST_RUN_FAILED",
    "LAN_FIRST_RUN_NEVER_RUN",
    "LAN_FIRST_RUN_LAST_RUN_UNVERIFIED",
})
_HISTORY_CODES = frozenset({
    "LAN_FIRST_RUN_HISTORY_MISSING",
    "LAN_FIRST_RUN_HISTORY_STALE",
    "LAN_FIRST_RUN_HISTORY_CLOCK_UNTRUSTED",
    "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE",
    "LAN_FIRST_RUN_HISTORY_FRESH_NEGATIVE",
    "LAN_FIRST_RUN_HISTORY_UNTRUSTED",
})
_NEXT = {
    "profile": "examples-ytdlp-lan-operator-setup",
    "task": "examples-ytdlp-lan-operator-status --register-monitor",
    "task_config": "INSPECT_EXISTING_TASK_IN_WINDOWS_TASK_SCHEDULER",
    "run": "INSPECT_MONITOR_TASK_LAST_RUN_AND_LOCAL_WINDOWS_SESSION",
    "history": "examples-ytdlp-lan-evidence-history",
    "peer": "examples-ytdlp-lan-operator-status --diagnose",
    "alert": "examples-ytdlp-lan-monitor-alert",
}


def _task_observation(doc: dict) -> dict:
    """Never trust raw PowerShell output or include errors in a user report."""
    base = {
        "status": "LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE",
        "task_execution": "LAN_FIRST_RUN_LAST_RUN_UNVERIFIED",
        "last_run_unix": None,
    }
    if not _windows() or not _PROBE.is_file():
        return base
    exe = shutil.which("powershell.exe")
    if not exe:
        return base
    args = [
        exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(_PROBE),
        "-TaskName", doc["task_name"],
        "-HostIp", doc["host"], "-SshUser", doc["user"],
        "-KnownHosts", doc["known_hosts"], "-RemoteRepo", doc["remote_repo"],
        "-SshPort", str(doc["ssh_port"]),
        "-CoordinatorPort", str(doc["coordinator_port"]),
        "-IntervalMinutes", str(doc["interval_minutes"]),
        "-EvidenceDb", doc["evidence_db"], "-AlertFile", doc["alert_file"],
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=30, check=False,
        )
        if result.returncode not in (0, 6) or not 1 <= len(result.stdout) <= 512:
            return base
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or set(response) != {
            "status", "task_execution", "last_run_unix", "ready",
        } or response["ready"] is not False:
            return base
        status = response["status"]
        execution = response["task_execution"]
        when = response["last_run_unix"]
        if status not in _TASK_CODES or execution not in _EXECUTION_CODES:
            return base
        if when is not None and (
            type(when) is not int or not 0 <= when <= 253402300799
        ):
            return base
        if (execution in {
            "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED",
            "LAN_FIRST_RUN_LAST_RUN_FAILED",
        }) is not (when is not None):
            return base
        if status != "LAN_FIRST_RUN_TASK_CONFIG_MATCHED" and execution not in {
            "LAN_FIRST_RUN_NEVER_RUN", "LAN_FIRST_RUN_LAST_RUN_UNVERIFIED",
        }:
            return base
        if status == "LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE":
            return base
        if (result.returncode != 0):
            return base
        return {"status": status, "task_execution": execution, "last_run_unix": when}
    except (OSError, UnicodeError, ValueError, TypeError, subprocess.TimeoutExpired):
        return base


def _history_observation(history: dict, *, now: int, interval_minutes: int) -> tuple[str, int | None]:
    if history.get("status") != "LAN_EVIDENCE_HISTORY_RECORDED":
        return "LAN_FIRST_RUN_HISTORY_MISSING", None
    latest = history.get("latest_observation")
    if not isinstance(latest, dict):
        return "LAN_FIRST_RUN_HISTORY_UNTRUSTED", None
    when = latest.get("observed_at_unix")
    if type(when) is not int or not 0 <= when <= 253402300799:
        return "LAN_FIRST_RUN_HISTORY_UNTRUSTED", None
    age = now - when
    if age < -120:
        return "LAN_FIRST_RUN_HISTORY_CLOCK_UNTRUSTED", when
    # Two cycles plus a small allowance: this is only a freshness bound,
    # not proof the Windows task has continuously run.
    if age > 2 * interval_minutes * 60 + 300:
        return "LAN_FIRST_RUN_HISTORY_STALE", when
    if (latest.get("diagnosis") == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
            and latest.get("scope") == "PAIRED"
            and latest.get("issue_keys") == []):
        return "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE", when
    return "LAN_FIRST_RUN_HISTORY_FRESH_NEGATIVE", when


def inspect_lan_first_run(
    *, profile_file: Path = DEFAULT_OPERATOR_PROFILE,
    check_remote: bool = False,
) -> dict[str, object]:
    """Inspect startup readiness signals. No writes, yt-dlp or lease RPCs."""
    report: dict[str, object] = {
        "status": "LAN_FIRST_RUN_PROFILE_MISSING",
        "profile": "NOT_CONFIGURED",
        "task": "NOT_INSPECTED",
        "task_execution": "NOT_INSPECTED",
        "history": "NOT_INSPECTED",
        "alert": "NOT_INSPECTED",
        "signed_peer": "NOT_REQUESTED",
        "scheduler_and_history_consistent": False,
        "local_monitor_observations_current": False,
        "next_actions": [],
        "commands_run": ["READ_ONLY_LOCAL_INSPECTION"],
        "changes_applied": False,
        "lease_requested": False,
        "ssh_trust_modified": False,
        "youtube_contacted": False,
        "physical_two_host_attested": False,
        "continuous_monitoring_verified": False,
        "production_ready": False,
        "ready": False,
    }
    try:
        doc = read_operator_profile(profile_file)
    except (OSError, ValueError, UnicodeError, TypeError):
        report["status"] = "LAN_FIRST_RUN_PROFILE_UNTRUSTED"
        report["profile"] = "INVALID"
        report["next_actions"] = [_NEXT["profile"]]
        return report
    if doc is None:
        report["next_actions"] = [_NEXT["profile"]]
        return report
    report["profile"] = "VALIDATED"
    args = _kwargs(doc)
    task = _task_observation(doc)
    report["task"] = task["status"]
    report["task_execution"] = task["task_execution"]
    try:
        hist = read_lan_evidence_history(args["evidence_db"])
        hist_code, hist_time = _history_observation(
            hist, now=int(time.time()), interval_minutes=doc["interval_minutes"],
        )
    except (OSError, ValueError, TypeError):
        hist_code, hist_time = "LAN_FIRST_RUN_HISTORY_UNTRUSTED", None
    report["history"] = hist_code
    try:
        alert = read_lan_monitor_alert(
            evidence_db=args["evidence_db"], alert_file=args["alert_file"],
        )
        report["alert"] = (
            alert["alert"]
            if alert.get("status") in {
                "LAN_MONITOR_ALERT_PRESENT", "LAN_MONITOR_NO_ALERT_RECORDED",
                "LAN_MONITOR_NEVER_RECORDED",
            } and alert.get("alert") in {
                "NONE", "REPEATED_SAME_LAN_DEFICITS",
                "REPEATED_INCOMPLETE_LAN_DIAGNOSIS",
            }
            else "LAN_FIRST_RUN_ALERT_UNTRUSTED"
        )
    except (OSError, ValueError, TypeError, UnicodeError):
        report["alert"] = "LAN_FIRST_RUN_ALERT_UNTRUSTED"
    last_run = task["last_run_unix"]
    correlated = (
        task["status"] == "LAN_FIRST_RUN_TASK_CONFIG_MATCHED"
        and task["task_execution"] == "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED"
        and isinstance(last_run, int)
        and isinstance(hist_time, int)
        and -30 <= hist_time - last_run <= 300
        and hist_code in {
            "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE",
            "LAN_FIRST_RUN_HISTORY_FRESH_NEGATIVE",
        }
    )
    report["scheduler_and_history_consistent"] = correlated
    report["local_monitor_observations_current"] = (
        correlated
        and hist_code == "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE"
        and report["alert"] == "NONE"
    )
    if task["status"] == "LAN_FIRST_RUN_TASK_ABSENT":
        report["next_actions"].append(_NEXT["task"])
    elif task["status"] != "LAN_FIRST_RUN_TASK_CONFIG_MATCHED":
        report["next_actions"].append(_NEXT["task_config"])
    if task["task_execution"] != "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED":
        report["next_actions"].append(_NEXT["run"])
    if hist_code != "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE" or not correlated:
        report["next_actions"].append(_NEXT["history"])
    if report["alert"] != "NONE":
        report["next_actions"].append(_NEXT["alert"])
    if check_remote:
        report["commands_run"].append("PINNED_SIGNED_PC_B_DIAGNOSIS")
        from holoclip.source_ytdlp_lan_deployment_diagnosis import diagnose_lan_deployment
        try:
            remote = diagnose_lan_deployment(
                host=doc["host"], user=doc["user"],
                known_hosts=Path(doc["known_hosts"]),
                remote_repo=doc["remote_repo"],
                ssh_port=doc["ssh_port"],
                coordinator_port=doc["coordinator_port"],
            )
            report["signed_peer"] = (
                "LAN_FIRST_RUN_SIGNED_PEER_OBSERVED"
                if remote.get("status") == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
                and remote.get("ready") is False
                else "LAN_FIRST_RUN_SIGNED_PEER_UNVERIFIED"
            )
        except (OSError, ValueError, UnicodeError, TypeError):
            report["signed_peer"] = "LAN_FIRST_RUN_SIGNED_PEER_UNVERIFIED"
        if report["signed_peer"] != "LAN_FIRST_RUN_SIGNED_PEER_OBSERVED":
            report["next_actions"].append(_NEXT["peer"])
    report["next_actions"] = list(dict.fromkeys(report["next_actions"]))
    if report["local_monitor_observations_current"]:
        report["status"] = (
            "LAN_FIRST_RUN_LOCAL_AND_PEER_OBSERVED"
            if report["signed_peer"] == "LAN_FIRST_RUN_SIGNED_PEER_OBSERVED"
            else "LAN_FIRST_RUN_LOCAL_MONITOR_OBSERVED"
        )
    else:
        report["status"] = "LAN_FIRST_RUN_ACTION_REQUIRED"
    return report
