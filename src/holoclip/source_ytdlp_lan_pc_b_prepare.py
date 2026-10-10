"""Fail-closed PC-B LAN enrollment over *previously pinned* public-key SFTP.

Preview performs only local inspection and ssh-keygen host-entry lookup.
Explicit apply reuses the strict SFTP import and existing latched client
bootstrap, then makes a signed status-only probe (no pacing lease/media).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_SECRET_PATH, _client_required_marker,
    _private_ipv4, _reject_linked_path,
)
from holoclip.source_ytdlp_lan_local_bootstrap import (
    _safe_local_storage, bootstrap_local_lan,
)
from holoclip.source_ytdlp_lan_onboarding import diagnose_lan_client
from holoclip.source_ytdlp_lan_setup_doctor import _host_pin_state
from holoclip.source_ytdlp_lan_ssh_transfer import (
    _REMOTE_PATH, _SSH_USER, pull_lan_secret_sftp,
)

_PIN_PRESENT = "HOST_PIN_ENTRY_OBSERVED"
_SAFE_STATUS = frozenset({
    "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
    "NOT_CONFIGURED",
    "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID",
    "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE",
    "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED",
})
_SUCCESSFUL_IMPORT = frozenset({
    "LAN_SECRET_SFTP_IMPORTED", "LAN_SECRET_SFTP_ALREADY_IMPORTED",
})


def _validate(
    *, coordinator_ip: str, ssh_user: str, remote_path: str,
    known_hosts: Path, ssh_port: int, coordinator_port: int,
    secret_file: Path, config_file: Path,
) -> tuple[str, Path, Path, Path]:
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError("LAN_PC_B_INVALID_SSH_PORT")
    if type(coordinator_port) is not int or not 1024 <= coordinator_port <= 65535:
        raise ValueError("LAN_PC_B_INVALID_COORDINATOR_PORT")
    try:
        host = _private_ipv4(coordinator_ip)
    except (ValueError, TypeError) as exc:
        raise ValueError("LAN_PC_B_UNSAFE_COORDINATOR_ADDRESS") from exc
    if host != coordinator_ip or host.startswith("127."):
        raise ValueError("LAN_PC_B_UNSAFE_COORDINATOR_ADDRESS")
    if not isinstance(ssh_user, str) or _SSH_USER.fullmatch(ssh_user) is None:
        raise ValueError("LAN_PC_B_INVALID_SSH_USER")
    if (
        not isinstance(remote_path, str)
        or _REMOTE_PATH.fullmatch(remote_path) is None
        or any(part in ("", ".", "..") for part in remote_path.split("/")[1:])
    ):
        raise ValueError("LAN_PC_B_UNSAFE_REMOTE_PATH")
    secret = _safe_local_storage(secret_file)
    config = _safe_local_storage(config_file)
    marker = _client_required_marker(config)
    hosts = known_hosts.absolute()
    _reject_linked_path(marker)
    _reject_linked_path(hosts)
    paths = (secret, config, marker, hosts)
    if len(set(paths)) != len(paths):
        raise ValueError("LAN_PC_B_OVERLAPPING_LOCAL_PATHS")
    if any(a in b.parents for a in paths for b in paths if a != b):
        raise ValueError("LAN_PC_B_OVERLAPPING_LOCAL_PATHS")
    if any(any(char in str(p) for char in ('"', "\r", "\n", "\x00")) for p in paths):
        raise ValueError("LAN_PC_B_UNSAFE_LOCAL_PATH")
    if not hosts.is_file() or not 1 <= hosts.stat().st_size <= 1024 * 1024:
        raise ValueError("LAN_PC_B_KNOWN_HOSTS_MISSING")
    return host, hosts, secret, config


def prepare_pc_b_lan(
    *, coordinator_ip: str, ssh_user: str, remote_path: str,
    known_hosts: Path, ssh_port: int = 22, coordinator_port: int = 8878,
    secret_file: Path = DEFAULT_SECRET_PATH,
    config_file: Path = DEFAULT_CLIENT_CONFIG,
    apply: bool = False,
) -> dict[str, object]:
    """Stage PC-B enrollment without auto-trusting the SSH server identity."""
    host, hosts, secret, config = _validate(
        coordinator_ip=coordinator_ip, ssh_user=ssh_user,
        remote_path=remote_path, known_hosts=known_hosts,
        ssh_port=ssh_port, coordinator_port=coordinator_port,
        secret_file=secret_file, config_file=config_file,
    )
    report: dict[str, object] = {
        "status": "LAN_PC_B_PREVIEW_BLOCKED",
        "mode": "apply" if apply else "preview",
        "steps": {
            "ssh_host_pin": "LAN_PC_B_NOT_CHECKED",
            "credential": "LAN_PC_B_NOT_CHECKED",
            "client": "LAN_PC_B_NOT_CHECKED",
            "signed_status": "LAN_PC_B_NOT_CHECKED",
        },
        "completed_steps": [],
        "blocking_step": None,
        "credential_imported": False,
        "credential_generated": False,
        "client_profile_prepared": False,
        "remote_coordinator_authenticated": False,
        "changes_may_be_partial": False,
        "ssh_host_identity_independently_verified": False,
        "ssh_host_pin_created": False,
        "ssh_configuration_modified": False,
        "coordinator_started": False,
        "task_modified": False,
        "firewall_modified": False,
        "youtube_contacted": False,
        "media_downloaded": False,
        "firewall_effective_policy_verified": False,
        "two_host_verified": False,
        "ready": False,
    }
    steps: dict[str, str] = report["steps"]  # type: ignore[assignment]
    done: list[str] = report["completed_steps"]  # type: ignore[assignment]

    if os.name != "nt":
        report["status"] = "LAN_PC_B_WINDOWS_REQUIRED"
        report["blocking_step"] = "platform"
        return report

    # These checks are read-only. A found key is NOT evidence that its
    # fingerprint was independently verified by the operator.
    try:
        pin = _host_pin_state(host, hosts, ssh_port)
        steps["ssh_host_pin"] = pin
        if pin != _PIN_PRESENT or shutil.which("sftp") is None:
            if pin == _PIN_PRESENT:
                steps["ssh_host_pin"] = "LAN_PC_B_SFTP_UNAVAILABLE"
            report["blocking_step"] = "ssh_host_pin"
            return report

        preview = bootstrap_local_lan(
            role="pc-b", coordinator_ip=host,
            coordinator_port=coordinator_port, secret_file=secret,
            config_file=config, apply=False,
        )
        steps["client"] = str(preview["status"])
        if steps["client"] != "LAN_BOOTSTRAP_PREVIEW":
            report["blocking_step"] = "client"
            return report
    except (ValueError, OSError, UnicodeError, TypeError, KeyError):
        report["status"] = "LAN_PC_B_PREFLIGHT_UNVERIFIED"
        report["blocking_step"] = "preflight"
        return report

    if not apply:
        report["status"] = "LAN_PC_B_PREVIEW_ELIGIBLE"
        return report

    # The remote file and local credential may be touched before failures.
    # Never pretend a transaction rollback or delete an existing key.
    report["status"] = "LAN_PC_B_APPLY_INCOMPLETE"
    report["changes_may_be_partial"] = True
    report["blocking_step"] = "credential"
    try:
        pulled = pull_lan_secret_sftp(
            host=host, user=ssh_user, remote_path=remote_path,
            known_hosts=hosts, secret_file=secret, port=ssh_port,
        )
        steps["credential"] = str(pulled["status"])
        if steps["credential"] not in _SUCCESSFUL_IMPORT:
            return report
        done.append("credential")
        report["credential_imported"] = steps["credential"] == "LAN_SECRET_SFTP_IMPORTED"

        report["blocking_step"] = "client"
        configured = bootstrap_local_lan(
            role="pc-b", coordinator_ip=host,
            coordinator_port=coordinator_port, secret_file=secret,
            config_file=config, apply=True,
        )
        steps["client"] = str(configured["status"])
        if steps["client"] != "LAN_BOOTSTRAP_LOCAL_PREPARED":
            return report
        done.append("client")
        report["client_profile_prepared"] = True

        report["blocking_step"] = "signed_status"
        diagnosed = diagnose_lan_client()
        code = diagnosed.get("status")
        steps["signed_status"] = (
            code if type(code) is str and code in _SAFE_STATUS
            else "LAN_PC_B_SIGNED_STATUS_UNTRUSTED"
        )
        if (
            steps["signed_status"] == "AUTHENTICATED_LOCAL_LAN_COORDINATOR"
            and diagnosed.get("authenticated") is True
        ):
            report["remote_coordinator_authenticated"] = True
            done.append("signed_status")
            report["status"] = "LAN_PC_B_PREPARED_AUTHENTICATED"
        else:
            report["status"] = "LAN_PC_B_PREPARED_STATUS_UNVERIFIED"
        report["blocking_step"] = None if report["remote_coordinator_authenticated"] else "signed_status"
        report["changes_may_be_partial"] = False
        return report
    except (ValueError, OSError, UnicodeError, TypeError, KeyError):
        report["status"] = "LAN_PC_B_APPLY_UNCONFIRMED"
        return report
