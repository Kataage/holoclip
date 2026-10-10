"""Strict no-media cross-SSH pacing trial tests with isolated loopback broker.

No actual SSH/PC-B/YouTube access, no production broker state mutation.
"""
from __future__ import annotations

import base64
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_pacing_trial as trial
from holoclip import source_ytdlp_lan_pair_collect as pair_collect
from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _CoordinatorServer, _CoordinatorState, create_coordinator_secret,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

HOST = "192.168.1.21"


@contextmanager
def coordinator(tmp_path: Path, monkeypatch):
    secret = tmp_path / "test.secret"
    create_coordinator_secret(secret)
    server = _CoordinatorServer(
        ("127.0.0.1", 0),
        bytes.fromhex(secret.read_text(encoding="ascii").strip()),
        _CoordinatorState(tmp_path / "isolated.sqlite3"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", url)
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
    monkeypatch.delenv("HOLOCLIP_YTDLP_COORDINATOR_CONFIG", raising=False)
    try:
        yield LanCoordinatorClient(url, secret_file=secret)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def ssh_host_file(tmp_path):
    file = tmp_path / "pc-b-known-hosts"
    file.write_text(f"{HOST} ssh-ed25519 MOCKED_KEY_DO_NOT_USE\n", encoding="ascii")
    return file


def params(tmp_path):
    return {
        "host": HOST, "user": "testuser",
        "known_hosts": ssh_host_file(tmp_path),
        "remote_repo": r"D:\AI\holoclip",
    }


def no_media(report):
    assert report["youtube_contacted"] is False
    assert report["media_downloaded"] is False
    assert report["video_process_started"] is False
    assert report["firewall_modified"] is False
    assert report["task_modified"] is False
    assert report["credentials_transferred"] is False
    assert report["ssh_fingerprint_independently_verified"] is False
    assert report["physical_two_host_identity_verified"] is False
    assert report["real_two_host_pacing_qualified"] is False
    assert report["two_host_verified"] is False
    assert report["ready"] is False


def mock_host(monkeypatch):
    monkeypatch.setattr(pair_collect.shutil, "which", lambda executable: "mock-ssh")


def test_preview_checks_host_pin_but_never_contacts_coordinator_or_pc_b(tmp_path, monkeypatch):
    mock_host(monkeypatch)
    monkeypatch.setattr(
        trial, "check_lan_onboarding",
        lambda **kwargs: pytest.fail("preview must not connect"),
    )
    monkeypatch.setattr(
        trial, "subprocess",
        SimpleNamespace(run=lambda *a, **k: pytest.fail("preview must not run SSH")),
    )
    preview = trial.trial_lan_pacing(**params(tmp_path))
    assert preview["status"] == "LAN_PACING_PREVIEW_ONLY"
    assert preview["preflight"] == "NOT_EXECUTED"
    assert preview["lease_requested"] is False
    assert preview["changes_may_be_partial"] is False
    no_media(preview)


def test_two_independent_clients_observe_lease_and_gap_without_media(
    tmp_path, monkeypatch,
):
    with coordinator(tmp_path, monkeypatch) as client:
        assert trial.witness_lan_pacing(phase="held")["status"] == (
            "LAN_PACING_WITNESS_UNVERIFIED"
        )
        with client.reserve(timeout_seconds=70):
            held = trial.witness_lan_pacing(phase="held")
            assert held["status"] == "LAN_PACING_WITNESS_HELD_DENIED"
            assert held["lease_access_deferred"] is True
            assert held["coordinator_instance_id"] == client.status()["coordinator_instance_id"]
        gap = trial.witness_lan_pacing(phase="gap")
        assert gap["status"] == "LAN_PACING_WITNESS_GAP_DENIED"
        assert gap["lease_access_deferred"] is True
        assert gap["coordinator_instance_id"] == held["coordinator_instance_id"]
        assert gap["youtube_contacted"] is False
        assert gap["ready"] is False


def test_trial_uses_two_fixed_pinned_ssh_phases_with_real_loopback_broker(
    tmp_path, monkeypatch,
):
    mock_host(monkeypatch)
    data = params(tmp_path)
    with coordinator(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            trial, "check_lan_onboarding",
            lambda **kwargs: {"status": "LAN_ONBOARD_OBSERVATIONS_COMPLETE"},
        )
        monkeypatch.setattr(
            yt_dlp, "_run_process",
            lambda *_a, **_kw: pytest.fail("yt-dlp must never launch"),
        )
        phases = []

        def fake_ssh(command, **options):
            assert "-EncodedCommand" in command
            assert "StrictHostKeyChecking=yes" in command
            assert "BatchMode=yes" in command
            assert "PasswordAuthentication=no" in command
            assert options["capture_output"] is True
            assert options["timeout"] == 30
            remote_script = base64.b64decode(command[-1]).decode("utf-16le")
            assert "uv run --offline --no-sync holoclip examples-ytdlp-lan-pacing-witness" in remote_script
            phase = "held" if "--phase held;" in remote_script else "gap"
            assert "--phase " + phase + ";" in remote_script
            phases.append(phase)
            witness = trial.witness_lan_pacing(phase=phase)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(witness).encode("ascii"),
                stderr=b"",
            )

        monkeypatch.setattr(trial.subprocess, "run", fake_ssh)
        report = trial.trial_lan_pacing(**data, apply=True)
        assert report["status"] == "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED"
        assert report["initial_gate"] == "AVAILABLE"
        assert report["preflight"] == "LAN_PACING_ONBOARDING_OBSERVED"
        assert report["held_witness"] == "LAN_PACING_WITNESS_HELD_DENIED"
        assert report["gap_witness"] == "LAN_PACING_WITNESS_GAP_DENIED"
        assert report["same_coordinator_state_observed"] is True
        assert report["lease_requested"] and report["lease_acquired"]
        assert report["lease_release_confirmed"] is True
        assert report["regular_pacing_gap_created"] is True
        assert report["possible_active_lease_until_expiry"] is False
        assert report["changes_may_be_partial"] is False
        assert phases == ["held", "gap"]
        assert client.status()["code"] == "WAIT_GAP"
        no_media(report)


def test_untrusted_remote_witness_never_passes(tmp_path, monkeypatch):
    mock_host(monkeypatch)
    data = params(tmp_path)
    with coordinator(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            trial, "check_lan_onboarding",
            lambda **kwargs: {"status": "LAN_ONBOARD_OBSERVATIONS_COMPLETE"},
        )
        calls = []

        def fake_ssh(*args, **kwargs):
            calls.append("called")
            witness = trial.witness_lan_pacing(phase="held")
            witness["coordinator_instance_id"] = "0" * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps(witness).encode(),
                                   stderr=b"password-secret-must-not-appear")

        monkeypatch.setattr(trial.subprocess, "run", fake_ssh)
        report = trial.trial_lan_pacing(**data, apply=True)
        assert report["status"] == "LAN_PACING_HELD_WITNESS_UNVERIFIED"
        assert report["lease_release_confirmed"] is True
        assert report["possible_active_lease_until_expiry"] is False
        assert report["same_coordinator_state_observed"] is False
        assert report["gap_witness"] == "NOT_OBSERVED"
        assert len(calls) == 1
        assert client.status()["code"] == "WAIT_GAP"
        assert "password-secret" not in json.dumps(report)
        no_media(report)


