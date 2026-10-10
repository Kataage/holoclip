"""One-command SSH-based cross-PC LAN receipt collection and comparison.

PC-A observes its local enrolled coordinator and uses pinned, key-only SSH
to run PC-B's read-only receipt command. No secret/report file transfer,
SSH server changes, media work, lease requests or blind host-key trust.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _private_ipv4, _reject_linked_path,
)
from holoclip.source_ytdlp_lan_pair_evidence import (
    _MAX_EVIDENCE_BYTES, _signed_evidence, compare_lan_pair_reports,
)
from holoclip.source_ytdlp_lan_ssh_transfer import _SSH_USER
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

_MAX_REMOTE_REPO_CHARS = 512


def _windows_remote_repo(value: str) -> str:
    """Require an absolute Windows drive path. Encode it, never shell-interpolate it."""
    if not isinstance(value, str) or not 3 <= len(value) <= _MAX_REMOTE_REPO_CHARS:
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    if any(ord(char) < 32 or char in '*?"<>|' for char in value):
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    # PureWindowsPath silently normalizes "." and redundant separators.
    # Inspect the original components first, before canonicalization.
    parts = value.replace("/", "\\").split("\\")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts[1:]):
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    parsed = PureWindowsPath(value)
    if any(part in (".", "..") or part.endswith((".", " ")) for part in parsed.parts[1:]):
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    if ":" in value[2:]:
        raise ValueError("LAN_PAIR_UNSAFE_REMOTE_REPOSITORY")
    return str(parsed)


def _pinned_ssh_command(*, host: str, user: str, known_hosts: Path,
                        remote_repo: str, port: int,
                        remote_probe: str = "pair") -> list[str]:
    # Never permit a caller-supplied command fragment through the SSH shell.
    if remote_probe not in {"pair", "onboard"}:
        raise ValueError("LAN_PAIR_INVALID_REMOTE_PROBE")
    peer = _private_ipv4(host)
    if not _SSH_USER.fullmatch(user):
        raise ValueError("LAN_PAIR_INVALID_SSH_USER")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("LAN_PAIR_INVALID_SSH_PORT")
    repo = _windows_remote_repo(remote_repo)
    pinned = known_hosts.absolute()
    _reject_linked_path(pinned)
    if not pinned.is_file() or not 1 <= pinned.stat().st_size <= 1024 * 1024:
        raise ValueError("LAN_PAIR_PINNED_HOST_KEY_REQUIRED")
    if any(char in str(pinned) for char in '"\r\n\x00'):
        raise ValueError("LAN_PAIR_UNSAFE_KNOWN_HOSTS_PATH")
    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("LAN_PAIR_SSH_CLIENT_NOT_FOUND")

    # The remote shell receives only ASCII base64, never a user-supplied
    # shell fragment. Windows PS single-quoted strings escape apostrophes.
    quoted_repo = repo.replace("'", "''")
    # The onboarding probe uses offline uv and has no credential or
    # arbitrary shell arguments. The established pair probe is unchanged.
    command = (
        "& uv run --no-sync holoclip examples-ytdlp-lan-pair-observe --role pc-b; "
        if remote_probe == "pair" else
        "& uv run --offline --no-sync holoclip examples-ytdlp-lan-onboard-node; "
    )
    remote_script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Set-Location -LiteralPath '{quoted_repo}'; "
        + command + "exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(remote_script.encode("utf-16le")).decode("ascii")
    return [
        ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={pinned}", "-o", "GlobalKnownHostsFile=none",
        "-o", "UpdateHostKeys=no", "-o", "IdentitiesOnly=yes",
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes",
        "-o", "ControlMaster=no", "-o", "RequestTTY=no",
        "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
        "-p", str(port), f"{user}@{peer}",
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]


def collect_lan_pair_over_ssh(
    *, host: str, user: str, known_hosts: Path, remote_repo: str,
    ssh_port: int = 22,
) -> dict[str, object]:
    """Live status from PC-A and SSH-pinned PC-B; compare entirely in memory.

    Both Windows PCs must have previously enrolled in the same coordinator;
    SSH server/key/host verification setup is an explicit prerequisite.
    """
    cmd = _pinned_ssh_command(
        host=host, user=user, known_hosts=known_hosts,
        remote_repo=remote_repo, port=ssh_port,
    )
    client = LanCoordinatorClient.from_env()
    if client is None:
        raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID")
    local = _signed_evidence(client=client, role="pc-a")
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=40, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LAN_PAIR_REMOTE_SSH_FAILED") from exc
    if result.returncode != 0:
        # Raw SSH errors are never included in our own diagnostic payload.
        raise ValueError("LAN_PAIR_REMOTE_SSH_FAILED")
    if not 1 <= len(result.stdout) <= _MAX_EVIDENCE_BYTES:
        raise ValueError("LAN_PAIR_REMOTE_REPORT_INVALID")
    try:
        remote = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("LAN_PAIR_REMOTE_REPORT_INVALID") from exc

    checked = compare_lan_pair_reports(
        pc_a=local, pc_b=remote, shared_secret=client.secret,
    )
    checked.update(
        collection="PINNED_PUBLIC_KEY_SSH",
        ssh_transport_completed=True,
        pc_b_report_saved_to_disk=False,
        pc_a_report_saved_to_disk=False,
        local_and_remote_status_observed=True,
        remote_host_fingerprint_independently_verified=False,
        independent_physical_host_attestation=False,
        ready=False,
        two_host_verified=False,
    )
    return checked
