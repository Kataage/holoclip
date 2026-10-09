"""Pair evidence tests use signed loopback status and synthetic distinct peers.

No GitHub-hosted media, no physical Windows PC, no network beyond loopback.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _CoordinatorServer, _CoordinatorState,
    create_coordinator_secret,
)
from holoclip.source_ytdlp_lan_pair_evidence import (
    PAIR_VERSION, _report_key, _canonical,
    compare_lan_pair_evidence, export_lan_pair_evidence,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred


@pytest.fixture(autouse=True)
def local_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)


@contextmanager
def broker(tmp_path, *, state=None, secret=None):
    secret = secret or (tmp_path / "secret")
    if not secret.exists():
        create_coordinator_secret(secret)
    state = state or (tmp_path / "state.sqlite3")
    server = _CoordinatorServer(
        ("127.0.0.1", 0), bytes.fromhex(secret.read_text().strip()),
        _CoordinatorState(state),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", secret, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _enroll(monkeypatch, url, secret):
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", url)
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))


def test_signed_state_identity_durable_and_isolated_from_secret_copy(tmp_path):
    with broker(tmp_path) as (url, secret, state):
        client = LanCoordinatorClient(url, secret_file=secret)
        reply = client.status()
        broker_id = reply["coordinator_instance_id"]
        peer_tag = reply["observed_peer_tag"]
        assert len(broker_id) == len(peer_tag) == 64
        assert all(char in "0123456789abcdef" for char in broker_id)
        assert client.status()["coordinator_instance_id"] == broker_id
        assert client.status()["observed_peer_tag"] == peer_tag
        assert url not in json.dumps(reply)
        assert secret.read_text().strip() not in json.dumps(reply)
        # A status RPC must not claim a lease or advance the 20-second gate.
        with sqlite3.connect(state) as db:
            assert db.execute("SELECT next_at, active_until FROM access").fetchone() == (0, 0)
    with broker(tmp_path, state=state, secret=secret) as (url2, _, _):
        assert LanCoordinatorClient(url2, secret_file=secret).status()["coordinator_instance_id"] == broker_id
    with broker(tmp_path / "fresh", secret=secret) as (url3, _, _):
        assert LanCoordinatorClient(url3, secret_file=secret).status()["coordinator_instance_id"] != broker_id


def test_real_loopback_export_pair_reports_cannot_claim_two_distinct_peers(tmp_path, monkeypatch):
    with broker(tmp_path) as (url, secret, _state):
        _enroll(monkeypatch, url, secret)
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        result_a = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-export", "--role", "pc-a", "--output", str(a),
        ])
        result_b = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-export", "--role", "pc-b", "--output", str(b),
        ])
        assert result_a.exit_code == result_b.exit_code == 0, (result_a.output, result_b.output)
        assert secret.read_text().strip() not in a.read_text()
        assert secret.read_text().strip() not in b.read_text()
        assert str(secret) not in a.read_text() + b.read_text()
        assert json.loads(a.read_text())["payload"]["version"] == PAIR_VERSION
        assert json.loads(a.read_text())["payload"]["observed_peer_tag"] == json.loads(b.read_text())["payload"]["observed_peer_tag"]
        outcome = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-verify", "--pc-a", str(a), "--pc-b", str(b),
        ])
        assert outcome.exit_code == 6
        report = json.loads(outcome.stdout)
        assert report["reports_authenticated"] is True
        assert report["same_coordinator_state_observed"] is True
        assert report["distinct_tcp_peers_observed"] is False
        assert report["two_host_verified"] is False
        assert report["ready"] is False
        assert url not in outcome.stdout
        assert secret.read_text().strip() not in outcome.stdout
        assert all("lease_token" not in p.read_text() for p in (a, b))


def test_synthetic_distinct_peer_tags_match_but_not_host_attestation(tmp_path, monkeypatch):
    with broker(tmp_path) as (url, secret, _state):
        _enroll(monkeypatch, url, secret)
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        export_lan_pair_evidence(role="pc-a", output=a)
        export_lan_pair_evidence(role="pc-b", output=b)
        # Test-only: synthesize the signed content another TCP peer *would*
        # receive; this never counts as real 2-PC network verification.
        payload_b = json.loads(b.read_text())["payload"]
        payload_b["observed_peer_tag"] = "f" * 64
        data = {
            "payload": payload_b,
            "signature": __import__("hmac").new(
                _report_key(bytes.fromhex(secret.read_text().strip())),
                _canonical(payload_b),
                __import__("hashlib").sha256,
            ).hexdigest(),
        }
        b.write_text(json.dumps(data))
        report = compare_lan_pair_evidence(pc_a=a, pc_b=b)
        assert report["status"] == "LAN_PAIR_OBSERVATIONS_MATCH"
        assert report["distinct_tcp_peers_observed"] is True
        assert report["same_coordinator_state_observed"] is True
        assert report["physical_two_host_identity_verified"] is False
        assert report["two_host_verified"] is False
        assert report["global_pacing_runtime_verified"] is False
        assert report["ready"] is False


def test_mismatched_coordinator_state_cannot_match_even_with_same_secret(tmp_path, monkeypatch):
    with broker(tmp_path / "a") as (url_a, secret, _):
        _enroll(monkeypatch, url_a, secret)
        export_lan_pair_evidence(role="pc-a", output=tmp_path / "a.json")
    with broker(tmp_path / "b", secret=secret) as (url_b, _, _):
        _enroll(monkeypatch, url_b, secret)
        export_lan_pair_evidence(role="pc-b", output=tmp_path / "b.json")
    report = compare_lan_pair_evidence(pc_a=tmp_path / "a.json", pc_b=tmp_path / "b.json")
    assert report["status"] == "LAN_PAIR_OBSERVATIONS_INCONCLUSIVE"
    assert report["same_coordinator_state_observed"] is False
    assert report["two_host_verified"] is False


@pytest.mark.parametrize("mutation", ["signature", "role", "stale", "future", "unknown", "truncated"])
def test_tampered_or_stale_reports_rejected(tmp_path, monkeypatch, mutation):
    with broker(tmp_path) as (url, secret, _):
        _enroll(monkeypatch, url, secret)
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        export_lan_pair_evidence(role="pc-a", output=a)
        export_lan_pair_evidence(role="pc-b", output=b)
        doc = json.loads(b.read_text())
        if mutation == "signature":
            doc["signature"] = "0" * 64
        elif mutation == "role":
            doc["payload"]["role"] = "pc-a"
        elif mutation == "stale":
            doc["payload"]["observed_at"] = int(time.time()) - 901
        elif mutation == "future":
            doc["payload"]["observed_at"] = int(time.time()) + 91
        elif mutation == "unknown":
            doc["payload"]["unexpected"] = "should fail closed"
        elif mutation == "truncated":
            b.write_text("{broken")
        if mutation != "truncated":
            b.write_text(json.dumps(doc))
        with pytest.raises(ValueError, match="LAN_PAIR_"):
            compare_lan_pair_evidence(pc_a=a, pc_b=b)
        result = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-verify", "--pc-a", str(a), "--pc-b", str(b),
        ])
        assert result.exit_code == 6
        assert json.loads(result.stdout)["ready"] is False
        assert secret.read_text().strip() not in result.stdout


def test_duplicate_and_oversized_reports_fail_closed(tmp_path, monkeypatch):
    with broker(tmp_path) as (url, secret, _):
        _enroll(monkeypatch, url, secret)
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        export_lan_pair_evidence(role="pc-a", output=a)
        with pytest.raises(ValueError, match="DUPLICATE"):
            compare_lan_pair_evidence(pc_a=a, pc_b=a)
        b.write_bytes(b"x" * 5000)
        with pytest.raises(ValueError, match="MISSING_REPORT"):
            compare_lan_pair_evidence(pc_a=a, pc_b=b)
        with pytest.raises(FileExistsError):
            export_lan_pair_evidence(role="pc-a", output=a)


def test_unconfigured_or_broker_downtime_never_creates_positive_receipt(
    tmp_path, monkeypatch,
):
    file = tmp_path / "a.json"
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pair-export", "--role", "pc-a", "--output", str(file),
    ])
    assert result.exit_code == 6
    assert not file.exists()
    with broker(tmp_path) as (url, secret, _):
        _enroll(monkeypatch, url, secret)
        export_lan_pair_evidence(role="pc-a", output=file)
    unavailable = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pair-export", "--role", "pc-b",
        "--output", str(tmp_path / "b.json"),
    ])
    assert unavailable.exit_code == 6
    assert not (tmp_path / "b.json").exists()
    assert "COORDINATOR_UNAVAILABLE" in unavailable.stdout
    # The real yt-dlp provider must not start an external process after
    # coordinator loss: existing fail-closed policy is also exercised here.
    monkeypatch.setattr(
        yt_dlp, "_run_process",
        lambda *_a, **_kw: pytest.fail("YouTube tool must never start"),
    )
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", manage_updates=False, pacing_path=tmp_path / "pacing.sqlite3",
    )
    with pytest.raises(yt_dlp.YtDlpError, match="COORDINATOR_UNAVAILABLE"):
        provider.probe("https://www.youtube.com/watch?v=AAAAAAAAAAA")
    assert not (tmp_path / "pacing.sqlite3").exists()


def test_report_symlink_rejected(tmp_path, monkeypatch):
    with broker(tmp_path) as (url, secret, _):
        _enroll(monkeypatch, url, secret)
        path = tmp_path / "report"
        export_lan_pair_evidence(role="pc-a", output=path)
        link = tmp_path / "linked"
        try:
            link.symlink_to(path)
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(ValueError, match="path"):
            compare_lan_pair_evidence(pc_a=link, pc_b=tmp_path / "missing")
        with pytest.raises(ValueError, match="path"):
            export_lan_pair_evidence(role="pc-b", output=link)


def test_client_request_signatures_reject_unrecognized_status_values(tmp_path, monkeypatch):
    with broker(tmp_path) as (url, secret, _):
        _enroll(monkeypatch, url, secret)
        monkeypatch.setattr(LanCoordinatorClient, "status", lambda _self: {
            "ok": True, "code": "AVAILABLE",
            "coordinator_instance_id": "0" * 64,
            "observed_peer_tag": "NOT_VALID",
        })
        with pytest.raises(ValueError, match="IDENTITY_UNAVAILABLE"):
            export_lan_pair_evidence(role="pc-b", output=tmp_path / "bad.json")
        assert not (tmp_path / "bad.json").exists()