def test_incomplete_preflight_never_requests_real_lease(tmp_path, monkeypatch):
    mock_host(monkeypatch)
    data = params(tmp_path)
    with coordinator(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            trial, "check_lan_onboarding",
            lambda **kwargs: {"status": "LAN_ONBOARD_SETUP_INCOMPLETE"},
        )
        monkeypatch.setattr(
            trial, "_remote_witness",
            lambda *args, **kwargs: pytest.fail("no witness without complete preflight"),
        )
        result = trial.trial_lan_pacing(**data, apply=True)
        assert result["preflight"] == "LAN_PACING_ONBOARDING_INCOMPLETE"
        assert result["lease_requested"] is False
        assert client.status()["code"] == "AVAILABLE"
        no_media(result)


def test_live_gate_not_available_never_forces_or_releases_someone_elses_lease(
    tmp_path, monkeypatch,
):
    mock_host(monkeypatch)
    data = params(tmp_path)
    with coordinator(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            trial, "check_lan_onboarding",
            lambda **kwargs: {"status": "LAN_ONBOARD_OBSERVATIONS_COMPLETE"},
        )
        with client.reserve(timeout_seconds=70):
            result = trial.trial_lan_pacing(**data, apply=True)
            assert result["status"] == "LAN_PACING_GATE_NOT_AVAILABLE"
            assert result["initial_gate"] == "ACTIVE"
            assert result["lease_requested"] is False
            assert client.status()["code"] == "ACTIVE"
            no_media(result)


