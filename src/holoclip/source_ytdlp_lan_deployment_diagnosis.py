"""Read-only, fail-closed PC-A/PC-B deployment diagnosis and ordered repair plan.

Unlike strict paired attestation, transport failure retains *only* a fresh,
sanitized local PC-A inspection. No remote status is fabricated, and unknown
responses never qualify deployment or production. No yt-dlp/lease operations.
"""
from __future__ import annotations

from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import _private_ipv4
from holoclip.source_ytdlp_lan_onboard_check import (
    _safe_doctor, check_lan_onboarding,
)
from holoclip.source_ytdlp_lan_setup_doctor import inspect_lan_setup
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

_EXPECTED = {
    "pc-a": {
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
        "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
        "coordinator_task": "RUNNING_OBSERVED",
        "coordinator_listener": "LAN_LISTENER_OBSERVED",
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
    },
    "pc-b": {
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
    },
}
_ORDER = [(role, key) for role in ("pc-a", "pc-b") for key in _EXPECTED[role]]
_COMMANDS = {
    ("pc-a", "local_credential"): "examples-ytdlp-lan-pc-a-prepare",
    ("pc-a", "credential_acl"): "examples-ytdlp-lan-secret-acl",
    ("pc-a", "firewall_rule"): "examples-ytdlp-lan-firewall-prepare",
    ("pc-a", "coordinator_task"): "examples-ytdlp-lan-task-prepare",
    ("pc-a", "coordinator_listener"): "examples-ytdlp-lan-setup-doctor",
    ("pc-a", "signed_coordinator_status"): "examples-ytdlp-lan-doctor",
    ("pc-b", "local_credential"): "examples-ytdlp-lan-pc-b-prepare",
    ("pc-b", "credential_acl"): "examples-ytdlp-lan-secret-acl",
    ("pc-b", "signed_coordinator_status"): "examples-ytdlp-lan-doctor",
}
_SSH_ERRORS = frozenset({
    "LAN_ONBOARD_REMOTE_SSH_FAILED",
    "LAN_PAIR_SSH_CLIENT_NOT_FOUND",
    "LAN_PAIR_PINNED_HOST_KEY_REQUIRED",
    "LAN_PAIR_INVALID_SSH_USER",
    "LAN_PAIR_INVALID_SSH_PORT",
    "LAN_PAIR_UNSAFE_REMOTE_REPOSITORY",
    "LAN_PAIR_UNSAFE_KNOWN_HOSTS_PATH",
})
_UNTRUSTED_ERRORS = frozenset({
    "LAN_ONBOARD_REMOTE_REPORT_INVALID",
    "LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT",
    "LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC",
    "LAN_PAIR_INVALID_REPORT",
    "LAN_PAIR_BAD_SIGNATURE",
    "LAN_PAIR_DUPLICATE_REPORT",
    "LAN_PAIR_STALE_REPORT",
    "LAN_PAIR_BROKER_IDENTITY_UNAVAILABLE",
})
_MANUAL_GATES = [
    "VERIFY_SSH_FINGERPRINT_OUT_OF_BAND",
    "VERIFY_WINDOWS_FIREWALL_EFFECTIVE_POLICY_ON_REAL_HOSTS",
    "VERIFY_REAL_TWO_PC_SHARED_PACING_WITHOUT_HOSTED_YOUTUBE",
    "QUALIFY_REAL_VIDEO_AND_EDITORIAL_OUTPUT",
]


def _ordered_repairs(
    pc_a: dict[str, object] | None,
    pc_b: dict[str, object] | None,
    remote_issue: str | None,
    pair_status: str | None,
) -> list[dict[str, object]]:
    plan: list[dict[str, object]] = []
    for role, check in _ORDER:
        doc = pc_a if role == "pc-a" else pc_b
        if doc is None:
            continue
        codes = doc["checks"]
        code = codes[check]
        expected = _EXPECTED[role][check]
        if code != expected:
            plan.append({
                "priority": len(plan) + 1,
                "role": role,
                "check": check,
                "observed": code,
                "expected": expected,
                "suggested_command": _COMMANDS[(role, check)],
                "automatically_modified": False,
            })
    if remote_issue is not None:
        plan.append({
            "priority": len(plan) + 1,
            "role": "pc-b",
            "check": "remote_ssh",
            "observed": remote_issue,
            "expected": "PINNED_PUBLIC_KEY_SSH_REPORT_VALIDATED",
            "suggested_command": "examples-ytdlp-lan-onboard-check",
            "automatically_modified": False,
        })
    elif pc_a is not None and pc_b is not None and pair_status != "LAN_PAIR_OBSERVATIONS_MATCH":
        plan.append({
            "priority": len(plan) + 1,
            "role": "pair",
            "check": "signed_pair",
            "observed": pair_status or "LAN_PAIR_RECEIPT_UNAVAILABLE",
            "expected": "LAN_PAIR_OBSERVATIONS_MATCH",
            "suggested_command": "examples-ytdlp-lan-pair-collect",
            "automatically_modified": False,
        })
    return plan


