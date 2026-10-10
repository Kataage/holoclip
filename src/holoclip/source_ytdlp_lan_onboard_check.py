"""Read-only paired Windows LAN onboarding inspection over pre-pinned SSH.

Reports include only hardcoded diagnostic codes and a purpose-signed status
receipt. A matching result does not attest independent physical devices,
effective firewall policy, or production/source-media quality.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from holoclip.source_ytdlp_lan_pair_collect import _pinned_ssh_command
from holoclip.source_ytdlp_lan_pair_evidence import (
    compare_lan_pair_reports, observe_lan_pair_evidence,
)
from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _private_ipv4,
)
from holoclip.source_ytdlp_lan_setup_doctor import (
    _ACTIONS, _SAFE_WINDOWS_CODES, inspect_lan_setup,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

_VERSION = "holoclip-lan-onboard-v1"
_MAX_REMOTE_BYTES = 16384
_CHECK_KEYS = frozenset({
    "signed_coordinator_status", "local_credential", "credential_acl",
    "coordinator_task", "coordinator_listener", "firewall_rule",
    "sshd_service", "ssh_host_pin",
})
_ALLOWED_CODES = frozenset(_ACTIONS) | frozenset({
    "AUTHENTICATED_LOCAL_LAN_COORDINATOR", "LAN_CREDENTIAL_VALID",
    "LAN_CREDENTIAL_ACL_OBSERVED", "WINDOWS_INSPECTION_UNAVAILABLE",
    "HOST_PIN_ENTRY_OBSERVED",
}) | frozenset().union(*_SAFE_WINDOWS_CODES.values())
_ALLOWED_STATUS = frozenset({"LOCAL_SETUP_INCOMPLETE", "LOCAL_SETUP_OBSERVATIONS_COMPLETE"})


def _safe_doctor(raw: object, role: str) -> dict[str, object]:
    """Discard all freeform strings from an untrusted remote diagnostic."""
    if (not isinstance(raw, dict) or raw.get("role") != role or
            raw.get("status") not in _ALLOWED_STATUS or
            raw.get("ready") is not False or
            raw.get("production_ready") is not False or
            raw.get("two_host_verified") is not False or
            raw.get("firewall_effective_policy_verified") is not False or
            raw.get("config_modified") is not False or
            raw.get("youtube_contacted") is not False):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC")
    checks = raw.get("checks")
    if not isinstance(checks, dict) or set(checks) != _CHECK_KEYS:
        raise ValueError("LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC")
    safe_codes: dict[str, str] = {}
    for key in sorted(_CHECK_KEYS):
        item = checks[key]
        if not isinstance(item, dict):
            raise ValueError("LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC")
        code = item.get("code")
        if not isinstance(code, str) or code not in _ALLOWED_CODES:
            raise ValueError("LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC")
        safe_codes[key] = code
    # "Complete" status must never be trusted if it contradicts the
    # known PC-B required gates. PC-A has additional Windows prerequisites.
    required = {
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
    }
    if role == "pc-a":
        required.update({
            "coordinator_task": "RUNNING_OBSERVED",
            "coordinator_listener": "LAN_LISTENER_OBSERVED",
            "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
        })
    if raw["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE" and any(
        safe_codes[k] != expected for k, expected in required.items()
    ):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_DIAGNOSTIC")
    return {
        "role": role, "status": raw["status"], "checks": safe_codes,
    }


def inspect_lan_onboard_node(
    *, role: str, peer_ip: str | None = None,
    port: int = 8878, task_name: str = "Holoclip yt-dlp LAN Coordinator",
) -> dict[str, object]:
    """Local inspection plus an optional fresh signed status, never a lease."""
    if role not in {"pc-a", "pc-b"}:
        raise ValueError("LAN_ONBOARD_INVALID_ROLE")
    doc = inspect_lan_setup(role=role, peer_ip=peer_ip,
                            port=port, task_name=task_name)
    clean = _safe_doctor(doc, role)
    receipt: dict | None = None
    if clean["checks"]["signed_coordinator_status"] == "AUTHENTICATED_LOCAL_LAN_COORDINATOR":
        try:
            receipt = observe_lan_pair_evidence(role=role)
        except (LocalYtDlpDeferred, ValueError, OSError):
            # No stale receipt or unsigned success on transient failure.
            receipt = None
    return {
        "version": _VERSION,
        "role": role,
        "diagnostic": clean,
        "signed_receipt": receipt,
        "ready": False,
        "two_host_verified": False,
    }


def _safe_node_report(raw: object) -> dict[str, object]:
    if (not isinstance(raw, dict) or
            set(raw) != {"version", "role", "diagnostic", "signed_receipt",
                         "ready", "two_host_verified"} or
            raw.get("version") != _VERSION or raw.get("role") != "pc-b" or
            raw.get("ready") is not False or raw.get("two_host_verified") is not False):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT")
    diagnostic = raw["diagnostic"]
    # A remote report intentionally contains a reduced diagnostic. Rehydrate
    # only known codes into the strict server-facing schema.
    if (not isinstance(diagnostic, dict) or
            set(diagnostic) != {"role", "status", "checks"} or
            diagnostic.get("role") != "pc-b" or
            diagnostic.get("status") not in _ALLOWED_STATUS or
            not isinstance(diagnostic.get("checks"), dict) or
            set(diagnostic["checks"]) != _CHECK_KEYS or
            any(not isinstance(x, str) or x not in _ALLOWED_CODES
                for x in diagnostic["checks"].values())):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT")
    checks = diagnostic["checks"]
    if diagnostic["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE" and any(
        checks[k] != c for k, c in (
            ("signed_coordinator_status", "AUTHENTICATED_LOCAL_LAN_COORDINATOR"),
            ("local_credential", "LAN_CREDENTIAL_VALID"),
            ("credential_acl", "LAN_CREDENTIAL_ACL_OBSERVED"),
        )
    ):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT")
    receipt = raw["signed_receipt"]
    if receipt is not None and not isinstance(receipt, dict):
        raise ValueError("LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT")
    return {
        "role": "pc-b",
        "diagnostic": {
            "role": "pc-b",
            "status": diagnostic["status"],
            "checks": {k: checks[k] for k in sorted(_CHECK_KEYS)},
        },
        "signed_receipt": receipt,
    }


def _remediations(a: dict, b: dict) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for role, doc in (("pc-a", a), ("pc-b", b)):
        for check, code in doc["checks"].items():
            # Optional checks are not blockers. PC-A's SSHD service and
            # PC-B's coordinator process/firewall observations are optional.
            required = (
                {"signed_coordinator_status", "local_credential", "credential_acl",
                 "coordinator_task", "coordinator_listener", "firewall_rule"}
                if role == "pc-a"
                else {"signed_coordinator_status", "local_credential", "credential_acl"}
            )
            expected = {
                "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
                "local_credential": "LAN_CREDENTIAL_VALID",
                "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
                "coordinator_task": "RUNNING_OBSERVED",
                "coordinator_listener": "LAN_LISTENER_OBSERVED",
                "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
            }
            if check in required and code != expected[check]:
                output.append({
                    "role": role, "check": check, "code": code,
                    "action": _ACTIONS.get(code, "Inspect the local setup doctor output."),
                })
    return output


def check_lan_onboarding(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22, coordinator_port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
) -> dict[str, object]:
    """Read both PCs through one pinned SSH call. Do not change their setup."""
    peer = _private_ipv4(host)
    if peer.startswith("127."):
        raise ValueError("LAN_ONBOARD_PC_B_LOOPBACK_NOT_ALLOWED")
    cmd = _pinned_ssh_command(
        host=peer, user=user, known_hosts=known_hosts,
        remote_repo=remote_repo, port=ssh_port, remote_probe="onboard",
    )
    local = inspect_lan_onboard_node(
        role="pc-a", peer_ip=peer, port=coordinator_port, task_name=task_name,
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LAN_ONBOARD_REMOTE_SSH_FAILED") from exc
    if result.returncode != 0:
        raise ValueError("LAN_ONBOARD_REMOTE_SSH_FAILED")
    if not 1 <= len(result.stdout) <= _MAX_REMOTE_BYTES:
        raise ValueError("LAN_ONBOARD_REMOTE_REPORT_INVALID")
    try:
        remote = _safe_node_report(json.loads(result.stdout))
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("LAN_ONBOARD_REMOTE_REPORT_INVALID") from exc

    a = local["diagnostic"]
    b = remote["diagnostic"]
    remediation = _remediations(a, b)
    pair: dict | None = None
    if local["signed_receipt"] is not None and remote["signed_receipt"] is not None:
        client = LanCoordinatorClient.from_env()
        if client is None:
            raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID")
        pair = compare_lan_pair_reports(
            pc_a=local["signed_receipt"], pc_b=remote["signed_receipt"],
            shared_secret=client.secret,
        )
    complete = (
        a["status"] == b["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
        and not remediation and pair is not None and
        pair["status"] == "LAN_PAIR_OBSERVATIONS_MATCH"
    )
    return {
        "status": ("LAN_ONBOARD_OBSERVATIONS_COMPLETE" if complete else
                   "LAN_ONBOARD_SETUP_INCOMPLETE" if remediation or
                   a["status"] != "LOCAL_SETUP_OBSERVATIONS_COMPLETE" or
                   b["status"] != "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
                   else "LAN_ONBOARD_PAIR_INCONCLUSIVE"),
        "pc_a": a, "pc_b": b,
        "remediations": remediation,
        "pair": pair,
        "collection": "PREPINNED_PUBLIC_KEY_SSH",
        "pc_b_ssh_fingerprint_verified_out_of_band": False,
        "windows_firewall_effective_policy_verified": False,
        "real_two_host_pacing_qualified": False,
        "hardware_identity_verified": False,
        "config_modified": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "ready": False,
    }