@pytest.mark.parametrize("mutation", [
    "unexpected_flag", "wrong_phase", "wrong_identity", "unknown_key",
    "truncated", "invalid_json", "bad_code", "status_exit_fail",
])
def test_malformed_remote_json_or_identity_never_qualifies(
    tmp_path, monkeypatch, mutation,
):
    def response(*args, **kwargs):
        report = {
            "phase": "held", "status": "LAN_PACING_WITNESS_HELD_DENIED",
            "coordinator_instance_id": "a" * 64,
            "lease_access_deferred": True,
            "youtube_contacted": False,
            "video_process_started": False,
            "ready": False,
        }
        if mutation == "unexpected_flag":
            report["ready"] = True
        elif mutation == "wrong_phase":
            report["phase"] = "gap"
        elif mutation == "wrong_identity":
            report["coordinator_instance_id"] = "b" * 64
        elif mutation == "unknown_key":
            report["credential"] = "SECRET"
        elif mutation == "bad_code":
            report["status"] = "LAN_PACING_WITNESS_UNEXPECTED_LEASE"
        elif mutation == "status_exit_fail":
            return SimpleNamespace(returncode=6, stdout=json.dumps(report).encode())
        elif mutation == "truncated":
            return SimpleNamespace(returncode=0, stdout=b"x" * 3000)
        elif mutation == "invalid_json":
            return SimpleNamespace(returncode=0, stdout=b"secret-invalid-JSON")
        return SimpleNamespace(returncode=0, stdout=json.dumps(report).encode())

    monkeypatch.setattr(trial.subprocess, "run", response)
    assert trial._remote_witness(["mock-ssh"], phase="held", identity="a" * 64) is False


@pytest.mark.parametrize("bad", [
    "127.0.0.1", "8.8.8.8", "192.168.001.021", "malicious;commands",
])
def test_bad_pc_b_endpoint_rejected_without_network(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(
        trial, "_pinned_ssh_command",
        lambda **kw: pytest.fail("bad host rejected before SSH configuration"),
    )
    with pytest.raises(ValueError, match="LAN_PACING_UNSAFE_PC_B"):
        trial.trial_lan_pacing(
            host=bad, user="safeuser", known_hosts=tmp_path / "keys",
            remote_repo=r"D:\AI\holoclip",
        )


def test_cli_preview_produces_no_false_ready(tmp_path, monkeypatch):
    mock_host(monkeypatch)
    data = params(tmp_path)
    args = [
        "examples-ytdlp-lan-pacing-trial",
        "--host", data["host"], "--user", data["user"],
        "--known-hosts", str(data["known_hosts"]),
        "--remote-repo", data["remote_repo"],
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "LAN_PACING_PREVIEW_ONLY"
    assert report["lease_requested"] is False
    assert report["ready"] is False


def test_cli_witness_rejects_invalid_phase_without_media():
    result = CliRunner().invoke(
        cli.app, ["examples-ytdlp-lan-pacing-witness", "--phase", "evil"],
    )
    assert result.exit_code == 6
    report = json.loads(result.stdout)
    assert report["status"] == "LAN_PACING_WITNESS_REJECTED"
    assert report["ready"] is False


def test_pinned_ssh_trial_restricts_phase_to_internal_allowlist(tmp_path, monkeypatch):
    mock_host(monkeypatch)
    data = params(tmp_path)
    for name in ("pacing-held", "pacing-gap"):
        args = pair_collect._pinned_ssh_command(
            host=HOST, user="testuser", known_hosts=data["known_hosts"],
            remote_repo=data["remote_repo"], port=22, remote_probe=name,
        )
        decoded = base64.b64decode(args[-1]).decode("utf-16le")
        assert "--phase " + name.removeprefix("pacing-") in decoded
        assert "uv run --offline --no-sync" in decoded
        assert "StrictHostKeyChecking=yes" in args
    with pytest.raises(ValueError, match="INVALID_REMOTE_PROBE"):
        pair_collect._pinned_ssh_command(
            host=HOST, user="testuser", known_hosts=data["known_hosts"],
            remote_repo=data["remote_repo"], port=22,
            remote_probe="pacing-held; Remove-Item",
        )
