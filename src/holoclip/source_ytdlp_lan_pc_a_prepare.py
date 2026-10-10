"""Safe ordered PC-A setup: local bootstrap, narrow firewall, then logon task.

Preview never writes; apply must be explicitly requested. Each existing step
retains its own fail-closed checks. This does not start or qualify a service.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_COORDINATOR_STATE, DEFAULT_SECRET_PATH,
    _private_ipv4,
)
from holoclip.source_ytdlp_lan_firewall_prepare import prepare_lan_firewall
from holoclip.source_ytdlp_lan_local_bootstrap import (
    _safe_local_storage, bootstrap_local_lan,
)
from holoclip.source_ytdlp_lan_task_prepare import inspect_lan_task_registration

_TASK_NAME = re.compile(r"[A-Za-z0-9 _-]{1,90}\Z")
_FIREWALL_ALLOWED = frozenset({
    "LAN_FIREWALL_PREVIEW_ELIGIBLE", "LAN_FIREWALL_ALREADY_SCOPED",
})
_FIREWALL_APPLIED = frozenset({
    "LAN_FIREWALL_RULE_ADDED", "LAN_FIREWALL_ALREADY_SCOPED",
})


def _valid_params(
    bind_ip: str, peer_ip: str, port: int, task_name: str,
    secret_file: Path, config_file: Path, state_path: Path,
) -> tuple[str, str, Path, Path, Path]:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("LAN_PC_A_INVALID_PORT")
    try:
        bind = _private_ipv4(bind_ip)
        peer = _private_ipv4(peer_ip)
    except (ValueError, TypeError) as exc:
        raise ValueError("LAN_PC_A_UNSAFE_ADDRESS") from exc
    if bind != bind_ip or peer != peer_ip or bind == peer or bind.startswith("127."):
        raise ValueError("LAN_PC_A_UNSAFE_ADDRESS")
    if peer.startswith("127."):
        raise ValueError("LAN_PC_A_UNSAFE_ADDRESS")
    if not isinstance(task_name, str) or _TASK_NAME.fullmatch(task_name) is None:
        raise ValueError("LAN_PC_A_INVALID_TASK_NAME")
    paths = tuple(_safe_local_storage(p) for p in (
        secret_file, config_file, state_path,
    ))
    if len(set(paths)) != len(paths):
        raise ValueError("LAN_PC_A_OVERLAPPING_STORAGE")
    if any(a in b.parents for a in paths for b in paths if a != b):
        raise ValueError("LAN_PC_A_OVERLAPPING_STORAGE")
    return bind, peer, paths[0], paths[1], paths[2]


def prepare_pc_a_lan(
    *, bind_ip: str, peer_ip: str, port: int = 8878,
    task_name: str = "Holoclip yt-dlp LAN Coordinator",
    secret_file: Path = DEFAULT_SECRET_PATH,
    config_file: Path = DEFAULT_CLIENT_CONFIG,
    state_path: Path = DEFAULT_COORDINATOR_STATE,
    apply: bool = False,
) -> dict[str, object]:
    """Run one composite preview, or guarded ordered changes on PC-A.

    No rollback: a later-step failure must not delete existing secret or
    firewall state. Report the last successful stage without raw OS output.
    """
    bind, peer, secret, config, state = _valid_params(
        bind_ip, peer_ip, port, task_name, secret_file, config_file, state_path,
    )
    report: dict[str, object] = {
        "status": "LAN_PC_A_PREVIEW_BLOCKED",
        "mode": "apply" if apply else "preview",
        "steps": {
            "client": "LAN_PC_A_NOT_CHECKED",
            "firewall": "LAN_PC_A_NOT_CHECKED",
            "task": "LAN_PC_A_NOT_CHECKED",
        },
        "completed_steps": [],
        "blocking_step": None,
        "credential_generated": False,
        "firewall_rule_added": False,
        "task_registered": False,
        "task_started": False,
        "changes_may_be_partial": False,
        "ssh_trust_modified": False,
        "credential_transferred": False,
        "youtube_contacted": False,
        "media_downloaded": False,
        "firewall_effective_policy_verified": False,
        "two_host_verified": False,
        "ready": False,
    }
    steps: dict[str, str] = report["steps"]  # type: ignore[assignment]
    completed: list[str] = report["completed_steps"]  # type: ignore[assignment]

    if apply and os.name != "nt":
        report["status"] = "LAN_PC_A_WINDOWS_REQUIRED"
        report["blocking_step"] = "platform"
        return report

    # Preflight all three steps before permitting *any* change. The task
    # preview may report missing credentials/firewall on a fresh PC-A;
    # those are expected to become valid after preceding gated stages.
    try:
        client_preview = bootstrap_local_lan(
            role="pc-a", coordinator_ip=bind, coordinator_port=port,
            secret_file=secret, config_file=config, apply=False,
        )
        steps["client"] = str(client_preview["status"])
        if client_preview["status"] != "LAN_BOOTSTRAP_PREVIEW":
            report["blocking_step"] = "client"
            return report

        firewall_preview = prepare_lan_firewall(
            bind_ip=bind, peer_ip=peer, port=port, apply=False,
        )
        steps["firewall"] = str(firewall_preview["status"])
        if steps["firewall"] not in _FIREWALL_ALLOWED:
            report["blocking_step"] = "firewall"
            return report

        task_preview = inspect_lan_task_registration(
            bind_ip=bind, peer_ip=peer, port=port, task_name=task_name,
            secret_file=secret, state_path=state, apply=False,
        )
        steps["task"] = str(task_preview["status"])
        task_checks = task_preview["checks"]
        if not isinstance(task_checks, dict) or (
            task_checks.get("platform") != "LAN_TASK_WINDOWS_OBSERVED"
            or task_checks.get("task") != "LAN_TASK_ABSENT_OBSERVED"
        ):
            report["blocking_step"] = "task"
            return report
        if steps["task"] not in {"LAN_TASK_PREVIEW_ELIGIBLE", "LAN_TASK_PREVIEW_BLOCKED"}:
            report["blocking_step"] = "task"
            return report
        # Block unexpected failure states not remedied by bootstrap/firewall.
        permitted = {
            "credential": {"LAN_TASK_CREDENTIAL_VALID", "LAN_TASK_CREDENTIAL_UNVERIFIED"},
            "credential_acl": {"LAN_TASK_ACL_SECURE", "LAN_TASK_ACL_UNVERIFIED"},
            "firewall": {"LAN_TASK_EXACT_PEER_RULE_OBSERVED", "LAN_TASK_FIREWALL_UNVERIFIED"},
        }
        if any(task_checks.get(key) not in allowed for key, allowed in permitted.items()):
            report["blocking_step"] = "task"
            return report
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        report["status"] = "LAN_PC_A_PREFLIGHT_UNVERIFIED"
        report["blocking_step"] = "preflight"
        return report

    if not apply:
        report["status"] = "LAN_PC_A_PREVIEW_ELIGIBLE"
        return report

    # A stage can be interrupted after writing durable state. Never claim
    # a global rollback; stop before later stages and preserve exact progress.
    report["status"] = "LAN_PC_A_APPLY_INCOMPLETE"
    report["changes_may_be_partial"] = True
    try:
        report["blocking_step"] = "client"
        result = bootstrap_local_lan(
            role="pc-a", coordinator_ip=bind, coordinator_port=port,
            secret_file=secret, config_file=config, apply=True,
        )
        steps["client"] = str(result["status"])
        if result["status"] != "LAN_BOOTSTRAP_LOCAL_PREPARED":
            return report
        completed.append("client")
        report["credential_generated"] = result.get("credential_generated") is True

        report["blocking_step"] = "firewall"
        result = prepare_lan_firewall(
            bind_ip=bind, peer_ip=peer, port=port, apply=True,
        )
        steps["firewall"] = str(result["status"])
        report["firewall_rule_added"] = result.get("rule_added") is True
        if steps["firewall"] not in _FIREWALL_APPLIED:
            report["changes_may_be_partial"] = True
            return report
        completed.append("firewall")

        report["blocking_step"] = "task"
        result = inspect_lan_task_registration(
            bind_ip=bind, peer_ip=peer, port=port, task_name=task_name,
            secret_file=secret, state_path=state, apply=True,
        )
        steps["task"] = str(result["status"])
        if steps["task"] != "LAN_TASK_REGISTERED_NOT_STARTED":
            report["changes_may_be_partial"] = True
            return report
        completed.append("task")
        report["task_registered"] = True
        report["blocking_step"] = None
        report["status"] = "LAN_PC_A_PREPARED_NOT_STARTED"
        report["changes_may_be_partial"] = False
        return report
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        report["status"] = "LAN_PC_A_APPLY_UNCONFIRMED"
        report["changes_may_be_partial"] = True
        return report
