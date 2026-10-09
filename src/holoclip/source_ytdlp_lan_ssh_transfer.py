"""Strict host-key-pinned SFTP pull of an existing shared LAN credential.

No SSH server provisioning, TOFU/accept-new, password prompts, or yt-dlp.
Only an already enrolled SSH server with an independently verified host key
and public-key authentication can be used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_SECRET_PATH, _private_ipv4, _reject_linked_path, _secret,
)
from holoclip.source_ytdlp_lan_onboarding import import_lan_secret

_SSH_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_REMOTE_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+)+|(?:[A-Za-z]:/)(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\Z")
_MAX_KNOWN_HOSTS_BYTES = 1024 * 1024


def _secure_windows_path(path: Path, *, apply: bool) -> bool:
    """Lock down or inspect Windows NTFS DACL; never print underlying details."""
    if os.name != "nt":
        return False
    ps = shutil.which("powershell.exe")
    script = Path(__file__).resolve().parents[2] / "scripts" / "secure-ytdlp-lan-acl.ps1"
    if ps is None or not script.is_file():
        raise ValueError("LAN_WINDOWS_ACL_TOOL_UNAVAILABLE")
    args = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Path", str(path)]
    if apply:
        args.append("-Apply")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=20, check=False,
        )
        info = json.loads(result.stdout.strip())
        if (result.returncode != 0 or info.get("status") != "ACL_SECURE"
                or info.get("acl_verified") is not True):
            raise ValueError("LAN_WINDOWS_ACL_NOT_SECURE")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LAN_WINDOWS_ACL_NOT_SECURE") from exc
    return True


def pull_lan_secret_sftp(
    *, host: str, user: str, remote_path: str, known_hosts: Path,
    secret_file: Path = DEFAULT_SECRET_PATH, port: int = 22,
) -> dict[str, object]:
    """Fetch via strict pinned-key SFTP into ACL-protected staging, then import.

    Caller independently verifies the SSH server host-key fingerprint and
    configures *key-only* login before running this command. Passwords are
    never supplied or prompted for.
    """
    peer = _private_ipv4(host)
    if not _SSH_USER.fullmatch(user):
        raise ValueError("LAN_INVALID_SSH_USER")
    if (not isinstance(remote_path, str) or
            not _REMOTE_PATH.fullmatch(remote_path) or
            any(part in ("", ".", "..") for part in remote_path.split("/")[1:])):
        raise ValueError("LAN_UNSAFE_REMOTE_PATH")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("LAN_INVALID_SSH_PORT")
    hosts = known_hosts.absolute()
    destination = secret_file.absolute()
    _reject_linked_path(hosts)
    _reject_linked_path(destination)
    if (not hosts.is_file() or not 1 <= hosts.stat().st_size <= _MAX_KNOWN_HOSTS_BYTES):
        raise ValueError("LAN_PINNED_KNOWN_HOSTS_MISSING")
    if destination == hosts:
        raise ValueError("LAN_UNSAFE_CREDENTIAL_DESTINATION")
    if any(c in str(destination) for c in '"\r\n\x00') or any(
        c in str(hosts) for c in '"\r\n\x00'
    ):
        raise ValueError("LAN_UNSAFE_LOCAL_PATH")
    sftp = shutil.which("sftp")
    if not sftp:
        raise ValueError("LAN_SFTP_TOOL_UNAVAILABLE")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(destination)
    # No credential is present when staging is created. On Windows,
    # make that directory user/SYSTEM/Admin-only before receiving bytes.
    with tempfile.TemporaryDirectory(prefix=".lan-sftp-", dir=destination.parent) as staging:
        stage_dir = Path(staging)
        staging_acl_verified = _secure_windows_path(stage_dir, apply=True)
        received = stage_dir / "credential.txt"
        # SFTP -b - takes commands via stdin; paths are quoted as one batch
        # argument. Remote names are conservatively allowlisted: no shell,
        # wildcard, quotes, backslashes, whitespace or .. components.
        batch = f'get "{remote_path}" "{received.as_posix()}"\n'
        cmd = [
            sftp, "-q", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={hosts}",
            "-o", "UpdateHostKeys=no",
            "-o", "ForwardAgent=no",
            "-o", "ProxyCommand=none",
            "-o", "ProxyJump=none",
            "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1",
            "-o", "PreferredAuthentications=publickey",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-P", str(port), "-b", "-", f"{user}@{peer}",
        ]
        try:
            outcome = subprocess.run(
                cmd, input=batch, text=True, capture_output=True,
                timeout=75, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("LAN_SFTP_TRANSFER_FAILED") from exc
        if outcome.returncode != 0:
            # NEVER echo raw SSH stderr (which may contain a user path).
            raise ValueError("LAN_SFTP_TRANSFER_FAILED")
        _reject_linked_path(received)
        _secret(received)  # Validate exact 256-bit token format/size.
        # The importer's O_EXCL is never allowed to overwrite local secrets.
        result = import_lan_secret(source_file=received, secret_file=destination)
        try:
            target_acl_verified = _secure_windows_path(destination, apply=True)
        except ValueError:
            # Remove only a credential newly created by this operation.
            if result == "IMPORTED":
                destination.unlink(missing_ok=True)
            raise
    return {
        "status": "LAN_SECRET_SFTP_" + result,
        "strict_host_key_checking": True,
        "transport": "SFTP_PUBLIC_KEY_ONLY",
        "windows_staging_acl_verified": staging_acl_verified,
        "windows_secret_acl_verified": target_acl_verified,
        "remote_coordinator_authenticated": False,
        "secret_printed": False,
        "youtube_contacted": False,
        "ready": False,
    }
