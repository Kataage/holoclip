"""Locally signed, expiring PC-A/PC-B LAN coordinator pairing observations.

Status probes are strictly read-only with respect to access leases. Receipts
verify a common durable coordinator *state* and distinguish observed TCP peer
addresses, not cryptographic physical-machine identity. No media calls.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _reject_linked_path,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

PAIR_VERSION = "holoclip-lan-pair-evidence-v1"
_MAX_EVIDENCE_BYTES = 4096
_MAX_AGE_SECONDS = 900
_MAX_FUTURE_SKEW_SECONDS = 90
_ALLOWED_GATE = frozenset({"AVAILABLE", "ACTIVE", "WAIT_GAP", "COOLDOWN"})
_REQUIRED_PAYLOAD = frozenset({
    "version", "role", "observed_at", "nonce",
    "coordinator_instance_id", "observed_peer_tag", "gate_code",
})
_REQUIRED_ENVELOPE = frozenset({"payload", "signature"})


def _report_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"holoclip-pair-report-v1", hashlib.sha256).digest()


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{" + str(length) + "}", value) is not None


def _validated_payload(payload: object, *, role: str, now: int) -> dict:
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_PAYLOAD:
        raise ValueError("LAN_PAIR_INVALID_REPORT")
    if payload["version"] != PAIR_VERSION or payload["role"] != role:
        raise ValueError("LAN_PAIR_WRONG_ROLE")
    stamp = payload["observed_at"]
    if type(stamp) is not int or now - stamp > _MAX_AGE_SECONDS or stamp - now > _MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("LAN_PAIR_STALE_REPORT")
    if (not _hex(payload["nonce"], 32) or
            not _hex(payload["coordinator_instance_id"], 64) or
            not _hex(payload["observed_peer_tag"], 64) or
            payload["gate_code"] not in _ALLOWED_GATE):
        raise ValueError("LAN_PAIR_INVALID_REPORT")
    return payload


def _signed_evidence(*, client: LanCoordinatorClient, role: str) -> dict:
    reply = client.status()  # Signed remote status; never reserve a lease.
    if (reply.get("ok") is not True or reply.get("code") not in _ALLOWED_GATE or
            not _hex(reply.get("coordinator_instance_id"), 64) or
            not _hex(reply.get("observed_peer_tag"), 64)):
        raise ValueError("LAN_PAIR_BROKER_IDENTITY_UNAVAILABLE")
    payload = {
        "version": PAIR_VERSION,
        "role": role,
        "observed_at": int(time.time()),
        "nonce": secrets.token_hex(16),
        "coordinator_instance_id": reply["coordinator_instance_id"],
        "observed_peer_tag": reply["observed_peer_tag"],
        "gate_code": reply["code"],
    }
    signature = hmac.new(_report_key(client.secret), _canonical(payload), hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": signature}


def export_lan_pair_evidence(*, role: str, output: Path) -> dict[str, object]:
    """Create one owner-held signed observation; never overwrite an existing report."""
    if role not in {"pc-a", "pc-b"}:
        raise ValueError("LAN_PAIR_INVALID_ROLE")
    client = LanCoordinatorClient.from_env()
    if client is None:
        raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID")
    report = _signed_evidence(client=client, role=role)
    destination = output.absolute()
    _reject_linked_path(destination)
    raw = _canonical(report) + b"\n"
    if len(raw) > _MAX_EVIDENCE_BYTES:
        raise ValueError("LAN_PAIR_INVALID_REPORT")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(destination)
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(raw)
    return {
        "status": "LAN_PAIR_EVIDENCE_SAVED",
        "role": role,
        "remote_status_authenticated": True,
        "report_file_contains_secret": False,
        "lease_requested": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "ready": False,
    }


def _read_evidence(path: Path, *, role: str, key: bytes, now: int) -> dict:
    file = path.absolute()
    _reject_linked_path(file)
    if not file.is_file() or not 1 <= file.stat().st_size <= _MAX_EVIDENCE_BYTES:
        raise ValueError("LAN_PAIR_MISSING_REPORT")
    try:
        item = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(item, dict) or set(item) != _REQUIRED_ENVELOPE:
            raise ValueError("LAN_PAIR_INVALID_REPORT")
        payload = _validated_payload(item["payload"], role=role, now=now)
        if not _hex(item["signature"], 64) or not hmac.compare_digest(
            item["signature"], hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest(),
        ):
            raise ValueError("LAN_PAIR_BAD_SIGNATURE")
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError("LAN_PAIR_INVALID_REPORT") from exc
    return payload


def compare_lan_pair_evidence(*, pc_a: Path, pc_b: Path) -> dict[str, object]:
    """Verify both owner-held reports offline using the enrolled shared secret.

    Reports are co-signed by a shared *client* key. An authorized holder can
    forge them, so they are not hardware attestation or independently
    verified two-host onboarding.
    """
    if pc_a.absolute() == pc_b.absolute():
        raise ValueError("LAN_PAIR_DUPLICATE_REPORT")
    client = LanCoordinatorClient.from_env()
    if client is None:
        raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID")
    key = _report_key(client.secret)
    now = int(time.time())
    a = _read_evidence(pc_a, role="pc-a", key=key, now=now)
    b = _read_evidence(pc_b, role="pc-b", key=key, now=now)
    if a["nonce"] == b["nonce"]:
        raise ValueError("LAN_PAIR_DUPLICATE_REPORT")
    same = hmac.compare_digest(a["coordinator_instance_id"], b["coordinator_instance_id"])
    different = not hmac.compare_digest(a["observed_peer_tag"], b["observed_peer_tag"])
    return {
        "status": ("LAN_PAIR_OBSERVATIONS_MATCH" if same and different
                   else "LAN_PAIR_OBSERVATIONS_INCONCLUSIVE"),
        "same_coordinator_state_observed": same,
        "distinct_tcp_peers_observed": different,
        "reports_authenticated": True,
        "reports_recent": True,
        "physical_two_host_identity_verified": False,
        "coordinator_currently_online_verified": False,
        "global_pacing_runtime_verified": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "ready": False,
    }