def diagnose_lan_deployment(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
) -> dict[str, object]:
    """Report available PC-A evidence even when pre-pinned PC-B SSH is unavailable."""
    try:
        peer = _private_ipv4(host)
    except (ValueError, TypeError) as exc:
        raise ValueError("LAN_DEPLOYMENT_INVALID_PEER") from exc
    if peer != host or peer.startswith("127."):
        raise ValueError("LAN_DEPLOYMENT_INVALID_PEER")
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError("LAN_DEPLOYMENT_INVALID_SSH_PORT")
    if type(coordinator_port) is not int or not 1024 <= coordinator_port <= 65535:
        raise ValueError("LAN_DEPLOYMENT_INVALID_COORDINATOR_PORT")

    local: dict[str, object] | None = None
    remote: dict[str, object] | None = None
    remote_issue: str | None = None
    pair_status: str | None = None
    paired_result: dict | None = None
    try:
        paired_result = check_lan_onboarding(
            host=peer, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
            coordinator_port=coordinator_port, task_name=task_name,
        )
        local = paired_result["pc_a"]
        remote = paired_result["pc_b"]
        pair = paired_result.get("pair")
        if isinstance(pair, dict):
            pair_status = pair.get("status")
        # A positive observation must not survive an inconsistent schema.
        if pair_status not in {
            None, "LAN_PAIR_OBSERVATIONS_MATCH", "LAN_PAIR_OBSERVATIONS_INCONCLUSIVE",
        }:
            raise ValueError("LAN_DEPLOYMENT_UNTRUSTED_PAIR_RESULT")
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        code = str(exc)
        if code in _SSH_ERRORS:
            remote_issue = code
        elif code in _UNTRUSTED_ERRORS:
            remote_issue = "LAN_DEPLOYMENT_REMOTE_REPORT_UNTRUSTED"
        else:
            remote_issue = "LAN_DEPLOYMENT_REMOTE_DIAGNOSIS_UNVERIFIED"
        # A remote failure must never appear as a PC-B observation. Inspect
        # PC-A locally *again*; never reuse a possibly tainted partial result.
        local = None
        remote = None
        pair_status = None
        try:
            local = _safe_doctor(inspect_lan_setup(
                role="pc-a", peer_ip=peer,
                port=coordinator_port, task_name=task_name,
            ), "pc-a")
        except (ValueError, OSError, UnicodeError, TypeError, KeyError):
            local = None

    repairs = _ordered_repairs(local, remote, remote_issue, pair_status)
    matched = (
        paired_result is not None
        and paired_result.get("status") == "LAN_ONBOARD_OBSERVATIONS_COMPLETE"
        and local is not None and remote is not None
        and pair_status == "LAN_PAIR_OBSERVATIONS_MATCH"
        and not repairs
    )
    if remote_issue is not None:
        status = "LAN_DEPLOYMENT_REMOTE_UNVERIFIED"
    elif matched:
        status = "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
    elif paired_result is None or local is None:
        status = "LAN_DEPLOYMENT_INSPECTION_INCOMPLETE"
    else:
        status = "LAN_DEPLOYMENT_GAPS_OBSERVED"
    return {
        "status": status,
        "pc_a": local,
        "pc_b": remote,
        "remote_failure": remote_issue,
        "pair_observation": pair_status,
        "repair_plan": repairs,
        "manual_qualification_gates": list(_MANUAL_GATES),
        "ssh_fingerprint_independently_verified": False,
        "remote_ssh_report_validated": remote is not None,
        "local_state_modified": False,
        "credential_transferred": False,
        "task_started": False,
        "firewall_modified": False,
        "lease_requested": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "firewall_effective_policy_verified": False,
        "ready": False,
    }
