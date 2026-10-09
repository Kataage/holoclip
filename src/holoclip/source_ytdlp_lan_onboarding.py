"""Offline LAN credential import and safe authenticated connectivity diagnosis.

Secret transport itself MUST be trusted (e.g. removable media or an independently
verified encrypted connection); the coordinator's HTTP channel cannot bootstrap trust.
No YouTube/video, remote credential delivery, or file-content logging occurs here.
"""
from __future__ import annotations

import os
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_SECRET_PATH, LanCoordinatorClient, _reject_linked_path, _secret,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

_REMOTE_CODES = frozenset({"AVAILABLE", "ACTIVE", "WAIT_GAP", "COOLDOWN"})
_SAFE_FAILURES = frozenset({
    "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID",
    "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE",
    "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED",
})


def import_lan_secret(
    *, source_file: Path, secret_file: Path = DEFAULT_SECRET_PATH,
) -> str:
    """Import only from a caller-provided trusted local file, never overwrite.

    A source file is NOT securely transferred by this function. File ACLs on
    Windows must additionally be reviewed by the owner; Python's 0o600 mode
    is not an adequate proof of Windows NTFS ACL isolation.
    """
    source = source_file.absolute()
    target = secret_file.absolute()
    _reject_linked_path(source)
    _reject_linked_path(target)
    if source == target:
        raise ValueError("Source and destination must differ")
    credential = _secret(source)  # Validate before writing any destination.
    if target.exists():
        if _secret(target) == credential:
            return "ALREADY_IMPORTED"
        raise ValueError("Destination credential already exists and differs")
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(target)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Destination credential already exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(credential.hex().encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Only remove our own failed creation; an existing target is never touched.
        target.unlink(missing_ok=True)
        raise
    return "IMPORTED"


def diagnose_lan_client() -> dict[str, object]:
    """Read-only client probe; only the coordinator's signed *status* RPC.

    This intentionally never requests a lease, starts yt-dlp, or checks
    whether other PCs are enrolled. Server status may initialize SQLite.
    """
    report: dict[str, object] = {
        "status": "NOT_CONFIGURED",
        "configured": False,
        "authenticated": False,
        "remote_gate_status": None,
        "youtube_contacted": False,
        "two_host_verified": False,
        "shared_ip_coordination_verified": False,
        "ready": False,
    }
    try:
        client = LanCoordinatorClient.from_env()
    except LocalYtDlpDeferred as exc:
        code = str(exc)
        report["status"] = code if code in _SAFE_FAILURES else "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE"
        return report
    if client is None:
        return report
    report["configured"] = True
    try:
        reply = client.status()
    except LocalYtDlpDeferred as exc:
        code = str(exc)
        report["status"] = code if code in _SAFE_FAILURES else "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE"
        return report
    if reply.get("ok") is not True or reply.get("code") not in _REMOTE_CODES:
        report["status"] = "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED"
        return report
    report.update(
        status="AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        authenticated=True,
        remote_gate_status=reply["code"],
    )
    return report
