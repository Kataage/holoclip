"""Read-only paired setup inspection: loopback coordinator and fake SSH only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_onboard_check as onboard
from holoclip import source_ytdlp_lan_pair_collect as collect
from holoclip.source_ytdlp_lan_coordinator import (
    _CoordinatorServer, _CoordinatorState, create_coordinator_secret,
)
from holoclip.source_ytdlp_lan_pair_evidence import _canonical, _report_key


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(k, raising=False)


@contextmanager
def broker(tmp_path, monkeypatch):
    secret = tmp_path / "local.secret"
    create_coordinator_secret(secret)
    server = _CoordinatorServer(
        ("127.0.0.1", 0), bytes.fromhex(secret.read_text().strip()),
        _CoordinatorState(tmp_path / "state.sqlite3"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setenv(
            "HOLOCLIP_YTDLP_COORDINATOR_URL",
            f"http://127.0.0.1:{server.server_port}",
        )
        monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
        yield secret
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def _doctor(role, *, firewall="EXACT_PEER_ALLOW_RULE_OBSERVED",
            signed="AUTHENTICATED_LOCAL_LAN_COORDINATOR", acl="LAN_CREDENTIAL_ACL_OBSERVED"):
    codes = {
        "signed_coordinator_status": signed,
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": acl,
        "coordinator_task": "RUNNING_OBSERVED" if role == "pc-a" else "NOT_APPLICABLE",
        "coordinator_listener": "LAN_LISTENER_OBSERVED" if role == "pc-a" else "NOT_APPLICABLE",
        "firewall_rule": firewall if role == "pc-a" else "NOT_APPLICABLE",
        "sshd_service": "NOT_APPLICABLE",
        "ssh_host_pin": "HOST_PIN_NOT_SUPPLIED",
    }
    complete = (
        signed == "AUTHENTICATED_LOCAL_LAN_COORDINATOR" and
        acl == "LAN_CREDENTIAL_ACL_OBSERVED" and
        (role == "pc-b" or firewall == "EXACT_PEER_ALLOW_RULE_OBSERVED")
    )
    return {
        "role": role, "status": (
            "LOCAL_SETUP_OBSERVATIONS_COMPLETE" if complete else "LOCAL_SETUP_INCOMPLETE"
        ),
        "ready": False, "production_ready": False, "two_host_verified": False,
        "firewall_effective_policy_verified": False, "config_modified": False,
        "youtube_contacted": False, "checks": {
            key: {"code": value, "action": "untrusted remote raw action"}
            for key, value in codes.items()
        },
    }


def _remote(secret, *, distinct=True):
    report = onboard.inspect_lan_onboard_node(role="pc-b")
    if report["signed_receipt"] is not None and distinct:
        payload = report["signed_receipt"]["payload"]
        payload["observed_peer_tag"] = "f" * 64
        report["signed_receipt"]["signature"] = hmac.new(
            _report_key(bytes.fromhex(secret.read_text().strip())),
            _canonical(payload), hashlib.sha256,
        ).hexdigest()
    return report


def _ssh_setup(tmp_path, monkeypatch, raw_report):
    known = tmp_path / "pinned"
    known.write_text("192.168.1.21 ssh-ed25519 PreverifiedOutOfBandFakeForTests\n")
    recorded = []
    monkeypatch.setattr(collect.shutil, "which", lambda cmd: "mock-ssh")
    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw_report).encode(), stderr=b"")
    monkeypatch.setattr(onboard.subprocess, "run", fake_run)
    return known, recorded


def _cmd(hosts):
    return [
        "examples-ytdlp-lan-onboard-check", "--host", "192.168.1.21",
        "--user", "safeuser", "--known-hosts", str(hosts),
        "--remote-repo", "D:\\AI\\holoclip",
    ]


def test_complete_observations_still_never_claim_real_two_pc_or_ready(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        monkeypatch.setattr(onboard, "inspect_lan_setup", lambda **kwargs: _doctor(kwargs["role"]))
        remote = _remote(secret)
        known, recorded = _ssh_setup(tmp_path, monkeypatch, remote)
        outcome = CliRunner().invoke(cli.app, _cmd(known))
        assert outcome.exit_code == 0, outcome.output
        data = json.loads(outcome.stdout)
        assert data["status"] == "LAN_ONBOARD_OBSERVATIONS_COMPLETE"
        assert data["pc_a"]["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
        assert data["pc_b"]["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
        assert data["remediations"] == []
        assert data["pair"]["status"] == "LAN_PAIR_OBSERVATIONS_MATCH"
        assert data["hardware_identity_verified"] is False
        assert data["windows_firewall_effective_policy_verified"] is False
        assert data["ready"] is False and data["two_host_verified"] is False
        assert data["youtube_contacted"] is False and data["config_modified"] is False
        assert secret.read_text().strip() not in outcome.stdout
        assert str(secret) not in outcome.stdout
        assert "untrusted remote raw action" not in outcome.stdout
        assert not list(tmp_path.glob("*.json"))
        cmd, kwargs = recorded[0]
        script = base64.b64decode(cmd[-1]).decode("utf-16le")
        assert "uv run --offline --no-sync holoclip examples-ytdlp-lan-onboard-node" in script
        assert "examples-ytdlp-lan-pair-observe" not in script
        assert "StrictHostKeyChecking=yes" in cmd
        assert kwargs["timeout"] == 45


def test_local_and_remote_deficits_are_aggregated_without_secret(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        def doctor(**kwargs):
            if kwargs["role"] == "pc-a":
                assert kwargs["peer_ip"] == "192.168.1.21"
                return _doctor("pc-a", firewall="BROAD_INBOUND_ALLOW_RULE_OBSERVED")
            return _doctor("pc-b", acl="LAN_CREDENTIAL_ACL_UNVERIFIED")
        monkeypatch.setattr(onboard, "inspect_lan_setup", doctor)
        hosts, _ = _ssh_setup(tmp_path, monkeypatch, _remote(secret))
        outcome = CliRunner().invoke(cli.app, _cmd(hosts))
        assert outcome.exit_code == 6
        data = json.loads(outcome.stdout)
        assert data["status"] == "LAN_ONBOARD_SETUP_INCOMPLETE"
        codes = {(x["role"], x["code"]) for x in data["remediations"]}
        assert ("pc-a", "BROAD_INBOUND_ALLOW_RULE_OBSERVED") in codes
        assert ("pc-b", "LAN_CREDENTIAL_ACL_UNVERIFIED") in codes
        assert data["pair"]["status"] == "LAN_PAIR_OBSERVATIONS_MATCH"
        assert data["ready"] is False and data["two_host_verified"] is False
        assert secret.read_text().strip() not in outcome.stdout


def test_remote_unconfigured_still_emits_negative_node_json(tmp_path, monkeypatch):
    result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-onboard-node"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["role"] == "pc-b"
    assert report["signed_receipt"] is None
    assert report["diagnostic"]["status"] == "LOCAL_SETUP_INCOMPLETE"
    assert report["ready"] is False


def test_same_peer_is_not_positive_two_pc_observation(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        monkeypatch.setattr(onboard, "inspect_lan_setup", lambda **kwargs: _doctor(kwargs["role"]))
        known, _ = _ssh_setup(tmp_path, monkeypatch, _remote(secret, distinct=False))
        out = CliRunner().invoke(cli.app, _cmd(known))
        assert out.exit_code == 6
        report = json.loads(out.stdout)
        assert report["status"] == "LAN_ONBOARD_PAIR_INCONCLUSIVE"
        assert report["pair"]["distinct_tcp_peers_observed"] is False
        assert report["two_host_verified"] is False


@pytest.mark.parametrize("mutation", [
    "wrong_version", "wrong_role", "untrusted_check", "spoofed_complete",
    "bad_signature", "oversized", "not_json", "ssh_failed", "unexpected_field",
])
def test_bad_remote_reports_never_claim_success(tmp_path, monkeypatch, mutation):
    with broker(tmp_path, monkeypatch) as secret:
        monkeypatch.setattr(onboard, "inspect_lan_setup", lambda **kwargs: _doctor(kwargs["role"]))
        remote = _remote(secret)
        if mutation == "wrong_version":
            remote["version"] = "malicious"
        elif mutation == "wrong_role":
            remote["role"] = "pc-a"
        elif mutation == "untrusted_check":
            remote["diagnostic"]["checks"]["firewall_rule"] = "BYPASS"
        elif mutation == "spoofed_complete":
            remote["diagnostic"]["checks"]["credential_acl"] = "LAN_CREDENTIAL_ACL_UNVERIFIED"
        elif mutation == "bad_signature":
            remote["signed_receipt"]["signature"] = "0" * 64
        elif mutation == "unexpected_field":
            remote["exfiltrated"] = "fake"
        hosts, captured = _ssh_setup(tmp_path, monkeypatch, remote)
        def fake_run(cmd, **kwargs):
            captured.append((cmd, kwargs))
            if mutation == "ssh_failed":
                return SimpleNamespace(returncode=255, stdout=b"", stderr=secret.read_bytes())
            if mutation == "oversized":
                return SimpleNamespace(returncode=0, stdout=b"x" * 17000, stderr=b"")
            if mutation == "not_json":
                return SimpleNamespace(returncode=0, stdout=b"{broken", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=json.dumps(remote).encode(), stderr=b"")
        monkeypatch.setattr(onboard.subprocess, "run", fake_run)
        out = CliRunner().invoke(cli.app, _cmd(hosts))
        assert out.exit_code == 6, (mutation, out.output)
        report = json.loads(out.stdout)
        assert report["ready"] is False
        assert report["status"] == "LAN_ONBOARD_CHECK_REJECTED"
        assert secret.read_text().strip() not in out.stdout
        assert str(secret) not in out.stdout
        assert "exfiltrated" not in out.stdout


def test_disallowed_remote_command_or_loopback_rejected_before_ssh(tmp_path, monkeypatch):
    known = tmp_path / "hosts"
    known.write_text("entry\n")
    monkeypatch.setattr(collect.shutil, "which", lambda name: "ssh")
    with pytest.raises(ValueError, match="INVALID_REMOTE_PROBE"):
        collect._pinned_ssh_command(
            host="192.168.1.21", user="safeuser", known_hosts=known,
            remote_repo="D:\\AI\\holoclip", port=22, remote_probe="; del C:",
        )
    with pytest.raises(ValueError, match="LOOPBACK"):
        onboard.check_lan_onboarding(
            host="127.0.0.1", user="safeuser", known_hosts=known,
            remote_repo="D:\\AI\\holoclip",
        )


def test_remote_incomplete_receipt_does_not_force_failure_of_diagnostics(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as _secret:
        monkeypatch.setattr(onboard, "inspect_lan_setup", lambda **kwargs: _doctor(kwargs["role"]))
        remote = onboard.inspect_lan_onboard_node(role="pc-b")
        remote["signed_receipt"] = None
        hosts, _ = _ssh_setup(tmp_path, monkeypatch, remote)
        out = CliRunner().invoke(cli.app, _cmd(hosts))
        assert out.exit_code == 6
        result = json.loads(out.stdout)
        assert result["pair"] is None
        assert result["status"] == "LAN_ONBOARD_PAIR_INCONCLUSIVE"


def test_yt_dlp_never_started_and_remote_does_not_transfer_credentials(
    tmp_path, monkeypatch,
):
    with broker(tmp_path, monkeypatch) as secret:
        monkeypatch.setattr(onboard, "inspect_lan_setup", lambda **kwargs: _doctor(kwargs["role"]))
        from holoclip.providers.source import yt_dlp
        monkeypatch.setattr(
            yt_dlp, "_run_process", lambda *_a, **_k: pytest.fail("must not call yt-dlp"),
        )
        hosts, seen = _ssh_setup(tmp_path, monkeypatch, _remote(secret))
        report = onboard.check_lan_onboarding(
            host="192.168.1.21", user="safeuser",
            known_hosts=hosts, remote_repo="D:\\AI\\holoclip",
        )
        assert report["youtube_contacted"] is False
        assert seen and all("local.secret" not in str(part) for part in seen[0][0])
        assert secret.read_text().strip() not in json.dumps(report)
