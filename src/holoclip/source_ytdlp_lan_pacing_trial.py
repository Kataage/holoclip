"""Explicit no-media two-node LAN lease/gap trial through pre-pinned SSH.

Only a user-selected live run touches the *real* shared coordinator gate.
It creates a temporary lease and the regular 20s pacing gap, but never
invokes yt-dlp, queries YouTube, modifies SSH trust or local Windows setup.
It is operational evidence, NOT physical identity/production qualification.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import LanCoordinatorClient
from holoclip.source_ytdlp_lan_onboard_check import check_lan_onboarding
from holoclip.source_ytdlp_lan_pair_collect import _pinned_ssh_command
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

_MAX_REMOTE_BYTES = 2048
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_WITNESS_CODES = {
    "held": "LAN_PACING_WITNESS_HELD_DENIED",
    "gap": "LAN_PACING_WITNESS_GAP_DENIED",
}


def _base_report(*, applied: bool) -> dict[str, object]:
    return {
        "status": "LAN_PACING_PREVIEW_ONLY" if not applied else "LAN_PACING_TRIAL_UNVERIFIED",
        "preflight": "NOT_EXECUTED",
        "initial_gate": "NOT_OBSERVED",
        "held_witness": "NOT_OBSERVED",
        "gap_witness": "NOT_OBSERVED",
        "same_coordinator_state_observed": False,
        "lease_requested": False,
        "lease_acquired": False,
        "lease_release_confirmed": False,
        "regular_pacing_gap_created": False,
        "possible_active_lease_until_expiry": False,
        "changes_may_be_partial": False,
        "ssh_fingerprint_independently_verified": False,
        "physical_two_host_identity_verified": False,
        "real_two_host_pacing_qualified": False,
        "youtube_contacted": False,
        "media_downloaded": False,
        "video_process_started": False,
        "firewall_modified": False,
        "task_modified": False,
        "credentials_transferred": False,
        "two_host_verified": False,
        "ready": False,
    }


def witness_lan_pacing(*, phase: str) -> dict[str, object]:
    """PC-B only: signed status then denied reservation, with fixed codes."""
    if phase not in _WITNESS_CODES:
        raise ValueError("LAN_PACING_INVALID_PHASE")
    report: dict[str, object] = {
        "phase": phase,
        "status": "LAN_PACING_WITNESS_UNVERIFIED",
        "coordinator_instance_id": None,
        "lease_access_deferred": False,
        "youtube_contacted": False,
        "video_process_started": False,
        "ready": False,
    }
    try:
        client = LanCoordinatorClient.from_env()
        if client is None:
            return report
        state = client.status()
        expected_gate = "ACTIVE" if phase == "held" else "WAIT_GAP"
        if state.get("ok") is not True or state.get("code") != expected_gate:
            return report
        instance = state.get("coordinator_instance_id")
        if not isinstance(instance, str) or not _SHA256.fullmatch(instance):
            return report
        report["coordinator_instance_id"] = instance
        try:
            # A granted reservation would reveal a serious isolation fault.
            # The context manager then releases it (no media is ever launched).
            with client.reserve(timeout_seconds=5):
                report["status"] = "LAN_PACING_WITNESS_UNEXPECTED_LEASE"
                return report
        except LocalYtDlpDeferred as exc:
            if str(exc) != "LOCAL_YTDLP_COORDINATOR_ACCESS_DEFERRED":
                return report
        report["lease_access_deferred"] = True
        report["status"] = _WITNESS_CODES[phase]
    except (LocalYtDlpDeferred, ValueError, OSError, UnicodeError):
        return report
    return report


def _remote_witness(command: list[str], *, phase: str, identity: str) -> bool:
    """Treat a remote receipt as untrusted, even through pinned SSH."""
    try:
        proc = subprocess.run(command, capture_output=True, timeout=30, check=False)
        if proc.returncode != 0 or not 1 <= len(proc.stdout) <= _MAX_REMOTE_BYTES:
            return False
        doc = json.loads(proc.stdout)
        return (
            isinstance(doc, dict)
            and set(doc) == {
                "phase", "status", "coordinator_instance_id",
                "lease_access_deferred", "youtube_contacted",
                "video_process_started", "ready",
            }
            and doc.get("phase") == phase
            and doc.get("status") == _WITNESS_CODES[phase]
            and doc.get("coordinator_instance_id") == identity
            and doc.get("lease_access_deferred") is True
            and doc.get("youtube_contacted") is False
            and doc.get("video_process_started") is False
            and doc.get("ready") is False
        )
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return False


def trial_lan_pacing(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
    apply: bool = False,
) -> dict[str, object]:
    """Read-only preview by default; explicit test reserves a real lease.

    Test may introduce a normal 20-second central gap. If release is lost,
    lease stays held until its bounded deadline; never erase gate state.
    """
    # Validate both pinned remote command variants even during preview.
    commands = {
        phase: _pinned_ssh_command(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, port=ssh_port,
            remote_probe="pacing-" + phase,
        )
        for phase in ("held", "gap")
    }
    report = _base_report(applied=apply)
    if not apply:
        return report

    # No new lease if initial two-PC observations are not consistent.
    try:
        check = check_lan_onboarding(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
            coordinator_port=coordinator_port, task_name=task_name,
        )
        if check.get("status") != "LAN_ONBOARD_OBSERVATIONS_COMPLETE":
            report["preflight"] = "LAN_PACING_ONBOARDING_INCOMPLETE"
            return report
        report["preflight"] = "LAN_PACING_ONBOARDING_OBSERVED"
        client = LanCoordinatorClient.from_env()
        if client is None:
            report["status"] = "LAN_PACING_CLIENT_UNCONFIGURED"
            return report
        start = client.status()
        instance = start.get("coordinator_instance_id")
        if not isinstance(instance, str) or not _SHA256.fullmatch(instance):
            report["status"] = "LAN_PACING_IDENTITY_UNVERIFIED"
            return report
        initial = start.get("code")
        report["initial_gate"] = (
            initial if initial in {"AVAILABLE", "ACTIVE", "WAIT_GAP", "COOLDOWN"}
            else "INVALID_STATUS"
        )
        if start.get("ok") is not True or initial != "AVAILABLE":
            report["status"] = "LAN_PACING_GATE_NOT_AVAILABLE"
            return report
    except (LocalYtDlpDeferred, ValueError, OSError, UnicodeError):
        report["status"] = "LAN_PACING_PREFLIGHT_UNVERIFIED"
        return report

    try:
        report["lease_requested"] = True
        report["changes_may_be_partial"] = True
        # reserve() always calls its signed release in finally after entry.
        # A broken peer witness never causes us to skip lease release.
        with client.reserve(timeout_seconds=70):
            report["lease_acquired"] = True
            report["possible_active_lease_until_expiry"] = True
            report["held_witness"] = (
                "LAN_PACING_WITNESS_HELD_DENIED"
                if _remote_witness(commands["held"], phase="held", identity=instance)
                else "LAN_PACING_HELD_WITNESS_UNVERIFIED"
            )
        report["lease_release_confirmed"] = True
        report["possible_active_lease_until_expiry"] = False
        report["regular_pacing_gap_created"] = True
        if report["held_witness"] != "LAN_PACING_WITNESS_HELD_DENIED":
            report["status"] = "LAN_PACING_HELD_WITNESS_UNVERIFIED"
            return report

        report["gap_witness"] = (
            "LAN_PACING_WITNESS_GAP_DENIED"
            if _remote_witness(commands["gap"], phase="gap", identity=instance)
            else "LAN_PACING_GAP_WITNESS_UNVERIFIED"
        )
        if report["gap_witness"] != "LAN_PACING_WITNESS_GAP_DENIED":
            report["status"] = "LAN_PACING_GAP_WITNESS_UNVERIFIED"
            return report
        report["same_coordinator_state_observed"] = True
        report["status"] = "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED"
        report["changes_may_be_partial"] = False
    except (LocalYtDlpDeferred, ValueError, OSError, UnicodeError) as exc:
        report["status"] = (
            "LAN_PACING_RELEASE_UNCONFIRMED"
            if report["lease_acquired"] and
            str(exc) == "LOCAL_YTDLP_COORDINATOR_RELEASE_UNCONFIRMED"
            else "LAN_PACING_TRIAL_UNVERIFIED"
        )
    return report
