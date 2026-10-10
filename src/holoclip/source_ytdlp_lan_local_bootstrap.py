"""Fail-closed local LAN client bootstrap with a no-change preview by default.

PC-A may create its own new credential; PC-B must already have a trusted
credential copy. Opt-in changes are credential creation, NTFS ACL hardening,
and the local latched client profile. No sshd/firewall/tasks/media.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_CLIENT_CONFIG, DEFAULT_SECRET_PATH, _client_required_marker,
    _private_ipv4, _read_client_config, _reject_linked_path, _secret, _url,
    _validate_required_marker, configure_lan_client, create_coordinator_secret,
)
from holoclip.source_ytdlp_lan_ssh_transfer import _secure_windows_path

_REPO = Path(__file__).resolve().parents[2]


def _is_windows() -> bool:
    return os.name == "nt"


def _safe_local_storage(path: Path) -> Path:
    """Avoid accidentally writing runtime files into tracked source paths."""
    target = path.absolute()
    _reject_linked_path(target)
    try:
        relative = target.relative_to(_REPO)
    except ValueError:
        return target  # Explicit external storage may be selected by user.
    if len(relative.parts) < 2 or relative.parts[0] != "data":
        raise ValueError("LAN_BOOTSTRAP_SOURCE_TREE_PATH_FORBIDDEN")
    if relative.parts[1] == ".gitkeep":
        raise ValueError("LAN_BOOTSTRAP_SOURCE_TREE_PATH_FORBIDDEN")
    return target


def _inspect_existing(*, secret: Path, config: Path, url: str) -> tuple[bool, bool]:
    """Validate all existing state before any changes; never replace a profile."""
    _validate_required_marker(config)
    if config.exists() or config.is_symlink():
        _reject_linked_path(config)
        if not secret.is_file():
            raise ValueError("LAN_BOOTSTRAP_CONFIG_PRESENT_WITHOUT_CREDENTIAL")
        try:
            saved_url, saved_secret = _read_client_config(config)
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("LAN_BOOTSTRAP_EXISTING_CONFIG_INVALID") from exc
        if saved_url != url or saved_secret.absolute() != secret:
            raise ValueError("LAN_BOOTSTRAP_EXISTING_CONFIG_DIFFERS")
    has_secret = secret.exists() or secret.is_symlink()
    if has_secret:
        _reject_linked_path(secret)
        try:
            _secret(secret)
        except (ValueError, OSError, UnicodeError) as exc:
            raise ValueError("LAN_BOOTSTRAP_EXISTING_CREDENTIAL_INVALID") from exc
    return has_secret, config.exists()


def bootstrap_local_lan(
    *, role: str, coordinator_ip: str, secret_file: Path = DEFAULT_SECRET_PATH,
    config_file: Path = DEFAULT_CLIENT_CONFIG, coordinator_port: int = 8878,
    apply: bool = False,
) -> dict[str, object]:
    """Prepare only this machine, never claiming real two-host qualification."""
    if role not in {"pc-a", "pc-b"}:
        raise ValueError("LAN_BOOTSTRAP_INVALID_ROLE")
    if type(coordinator_port) is not int or not 1024 <= coordinator_port <= 65535:
        raise ValueError("LAN_BOOTSTRAP_INVALID_PORT")
    host = _private_ipv4(coordinator_ip)
    if host.startswith("127."):
        raise ValueError("LAN_BOOTSTRAP_LOOPBACK_NOT_ALLOWED")
    url = _url(f"http://{host}:{coordinator_port}").removesuffix("/request")
    secret = _safe_local_storage(secret_file)
    profile = _safe_local_storage(config_file)
    marker = _client_required_marker(profile)
    _reject_linked_path(marker)
    if len({secret, profile, marker}) != 3:
        raise ValueError("LAN_BOOTSTRAP_OVERLAPPING_PATHS")
    if secret in profile.parents or profile in secret.parents:
        raise ValueError("LAN_BOOTSTRAP_OVERLAPPING_PATHS")
    has_secret, has_config = _inspect_existing(secret=secret, config=profile, url=url)

    proposed = []
    if not has_secret:
        proposed.append(
            "CREATE_NEW_PC_A_CREDENTIAL" if role == "pc-a"
            else "TRUSTED_PC_B_CREDENTIAL_IMPORT_REQUIRED"
        )
    proposed.extend(["VERIFY_OR_RESTRICT_LOCAL_NTFS_ACL",
                     "KEEP_EXISTING_PROFILE" if has_config else
                     "CREATE_LATCHED_LOCAL_CLIENT_PROFILE"])
    report: dict[str, object] = {
        "status": "LAN_BOOTSTRAP_PREVIEW",
        "role": role,
        "planned_actions": proposed,
        "credential_present": has_secret,
        "client_profile_present": has_config,
        "changes_applied": False,
        "credential_generated": False,
        "credential_printed": False,
        "credential_bytes_transferred": False,
        "ssh_host_pin_bootstrapped": False,
        "task_installed": False,
        "firewall_modified": False,
        "coordinator_started": False,
        "remote_coordinator_authenticated": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "ready": False,
    }
    if not apply:
        return report
    if not _is_windows():
        raise ValueError("LAN_BOOTSTRAP_WINDOWS_APPLY_REQUIRED")
    if not has_secret and role == "pc-b":
        report["status"] = "LAN_BOOTSTRAP_TRUSTED_CREDENTIAL_REQUIRED"
        return report

    created = False
    if not has_secret:
        create_coordinator_secret(secret)
        created = True
    try:
        if _secure_windows_path(secret, apply=True) is not True:
            raise ValueError("LAN_BOOTSTRAP_CREDENTIAL_ACL_UNVERIFIED")
    except (ValueError, OSError) as exc:
        if created:
            secret.unlink(missing_ok=True)
        raise ValueError("LAN_BOOTSTRAP_CREDENTIAL_ACL_UNVERIFIED") from exc

    try:
        status = configure_lan_client(
            url=url, secret_file=secret, config_path=profile, replace=False,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        # Keep a newly created *secured* key if profile writing failed.
        raise ValueError("LAN_BOOTSTRAP_PROFILE_WRITE_FAILED") from exc

    report.update(
        status="LAN_BOOTSTRAP_LOCAL_PREPARED",
        changes_applied=True,
        credential_present=True,
        credential_generated=created,
        client_profile_present=True,
        client_profile_result=status,
    )
    return report
