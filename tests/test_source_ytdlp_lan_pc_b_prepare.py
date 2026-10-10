"""Synthetic PC-B enrollment tests; no SSH, Windows changes, or YouTube."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_pc_b_prepare as prepare

HOST = "192.168.1.10"
REMOTE = "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt"


def _paths(tmp_path: Path):
    base = tmp_path / "data" / "runtime"
    hosts = tmp_path / "known_hosts"
    hosts.write_text("192.168.1.10 ssh-ed25519 OFFLINE_TEST_ONLY\n", encoding="ascii")
    return hosts, base / "lan-secret.txt", base / "lan-client.json"


def _invoke(tmp_path, **kwargs):
    hosts, secret, config = _paths(tmp_path)
    return prepare.prepare_pc_b_lan(
        coordinator_ip=HOST, ssh_user="operator", remote_path=REMOTE,
        known_hosts=hosts, secret_file=secret, config_file=config,
        **kwargs,
    )


def _fake_stages(
    monkeypatch, *, pin="HOST_PIN_ENTRY_OBSERVED",
    sftp_status="LAN_SECRET_SFTP_IMPORTED",
    client_status="LAN_BOOTSTRAP_LOCAL_PREPARED",
    signed_status="AUTHENTICATED_LOCAL_LAN_COORDINATOR",
):
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="nt"))
    seen = []

    def fake_pin(*args):
        seen.append(("pin", False))
        return pin

    def fake_bootstrap(**kwargs):
        seen.append(("bootstrap", kwargs["apply"]))
        return {
            "status": client_status if kwargs["apply"] else "LAN_BOOTSTRAP_PREVIEW",
        }

    def fake_sftp(**kwargs):
        seen.append(("sftp", True))
        return {"status": sftp_status, "ready": False}

    def fake_signed():
        seen.append(("signed", True))
        return {
            "status": signed_status,
            "authenticated": signed_status == "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        }

    monkeypatch.setattr(prepare, "_host_pin_state", fake_pin)
    monkeypatch.setattr(prepare.shutil, "which", lambda tool: "sftp-fake")
    monkeypatch.setattr(prepare, "bootstrap_local_lan", fake_bootstrap)
    monkeypatch.setattr(prepare, "pull_lan_secret_sftp", fake_sftp)
    monkeypatch.setattr(prepare, "diagnose_lan_client", fake_signed)
    return seen


def _secure(report):
    assert report["ready"] is False
    assert report["two_host_verified"] is False
    assert report["firewall_effective_policy_verified"] is False
    assert report["ssh_host_identity_independently_verified"] is False
    assert report["ssh_host_pin_created"] is False
    assert report["ssh_configuration_modified"] is False
    assert report["credential_generated"] is False
    assert report["task_modified"] is False
    assert report["firewall_modified"] is False
    assert report["coordinator_started"] is False
    assert report["youtube_contacted"] is False
    assert report["media_downloaded"] is False


def test_preview_only_local_pin_inspection_and_client_read(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    result = _invoke(tmp_path)
    assert result["status"] == "LAN_PC_B_PREVIEW_ELIGIBLE"
    assert seen == [("pin", False), ("bootstrap", False)]
    assert result["completed_steps"] == []
    assert result["blocking_step"] is None
    assert result["credential_imported"] is False
    assert result["changes_may_be_partial"] is False
    _secure(result)


def test_apply_orders_import_acl_client_profile_then_signed_status(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    result = _invoke(tmp_path, apply=True)
    assert seen == [
        ("pin", False), ("bootstrap", False), ("sftp", True),
        ("bootstrap", True), ("signed", True),
    ]
    assert result["status"] == "LAN_PC_B_PREPARED_AUTHENTICATED"
    assert result["completed_steps"] == ["credential", "client", "signed_status"]
    assert result["blocking_step"] is None
    assert result["credential_imported"] is True
    assert result["client_profile_prepared"] is True
    assert result["remote_coordinator_authenticated"] is True
    assert result["changes_may_be_partial"] is False
    _secure(result)


def test_reimport_existing_secret_remains_idempotent(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch, sftp_status="LAN_SECRET_SFTP_ALREADY_IMPORTED")
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_PREPARED_AUTHENTICATED"
    assert result["credential_imported"] is False
    assert ("bootstrap", True) in seen
    _secure(result)


@pytest.mark.parametrize("pin", [
    "HOST_PIN_NOT_FOUND", "SSH_KEYGEN_UNAVAILABLE", "HOST_PIN_NOT_SUPPLIED",
])
def test_unverified_host_pin_refuses_contact(tmp_path, monkeypatch, pin):
    seen = _fake_stages(monkeypatch, pin=pin)
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_PREVIEW_BLOCKED"
    assert result["blocking_step"] == "ssh_host_pin"
    assert seen == [("pin", False)]
    _secure(result)


def test_sftp_missing_blocked_preimport(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    monkeypatch.setattr(prepare.shutil, "which", lambda tool: None)
    result = _invoke(tmp_path, apply=True)
    assert result["steps"]["ssh_host_pin"] == "LAN_PC_B_SFTP_UNAVAILABLE"
    assert seen == [("pin", False)]
    _secure(result)


@pytest.mark.parametrize("status", [
    "LAN_SECRET_SFTP_REJECTED", "LAN_SECRET_SFTP_NOT_VERIFIED",
])
def test_unexpected_sftp_result_blocks_profile(tmp_path, monkeypatch, status):
    seen = _fake_stages(monkeypatch, sftp_status=status)
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_APPLY_INCOMPLETE"
    assert result["blocking_step"] == "credential"
    assert result["changes_may_be_partial"] is True
    assert result["completed_steps"] == []
    assert ("bootstrap", True) not in seen
    _secure(result)


def test_sftp_error_does_not_leak_secret_or_continue(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)

    def failure(**kwargs):
        seen.append(("sftp", True))
        raise ValueError("leaked-secret-MUST-NOT-PRINT")

    monkeypatch.setattr(prepare, "pull_lan_secret_sftp", failure)
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_APPLY_UNCONFIRMED"
    assert result["blocking_step"] == "credential"
    assert result["changes_may_be_partial"] is True
    assert "MUST-NOT-PRINT" not in json.dumps(result)
    assert ("bootstrap", True) not in seen
    _secure(result)


def test_profile_failure_stops_before_network_status(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch, client_status="LAN_BOOTSTRAP_TRUSTED_CREDENTIAL_REQUIRED")
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_APPLY_INCOMPLETE"
    assert result["completed_steps"] == ["credential"]
    assert result["blocking_step"] == "client"
    assert ("signed", True) not in seen
    _secure(result)


@pytest.mark.parametrize("signed", [
    "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE",
    "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED",
    "NOT_CONFIGURED",
    "MALICIOUS_PAYLOAD",
])
def test_failed_or_untrusted_signed_status_is_not_readiness(tmp_path, monkeypatch, signed):
    seen = _fake_stages(monkeypatch, signed_status=signed)
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_PREPARED_STATUS_UNVERIFIED"
    assert result["remote_coordinator_authenticated"] is False
    assert result["blocking_step"] == "signed_status"
    assert result["completed_steps"] == ["credential", "client"]
    assert result["changes_may_be_partial"] is False
    assert seen[-1] == ("signed", True)
    if signed == "MALICIOUS_PAYLOAD":
        assert result["steps"]["signed_status"] == "LAN_PC_B_SIGNED_STATUS_UNTRUSTED"
    _secure(result)


@pytest.mark.parametrize(("ip", "user", "remote", "ssh_port", "broker_port"), [
    ("8.8.8.8", "operator", REMOTE, 22, 8878),
    ("127.0.0.1", "operator", REMOTE, 22, 8878),
    ("192.168.001.010", "operator", REMOTE, 22, 8878),
    (HOST, "bad;name", REMOTE, 22, 8878),
    (HOST, "operator", "D:/tmp/../secret", 22, 8878),
    (HOST, "operator", "D:/folder with spaces/key", 22, 8878),
    (HOST, "operator", REMOTE, 0, 8878),
    (HOST, "operator", REMOTE, 22, 80),
])
def test_untrusted_parameters_stop_before_ssh(
    tmp_path, monkeypatch, ip, user, remote, ssh_port, broker_port,
):
    seen = _fake_stages(monkeypatch)
    hosts, secret, config = _paths(tmp_path)
    with pytest.raises(ValueError, match="LAN_PC_B_"):
        prepare.prepare_pc_b_lan(
            coordinator_ip=ip, ssh_user=user, remote_path=remote,
            known_hosts=hosts, ssh_port=ssh_port,
            coordinator_port=broker_port,
            secret_file=secret, config_file=config, apply=True,
        )
    assert seen == []


def test_missing_and_linked_pinned_hosts_never_contact_network(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    hosts, secret, config = _paths(tmp_path)
    hosts.unlink()
    with pytest.raises(ValueError, match="KNOWN_HOSTS_MISSING"):
        prepare.prepare_pc_b_lan(
            coordinator_ip=HOST, ssh_user="operator", remote_path=REMOTE,
            known_hosts=hosts, secret_file=secret, config_file=config, apply=True,
        )
    try:
        hosts.symlink_to(tmp_path / "target")
    except (OSError, NotImplementedError):
        pytest.skip("Symlink unsupported")
    with pytest.raises(ValueError):
        prepare.prepare_pc_b_lan(
            coordinator_ip=HOST, ssh_user="operator", remote_path=REMOTE,
            known_hosts=hosts, secret_file=secret, config_file=config, apply=True,
        )
    assert seen == []


def test_overlapping_destination_and_hosts_rejected(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    hosts, _, config = _paths(tmp_path)
    with pytest.raises(ValueError, match="OVERLAPPING_LOCAL_PATHS"):
        prepare.prepare_pc_b_lan(
            coordinator_ip=HOST, ssh_user="operator", remote_path=REMOTE,
            known_hosts=hosts, secret_file=hosts, config_file=config, apply=True,
        )
    assert seen == []


def test_windows_required_for_preview_and_apply(tmp_path, monkeypatch):
    seen = _fake_stages(monkeypatch)
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="posix"))
    result = _invoke(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_B_WINDOWS_REQUIRED"
    assert seen == []
    _secure(result)


def test_cli_invalid_host_sanitized(tmp_path):
    hosts, secret, config = _paths(tmp_path)
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pc-b-prepare", "--coordinator-ip", "8.8.8.8",
        "--ssh-user", "operator", "--remote-path", REMOTE,
        "--known-hosts", str(hosts), "--secret-file", str(secret),
        "--config-file", str(config),
    ])
    assert result.exit_code == 6
    report = json.loads(result.stdout)
    assert report["status"] == "LAN_PC_B_PREPARE_REJECTED"
    assert report["reason"] == "LAN_PC_B_UNSAFE_COORDINATOR_ADDRESS"
    assert report["ready"] is False


def test_cli_signed_status_unverified_has_nonzero_exit(tmp_path, monkeypatch):
    hosts, secret, config = _paths(tmp_path)
    monkeypatch.setattr(
        cli, "prepare_pc_b_lan",
        lambda **kwargs: {
            "status": "LAN_PC_B_PREPARED_STATUS_UNVERIFIED",
            "client_profile_prepared": True,
            "remote_coordinator_authenticated": False,
            "ready": False,
            "two_host_verified": False,
        },
    )
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pc-b-prepare", "--coordinator-ip", HOST,
        "--ssh-user", "operator", "--remote-path", REMOTE,
        "--known-hosts", str(hosts), "--secret-file", str(secret),
        "--config-file", str(config), "--apply",
    ])
    assert result.exit_code == 6
    report = json.loads(result.stdout)
    assert report["status"] == "LAN_PC_B_PREPARED_STATUS_UNVERIFIED"
    assert report["remote_coordinator_authenticated"] is False
    assert report["ready"] is False
