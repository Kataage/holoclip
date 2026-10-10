"""Bounded, safe-code-only LAN deployment/pacing history.

Live capture reuses existing signed, pinned-SSH diagnosis. Pacing is never run
unless BOTH --record and --include-pacing-trial are selected. Only fixed enum
codes and times enter durable SQLite; no raw result, identity, IP, credential,
SSH stderr or paths are stored. This is NOT machine/production attestation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_COORDINATOR_STATE, DEFAULT_SECRET_PATH,
)
from holoclip.source_ytdlp_lan_deployment_diagnosis import (
    _EXPECTED, _COMMANDS, _SSH_ERRORS, diagnose_lan_deployment,
)
from holoclip.source_ytdlp_lan_local_bootstrap import _safe_local_storage
from holoclip.source_ytdlp_lan_onboard_check import (
    _ALLOWED_CODES, _ALLOWED_STATUS, _CHECK_KEYS,
)
from holoclip.source_ytdlp_lan_pacing_trial import trial_lan_pacing

DEFAULT_EVIDENCE_DB = Path("data/runtime/yt-dlp-lan-evidence.sqlite3")
MAX_EVENTS = 90
_ALLOWED_DIAGNOSIS = frozenset({
    "LAN_DEPLOYMENT_REMOTE_UNVERIFIED",
    "LAN_DEPLOYMENT_OBSERVATIONS_MATCH",
    "LAN_DEPLOYMENT_INSPECTION_INCOMPLETE",
    "LAN_DEPLOYMENT_GAPS_OBSERVED",
})
_ALLOWED_TRIAL = frozenset({
    "NOT_RUN",
    "LAN_PACING_TRIAL_UNVERIFIED",
    "LAN_PACING_ONBOARDING_INCOMPLETE",
    "LAN_PACING_CLIENT_UNCONFIGURED",
    "LAN_PACING_IDENTITY_UNVERIFIED",
    "LAN_PACING_GATE_NOT_AVAILABLE",
    "LAN_PACING_PREFLIGHT_UNVERIFIED",
    "LAN_PACING_HELD_WITNESS_UNVERIFIED",
    "LAN_PACING_GAP_WITNESS_UNVERIFIED",
    "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED",
    "LAN_PACING_RELEASE_UNCONFIRMED",
})
_ALLOWED_SCOPE = frozenset({"PAIRED", "PC_A_ONLY", "NONE"})
_ALLOWED_ISSUES = frozenset({
    *(f"{role}:{check}" for role in _EXPECTED for check in _EXPECTED[role]),
    "pc-b:remote_ssh", "pair:signed_pair",
})
_ALLOWED_REMOTE = _SSH_ERRORS | {
    "LAN_DEPLOYMENT_REMOTE_REPORT_UNTRUSTED",
    "LAN_DEPLOYMENT_REMOTE_DIAGNOSIS_UNVERIFIED",
}
_ALLOWED_PAIR = {None, "LAN_PAIR_OBSERVATIONS_MATCH", "LAN_PAIR_OBSERVATIONS_INCONCLUSIVE"}
_SAFE_TRIAL_FLAGS = (
    "youtube_contacted", "media_downloaded", "video_process_started",
    "firewall_modified", "task_modified", "credentials_transferred",
    "ssh_fingerprint_independently_verified",
    "physical_two_host_identity_verified",
    "real_two_host_pacing_qualified", "two_host_verified", "ready",
)
_COLUMNS = ("id", "observed_at", "diagnosis", "scope", "issues", "trial")


def _path(path: Path) -> Path:
    try:
        db = _safe_local_storage(path)
        forbidden = {
            DEFAULT_COORDINATOR_STATE.absolute(), DEFAULT_SECRET_PATH.absolute(),
            DEFAULT_CLIENT_CONFIG.absolute(),
            DEFAULT_CLIENT_CONFIG.with_name(DEFAULT_CLIENT_CONFIG.name + ".required").absolute(),
        }
        if db in forbidden or db.suffix.lower() not in {".sqlite3", ".db"}:
            raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE")
        if any(db in x.parents or x in db.parents for x in forbidden):
            raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE")
        if not db.name or any(c in str(db) for c in ('"', "\r", "\n", "\x00")):
            raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE")
        if db.exists() and not db.is_file():
            raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE")
    except (ValueError, OSError, TypeError) as exc:
        raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE") from exc
    return db


def _diagnosis_event(report: dict) -> dict:
    if (
        not isinstance(report, dict)
        or report.get("status") not in _ALLOWED_DIAGNOSIS
        or report.get("ready") is not False
        or report.get("two_host_verified") is not False
        or report.get("firewall_effective_policy_verified") is not False
        or report.get("youtube_contacted") is not False
        or report.get("local_state_modified") is not False
        or report.get("lease_requested") is not False
    ):
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    a, b = report.get("pc_a"), report.get("pc_b")
    for role, doc in (("pc-a", a), ("pc-b", b)):
        if doc is None:
            continue
        if (
            not isinstance(doc, dict) or doc.get("role") != role
            or doc.get("status") not in _ALLOWED_STATUS
            or not isinstance(doc.get("checks"), dict)
            or set(doc["checks"]) != _CHECK_KEYS
            or any(type(value) is not str or value not in _ALLOWED_CODES
                   for value in doc["checks"].values())
        ):
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    pair = report.get("pair_observation")
    remote = report.get("remote_failure")
    if (
        pair not in _ALLOWED_PAIR
        or (remote is not None and remote not in _ALLOWED_REMOTE)
        or report.get("remote_ssh_report_validated") is not (b is not None)
        or (remote is not None and b is not None)
    ):
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    scope = "PAIRED" if a is not None and b is not None else (
        "PC_A_ONLY" if a is not None else "NONE"
    )
    if b is not None and a is None:
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    repairs = report.get("repair_plan")
    if not isinstance(repairs, list) or len(repairs) > len(_ALLOWED_ISSUES):
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    issues: set[str] = set()
    for entry in repairs:
        if not isinstance(entry, dict) or set(entry) != {
            "priority", "role", "check", "observed", "expected",
            "suggested_command", "automatically_modified",
        }:
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
        key = f"{entry['role']}:{entry['check']}"
        if (
            key not in _ALLOWED_ISSUES or key in issues
            or type(entry["priority"]) is not int
            or entry["priority"] != len(issues) + 1
            or entry["automatically_modified"] is not False
        ):
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
        if key == "pc-b:remote_ssh":
            valid = (
                remote is not None and entry["observed"] == remote
                and entry["expected"] == "PINNED_PUBLIC_KEY_SSH_REPORT_VALIDATED"
                and entry["suggested_command"] == "examples-ytdlp-lan-onboard-check"
            )
        elif key == "pair:signed_pair":
            valid = (
                scope == "PAIRED"
                and entry["observed"] == (pair or "LAN_PAIR_RECEIPT_UNAVAILABLE")
                and entry["expected"] == "LAN_PAIR_OBSERVATIONS_MATCH"
                and entry["suggested_command"] == "examples-ytdlp-lan-pair-collect"
            )
        else:
            role, check = key.split(":", 1)
            doc = a if role == "pc-a" else b
            valid = (
                doc is not None
                and entry["observed"] == doc["checks"][check]
                and entry["expected"] == _EXPECTED[role][check]
                and entry["suggested_command"] == _COMMANDS[(role, check)]
                and entry["observed"] != entry["expected"]
            )
        if not valid:
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
        issues.add(key)
    if report["status"] == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH":
        if (
            scope != "PAIRED" or issues
            or pair != "LAN_PAIR_OBSERVATIONS_MATCH"
            or remote is not None
            or any(doc["checks"][name] != expected
                   for role, doc in (("pc-a", a), ("pc-b", b))
                   for name, expected in _EXPECTED[role].items())
        ):
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    if report["status"] == "LAN_DEPLOYMENT_REMOTE_UNVERIFIED":
        if remote is None or scope == "PAIRED" or "pc-b:remote_ssh" not in issues:
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS")
    return {"diagnosis": report["status"], "scope": scope, "issues": sorted(issues)}


def _trial_status(report: dict | None) -> str:
    if report is None:
        return "NOT_RUN"
    if not isinstance(report, dict) or report.get("status") not in _ALLOWED_TRIAL - {"NOT_RUN"}:
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_TRIAL")
    if any(report.get(flag) is not False for flag in _SAFE_TRIAL_FLAGS):
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_TRIAL")
    for name in ("lease_requested", "lease_acquired", "lease_release_confirmed",
                 "possible_active_lease_until_expiry", "changes_may_be_partial"):
        if type(report.get(name)) is not bool:
            raise ValueError("LAN_EVIDENCE_UNTRUSTED_TRIAL")
    if report["status"] == "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED" and not (
        report["lease_acquired"] and report["lease_release_confirmed"]
        and report.get("regular_pacing_gap_created") is True
        and report.get("same_coordinator_state_observed") is True
        and report.get("held_witness") == "LAN_PACING_WITNESS_HELD_DENIED"
        and report.get("gap_witness") == "LAN_PACING_WITNESS_GAP_DENIED"
        and not report["possible_active_lease_until_expiry"]
    ):
        raise ValueError("LAN_EVIDENCE_UNTRUSTED_TRIAL")
    return report["status"]


def _row(row: tuple) -> dict:
    if len(row) != 6:
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
    rid, when, diag, scope, issues_json, trial = row
    try:
        issues = json.loads(issues_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT") from exc
    if (
        type(rid) is not int or rid < 1 or type(when) is not int or when < 0
        or diag not in _ALLOWED_DIAGNOSIS or scope not in _ALLOWED_SCOPE
        or trial not in _ALLOWED_TRIAL or not isinstance(issues, list)
        or any(type(k) is not str or k not in _ALLOWED_ISSUES for k in issues)
        or len(issues) != len(set(issues))
        or issues != sorted(issues)
    ):
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
    if diag == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH" and (scope != "PAIRED" or issues):
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
    return {
        "observed_at_unix": when, "diagnosis": diag,
        "scope": scope, "issue_keys": issues, "pacing_trial": trial,
    }


def _open(path: Path, *, readonly: bool) -> sqlite3.Connection | None:
    db = _path(path)
    if readonly:
        if not db.exists():
            return None
        uri = f"file:{quote(db.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        _path(db)  # Repeat symlink and path validation after parent creation.
        if not db.exists():
            try:
                descriptor = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # Another writer may have won the creation race.
            else:
                os.close(descriptor)
        _path(db)
        connection = sqlite3.connect(db, timeout=5)
    return connection


def _read_rows(conn: sqlite3.Connection) -> list[dict]:
    columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(lan_evidence)").fetchall())
    if columns != _COLUMNS:
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
    entries = conn.execute(
        "SELECT id, observed_at, diagnosis, scope, issues, trial "
        "FROM lan_evidence ORDER BY id DESC LIMIT ?", (MAX_EVENTS + 1,),
    ).fetchall()
    if len(entries) > MAX_EVENTS:
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
    return [_row(entry) for entry in reversed(entries)]


def read_lan_evidence_history(path: Path = DEFAULT_EVIDENCE_DB) -> dict:
    """Read only saved fixed codes. Never infer successful unattended uptime."""
    report: dict[str, object] = {
        "status": "LAN_EVIDENCE_HISTORY_EMPTY",
        "retained_observations": 0,
        "max_retained_observations": MAX_EVENTS,
        "recent_observations": [],
        "latest_observation": None,
        "previous_observation": None,
        "comparison": "UNKNOWN_INSUFFICIENT_EVIDENCE",
        "consecutive_incomplete_observations": 0,
        "repeated_issue_keys": [],
        "alert": "NONE",
        "calendar_uptime_verified": False,
        "physical_two_host_identity_verified": False,
        "real_two_host_pacing_qualified": False,
        "production_ready": False,
        "ready": False,
    }
    conn = _open(path, readonly=True)
    if conn is None:
        return report
    try:
        with conn:
            rows = _read_rows(conn)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT") from exc
    finally:
        conn.close()
    if not rows:
        return report
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    failure_streak = 0
    for item in reversed(rows):
        if item["diagnosis"] == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH":
            break
        failure_streak += 1
    repeated = []
    if len(rows) >= 3:
        last_three = rows[-3:]
        repeated = sorted(set.intersection(*(set(x["issue_keys"]) for x in last_three)))
    comparison = "UNKNOWN_INSUFFICIENT_EVIDENCE"
    if previous is not None:
        if previous["scope"] != latest["scope"]:
            comparison = "INCOMPARABLE_OBSERVATION_SCOPE"
        elif latest["issue_keys"] == previous["issue_keys"] and (
            latest["diagnosis"] == previous["diagnosis"]
        ):
            comparison = "NO_CHANGE_OBSERVED"
        else:
            new_keys, old_keys = set(latest["issue_keys"]), set(previous["issue_keys"])
            comparison = (
                "FEWER_ISSUES_OBSERVED" if new_keys < old_keys
                else "MORE_ISSUES_OBSERVED" if new_keys > old_keys
                else "MIXED_CHANGE_OBSERVED"
            )
    report.update(
        status="LAN_EVIDENCE_HISTORY_RECORDED",
        retained_observations=len(rows),
        recent_observations=rows[-10:],
        latest_observation=latest,
        previous_observation=previous,
        comparison=comparison,
        consecutive_incomplete_observations=failure_streak,
        repeated_issue_keys=repeated,
        alert=(
            "REPEATED_SAME_LAN_DEFICITS" if repeated else
            "REPEATED_INCOMPLETE_LAN_DIAGNOSIS" if failure_streak >= 3
            else "NONE"
        ),
    )
    return report


def record_lan_evidence(
    *, path: Path, diagnosis: dict, trial: dict | None = None,
) -> dict:
    """Insert one sanitized snapshot with a bounded retention window."""
    event = _diagnosis_event(diagnosis)
    event["trial"] = _trial_status(trial)
    conn = _open(path, readonly=False)
    if conn is None:
        raise ValueError("LAN_EVIDENCE_UNSAFE_STORAGE")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lan_evidence ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "observed_at INTEGER NOT NULL, diagnosis TEXT NOT NULL,"
                "scope TEXT NOT NULL, issues TEXT NOT NULL, trial TEXT NOT NULL)"
            )
            # Reject unexpected existing/corrupt schema BEFORE writing.
            columns = tuple(
                row[1] for row in conn.execute("PRAGMA table_info(lan_evidence)")
            )
            if columns != _COLUMNS:
                raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
            # Refuse to append when previously persisted codes were changed.
            old = conn.execute(
                "SELECT id, observed_at, diagnosis, scope, issues, trial "
                "FROM lan_evidence ORDER BY id DESC LIMIT ?", (MAX_EVENTS + 1,),
            ).fetchall()
            for row in old:
                _row(row)
            if len(old) > MAX_EVENTS:
                raise ValueError("LAN_EVIDENCE_HISTORY_CORRUPT")
            conn.execute(
                "INSERT INTO lan_evidence(observed_at,diagnosis,scope,issues,trial)"
                " VALUES(?,?,?,?,?)",
                (int(time.time()), event["diagnosis"], event["scope"],
                 json.dumps(event["issues"], separators=(",", ":")), event["trial"]),
            )
            conn.execute(
                "DELETE FROM lan_evidence WHERE id NOT IN "
                "(SELECT id FROM lan_evidence ORDER BY id DESC LIMIT ?)",
                (MAX_EVENTS,),
            )
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise ValueError("LAN_EVIDENCE_RECORD_FAILED") from exc
    finally:
        conn.close()
    return read_lan_evidence_history(path)


def capture_lan_evidence(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    evidence_db: Path = DEFAULT_EVIDENCE_DB, ssh_port: int = 22,
    coordinator_port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
    record: bool = False, include_pacing_trial: bool = False,
) -> dict:
    """One read-only diagnosis; optional explicit record and media-free trial."""
    if include_pacing_trial and not record:
        raise ValueError("LAN_EVIDENCE_TRIAL_REQUIRES_RECORD")
    # Validate destination before any SSH or lease when recording is requested.
    if record:
        _path(evidence_db)
        # Detect corrupted or mismatched history before optional live
        # coordinator lease and before any SSH-based network observation.
        read_lan_evidence_history(evidence_db)
    arguments = {
        "host": host, "user": user, "known_hosts": known_hosts,
        "remote_repo": remote_repo, "ssh_port": ssh_port,
        "coordinator_port": coordinator_port, "task_name": task_name,
    }
    diagnosis = diagnose_lan_deployment(**arguments)
    sanitized = _diagnosis_event(diagnosis)
    trial: dict | None = None
    trial_skipped = False
    if include_pacing_trial:
        if sanitized["diagnosis"] == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH":
            trial = trial_lan_pacing(**arguments, apply=True)
            _trial_status(trial)
        else:
            trial_skipped = True
    history = record_lan_evidence(path=evidence_db, diagnosis=diagnosis, trial=trial) if record else None
    trial_code = _trial_status(trial)
    return {
        "status": "LAN_EVIDENCE_RECORDED" if record else "LAN_EVIDENCE_PREVIEW_ONLY",
        "diagnosis": sanitized["diagnosis"],
        "observation_scope": sanitized["scope"],
        "issue_keys": sanitized["issues"],
        "pacing_trial": trial_code,
        "pacing_trial_skipped_for_incomplete_setup": trial_skipped,
        "history": history,
        "database_written": record,
        "lease_trial_requested": include_pacing_trial and not trial_skipped,
        "youtube_contacted": False,
        "credentials_saved": False,
        "physical_two_host_identity_verified": False,
        "real_two_host_pacing_qualified": False,
        "production_ready": False,
        "ready": False,
    }
