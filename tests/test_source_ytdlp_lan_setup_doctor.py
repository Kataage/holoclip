"""LAN setup preflight: no media calls and no changes to local machine."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_ytdlp_lan_coordinator import (
    _CoordinatorServer, _CoordinatorState,
    create_coordinator_secret, configure_lan_client,
)
from holoclip import source_ytdlp_lan_setup_doctor as doctor


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(k, raising=False)


def _observed(**overrides):
    defaults = {
        "coordinator_task": "RUNNING_OBSERVED",
        "coordinator_listener": "LAN_LISTENER_OBSERVED",
        "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
        "sshd_service": "NOT_INSTALLED_OR_INACCESSIBLE",
    }
    defaults.update(overrides)
    return defaults


def test_unconfigured_pc_b_is_actionable_and_nonzero(tmp_path):
    result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-setup-doctor", "--role", "pc-b"])
    assert result.exit_code == 6, result.output
    data = json.loads(result.stdout)
    assert data["role"] == "pc-b"
    assert data["status"] == "LOCAL_SETUP_INCOMPLETE"
    assert data["ready"] is False and data["production_ready"] is False
    assert data["two_host_verified"] is False
    assert data["media_downloaded"] is False
    assert data["config_modified"] is False
    assert data["checks"]["signed_coordinator_status"]["code"] == "NOT_CONFIGURED"
    assert "examples-ytdlp-lan-configure" in data["checks"]["signed_coordinator_status"]["action"]
    assert "LAN_CREDENTIAL_ACL_UNVERIFIED" == data["checks"]["credential_acl"]["code"]


def test_missing_pc_a_secret_does_not_claim_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: _observed())
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    result = doctor.inspect_lan_setup(role="pc-a", peer_ip="192.168.1.21")
    assert result["checks"]["local_credential"]["code"] == "LAN_CREDENTIAL_MISSING_OR_INVALID"
    assert result["status"] == "LOCAL_SETUP_INCOMPLETE"
    assert result["ready"] is False


def test_signed_loopback_pc_b_with_readonly_acl_check(tmp_path, monkeypatch):
    import threading
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    server = _CoordinatorServer(
        ("127.0.0.1", 0), bytes.fromhex(secret.read_text().strip()),
        _CoordinatorState(tmp_path / "state.sqlite3"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        configure_lan_client(
            url=f"http://127.0.0.1:{server.server_port}", secret_file=secret,
        )
        seen = []
        monkeypatch.setattr(doctor, "_is_windows", lambda: True)
        monkeypatch.setattr(doctor, "_secure_windows_path",
                            lambda path, *, apply: seen.append((path, apply)) or True)
        monkeypatch.setattr(doctor, "_observe_windows",
                            lambda **_kw: _observed(
                                coordinator_task="NOT_APPLICABLE",
                                coordinator_listener="NOT_APPLICABLE",
                                firewall_rule="NOT_APPLICABLE",
                                sshd_service="NOT_APPLICABLE"))
        data = doctor.inspect_lan_setup(role="pc-b")
        assert data["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
        assert data["checks"]["signed_coordinator_status"]["observed_remote_gate"] == "AVAILABLE"
        assert data["checks"]["credential_acl"]["code"] == "LAN_CREDENTIAL_ACL_OBSERVED"
        assert seen == [(secret, False)]
        assert data["two_host_verified"] is False
        assert data["firewall_effective_policy_verified"] is False
        assert data["production_ready"] is False
        assert secret.read_text().strip() not in json.dumps(data)
        assert str(secret) not in json.dumps(data)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_pc_a_requires_scoped_firewall_and_listener(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "_secure_windows_path", lambda *_a, **_kw: True)
    monkeypatch.setattr(doctor, "diagnose_lan_client", lambda: {
        "status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        "authenticated": True, "remote_gate_status": "ACTIVE",
    })
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: _observed())
    good = doctor.inspect_lan_setup(
        role="pc-a", secret_file=secret, peer_ip="192.168.1.21",
    )
    assert good["status"] == "LOCAL_SETUP_OBSERVATIONS_COMPLETE"
    assert good["production_ready"] is False
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: _observed(
        firewall_rule="BROAD_INBOUND_ALLOW_RULE_OBSERVED"))
    bad = doctor.inspect_lan_setup(
        role="pc-a", secret_file=secret, peer_ip="192.168.1.21",
    )
    assert bad["status"] == "LOCAL_SETUP_INCOMPLETE"
    assert "restrict" in bad["checks"]["firewall_rule"]["action"].lower()
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: _observed(
        coordinator_listener="WILDCARD_LISTENER_OBSERVED"))
    wildcard = doctor.inspect_lan_setup(role="pc-a", secret_file=secret)
    assert wildcard["status"] == "LOCAL_SETUP_INCOMPLETE"


def test_no_acl_readiness_when_windows_probe_fails(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "_secure_windows_path", lambda *_a, **_kw: False)
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: None)
    report = doctor.inspect_lan_setup(role="pc-a", secret_file=secret)
    assert report["checks"]["credential_acl"]["code"] == "LAN_CREDENTIAL_ACL_UNVERIFIED"
    assert report["checks"]["firewall_rule"]["code"] == "WINDOWS_INSPECTION_UNAVAILABLE"
    assert report["status"] == "LOCAL_SETUP_INCOMPLETE"


@pytest.mark.parametrize("args", [
    {"role": "unknown"},
    {"role": "pc-a", "port": 90000},
    {"role": "pc-a", "peer_ip": "8.8.8.8"},
    {"role": "pc-a", "peer_ip": "localhost"},
    {"role": "pc-a", "task_name": 'Task" -Run "evil'},
])
def test_invalid_parameters_are_rejected_before_windows_probes(tmp_path, monkeypatch, args):
    monkeypatch.setattr(doctor, "_observe_windows", lambda **_kw: pytest.fail("No subprocess"))
    with pytest.raises(ValueError):
        doctor.inspect_lan_setup(**args)


def test_known_hosts_check_uses_offline_ssh_keygen_only(tmp_path, monkeypatch):
    hosts = tmp_path / "known_hosts"
    hosts.write_text("192.168.1.20 ssh-ed25519 fake\n")
    recorded = []

    def fake_run(args, **kwargs):
        recorded.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "ssh-keygen" if cmd == "ssh-keygen" else None)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor._host_pin_state("192.168.1.20", hosts, 22) == "HOST_PIN_ENTRY_OBSERVED"
    assert recorded[0][0] == ["ssh-keygen", "-F", "192.168.1.20", "-f", str(hosts)]
    assert recorded[0][1]["timeout"] == 8
    assert doctor._host_pin_state("192.168.1.20", hosts, 2222) == "HOST_PIN_ENTRY_OBSERVED"
    assert recorded[1][0][2] == "[192.168.1.20]:2222"
    assert doctor._host_pin_state(None, None, 22) == "HOST_PIN_NOT_SUPPLIED"
    assert doctor._host_pin_state("192.168.1.20", None, 22) == "HOST_PIN_NOT_FOUND"


def test_untrusted_windows_inspector_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "powershell.exe")
    monkeypatch.setattr(doctor, "_WINDOWS_SCRIPT", Path(__file__))
    monkeypatch.setattr(doctor.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({
            "status": "WINDOWS_OBSERVATIONS", "role": "pc-a", "ready": False,
            "two_host_verified": False,
            "firewall_effective_policy_verified": False,
            **_observed(firewall_rule="UNTRUSTED_BAD_STATUS"),
        })))
    assert doctor._observe_windows(role="pc-a", port=8878,
                                   task_name="Holoclip yt-dlp LAN Coordinator",
                                   peer_ip=None) is None


def test_missing_or_corrupt_explicit_profile_cannot_succeed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_CONFIG", str(tmp_path / "missing.json"))
    data = doctor.inspect_lan_setup(role="pc-b")
    assert data["status"] == "LOCAL_SETUP_INCOMPLETE"
    assert data["checks"]["signed_coordinator_status"]["code"] == "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID"
    assert data["checks"]["local_credential"]["code"] == "LAN_CREDENTIAL_MISSING_OR_INVALID"
