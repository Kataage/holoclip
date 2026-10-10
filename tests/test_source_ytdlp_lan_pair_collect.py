"""One-command Windows LAN paired observation, using loopback and mock SSH only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_ytdlp_lan_coordinator import (
    _CoordinatorServer, _CoordinatorState, create_coordinator_secret,
)
from holoclip.source_ytdlp_lan_pair_evidence import (
    _canonical, _report_key, observe_lan_pair_evidence,
)
from holoclip import source_ytdlp_lan_pair_collect as collect


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


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


def _remote_report(secret, *, different_peer=True, role="pc-b"):
    report = observe_lan_pair_evidence(role="pc-b")
    report["payload"]["role"] = role
    if different_peer:
        report["payload"]["observed_peer_tag"] = "f" * 64
    report["signature"] = hmac.new(
        _report_key(bytes.fromhex(secret.read_text().strip())),
        _canonical(report["payload"]), hashlib.sha256,
    ).hexdigest()
    return report


def _prepare(tmp_path, monkeypatch, report):
    known_hosts = tmp_path / "pc-b-known-hosts"
    known_hosts.write_text("192.168.1.21 ssh-ed25519 FakePreverifiedKeyForTest\n")
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(report).encode("utf-8"), stderr=b"")

    monkeypatch.setattr(collect.shutil, "which", lambda name: "mock-ssh" if name == "ssh" else None)
    monkeypatch.setattr(collect.subprocess, "run", fake_run)
    return known_hosts, captured


def _args(known_hosts, *, repo="D:\\AI\\holoclip"):
    return {
        "host": "192.168.1.21", "user": "safeuser",
        "known_hosts": known_hosts, "remote_repo": repo,
    }


def test_one_command_collects_live_pair_without_report_files(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        known_hosts, captured = _prepare(tmp_path, monkeypatch, _remote_report(secret))
        result = collect.collect_lan_pair_over_ssh(**_args(known_hosts))
        assert result["status"] == "LAN_PAIR_OBSERVATIONS_MATCH"
        assert result["same_coordinator_state_observed"]
        assert result["distinct_tcp_peers_observed"]
        assert result["collection"] == "PINNED_PUBLIC_KEY_SSH"
        assert result["pc_a_report_saved_to_disk"] is False
        assert result["pc_b_report_saved_to_disk"] is False
        assert result["remote_host_fingerprint_independently_verified"] is False
        assert result["two_host_verified"] is False
        assert result["physical_two_host_identity_verified"] is False
        assert result["ready"] is False
        assert not list(tmp_path.glob("*.json"))
        assert secret.read_text().strip() not in json.dumps(result)
        cmd, options = captured[0]
        assert cmd[0] == "mock-ssh"
        assert cmd[cmd.index("-p") + 1] == "22"
        assert cmd[cmd.index("-o", cmd.index("StrictHostKeyChecking=yes") - 1) + 1] == "StrictHostKeyChecking=yes"
        for option in (
            "BatchMode=yes", "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=none", "UpdateHostKeys=no",
            "IdentitiesOnly=yes", "ForwardAgent=no", "ForwardX11=no",
            "ProxyCommand=none", "ProxyJump=none",
            "PreferredAuthentications=publickey", "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
        ):
            assert option in cmd
        assert f"UserKnownHostsFile={known_hosts}" in cmd
        assert cmd[-5:-1] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        source = base64.b64decode(cmd[-1]).decode("utf-16le")
        assert "Set-Location -LiteralPath 'D:\\AI\\holoclip'" in source
        assert "uv run --no-sync holoclip examples-ytdlp-lan-pair-observe --role pc-b" in source
        assert source.endswith("exit $LASTEXITCODE")
        assert "local.secret" not in source
        assert options["timeout"] == 40
        assert options["capture_output"] is True


def test_cli_observe_emits_only_signed_receipt_without_files(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-pair-observe", "--role", "pc-b"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["payload"]["role"] == "pc-b"
        assert re.fullmatch("[a-f0-9]{64}", report["signature"])
        assert secret.read_text().strip() not in result.stdout
        assert str(secret) not in result.stdout
        assert not list(tmp_path.glob("*.json"))


def test_same_network_peer_cannot_claim_cross_pc_distinction(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        hosts, _captured = _prepare(
            tmp_path, monkeypatch, _remote_report(secret, different_peer=False),
        )
        result = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-collect", "--host", "192.168.1.21",
            "--user", "safeuser", "--known-hosts", str(hosts),
            "--remote-repo", "D:\\AI\\holoclip",
        ])
        assert result.exit_code == 6
        data = json.loads(result.stdout)
        assert data["status"] == "LAN_PAIR_OBSERVATIONS_INCONCLUSIVE"
        assert data["distinct_tcp_peers_observed"] is False
        assert data["two_host_verified"] is False


def test_remote_shell_command_never_embeds_untrusted_text_as_shell_code(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts"
    hosts.write_text("pinned\n")
    monkeypatch.setattr(collect.shutil, "which", lambda name: "ssh")
    script_path = "D:\\AI\\O'Brien's directory\\holoclip"
    cmd = collect._pinned_ssh_command(**_args(hosts, repo=script_path), port=2222)
    body = base64.b64decode(cmd[-1]).decode("utf-16le")
    assert "O''Brien''s directory" in body
    assert "O'Brien" not in body
    assert script_path not in " ".join(cmd)
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "2222"
    assert cmd[-1].isascii()
    assert all("local.secret" not in segment for segment in cmd)


@pytest.mark.parametrize("bad, field", [
    ("8.8.8.8", "host"),
    ("hostname.local", "host"),
    ("evil;cat", "user"),
    ("name@domain", "user"),
    ("relative/path", "remote_repo"),
    ("\\\\server\\share", "remote_repo"),
    ("D:\\AI\\..\\outside", "remote_repo"),
    ('D:\\AI\\"; Invoke-Expression "evil"', "remote_repo"),
    ("D:\\AI\\bad\npath", "remote_repo"),
    ("D:\\AI\\bad*name", "remote_repo"),
])
def test_invalid_inputs_never_start_ssh(tmp_path, monkeypatch, bad, field):
    hosts = tmp_path / "hosts"
    hosts.write_text("pinned\n")
    seen = []
    monkeypatch.setattr(collect.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(collect.subprocess, "run", lambda *_a, **_kw: seen.append(1))
    args = _args(hosts)
    args[field] = bad
    with pytest.raises(ValueError):
        collect._pinned_ssh_command(**args, port=22)
    assert seen == []


def test_missing_known_hosts_or_ssh_client_never_falls_back(tmp_path, monkeypatch):
    hosts = tmp_path / "missing"
    monkeypatch.setattr(collect.shutil, "which", lambda name: "ssh")
    with pytest.raises(ValueError, match="PINNED_HOST_KEY_REQUIRED"):
        collect._pinned_ssh_command(**_args(hosts), port=22)
    hosts.write_text("known\n")
    monkeypatch.setattr(collect.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="SSH_CLIENT_NOT_FOUND"):
        collect._pinned_ssh_command(**_args(hosts), port=22)


@pytest.mark.parametrize("mode", ["command_failed", "oversized", "invalid_json", "bad_signature", "wrong_role"])
def test_remote_untrusted_output_fails_without_leaking_details(tmp_path, monkeypatch, mode):
    with broker(tmp_path, monkeypatch) as secret:
        report = _remote_report(secret, role="pc-a" if mode == "wrong_role" else "pc-b")
        if mode == "bad_signature":
            report["signature"] = "0" * 64
        hosts, captured = _prepare(tmp_path, monkeypatch, report)

        def untrusted(command, **kwargs):
            captured.append((command, kwargs))
            if mode == "command_failed":
                return SimpleNamespace(returncode=255, stdout=b"", stderr=secret.read_bytes())
            if mode == "oversized":
                return SimpleNamespace(returncode=0, stdout=b"x" * 4500, stderr=b"")
            if mode == "invalid_json":
                return SimpleNamespace(returncode=0, stdout=b"random hostname", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=json.dumps(report).encode(), stderr=b"")

        monkeypatch.setattr(collect.subprocess, "run", untrusted)
        outcome = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-pair-collect", "--host", "192.168.1.21",
            "--user", "safeuser", "--known-hosts", str(hosts),
            "--remote-repo", "D:\\AI\\holoclip",
        ])
        assert outcome.exit_code == 6
        info = json.loads(outcome.stdout)
        assert info["status"] == "LAN_PAIR_COLLECTION_REJECTED"
        assert info["ready"] is False
        assert secret.read_text().strip() not in outcome.stdout
        assert str(secret) not in outcome.stdout
        assert "hostname" not in outcome.stdout


def test_disconnected_local_broker_fails_before_ssh(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        hosts, captured = _prepare(tmp_path, monkeypatch, _remote_report(secret))
    outcome = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pair-collect", "--host", "192.168.1.21",
        "--user", "safeuser", "--known-hosts", str(hosts),
        "--remote-repo", "D:\\AI\\holoclip",
    ])
    assert outcome.exit_code == 6
    assert "COORDINATOR_UNAVAILABLE" in outcome.stdout
    assert captured == []


def test_pinned_ssh_command_does_not_write_reports_or_credentials(tmp_path, monkeypatch):
    with broker(tmp_path, monkeypatch) as secret:
        hosts, _seen = _prepare(tmp_path, monkeypatch, _remote_report(secret))
        from holoclip.providers.source import yt_dlp
        monkeypatch.setattr(
            yt_dlp, "_run_process",
            lambda *_a, **_kw: pytest.fail("yt-dlp MUST NOT be started"),
        )
        report = collect.collect_lan_pair_over_ssh(**_args(hosts))
        assert report["youtube_contacted"] is False
        assert list(tmp_path.glob("*.json")) == []
