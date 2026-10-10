"""Safe Windows task registration: preview, guarded apply, never overwrite."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_task_prepare as prepare
from holoclip.source_ytdlp_lan_coordinator import create_coordinator_secret


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _paths(tmp_path):
    base = tmp_path / "data" / "runtime"
    return base / "lan-secret.txt", base / "state.sqlite3"


def _observations(firewall="EXACT_PEER_ALLOW_RULE_OBSERVED"):
    return {
        "coordinator_task": "NOT_FOUND_OR_INACCESSIBLE",
        "coordinator_listener": "NO_LAN_LISTENER",
        "firewall_rule": firewall,
        "sshd_service": "NOT_APPLICABLE",
    }


def _fake_host(monkeypatch, *, firewall="EXACT_PEER_ALLOW_RULE_OBSERVED",
               inventory="LAN_TASK_ABSENT_OBSERVED", acl=True):
    monkeypatch.setattr(prepare, "_windows", lambda: True)
    monkeypatch.setattr(prepare, "_task_state", lambda name: inventory)
    monkeypatch.setattr(prepare, "_secure_windows_path", lambda path, *, apply: acl)
    monkeypatch.setattr(prepare, "_observe_windows", lambda **kwargs: _observations(firewall))


def _call(secret, state, **extra):
    return prepare.inspect_lan_task_registration(
        bind_ip="192.168.1.10", peer_ip="192.168.1.21",
        secret_file=secret, state_path=state, **extra,
    )


def test_default_preview_never_installs_or_starts(tmp_path, monkeypatch):
    secret, state = _paths(tmp_path)
    create_coordinator_secret(secret)
    _fake_host(monkeypatch)
    monkeypatch.setattr(prepare.subprocess, "run",
                        lambda *_a, **_kw: pytest.fail("preview never installs"))
    report = _call(secret, state)
    assert report["status"] == "LAN_TASK_PREVIEW_ELIGIBLE"
    assert report["blockers"] == {}
    assert report["task_registered"] is False
    assert report["task_started"] is False
    assert report["existing_task_overwritten"] is False
    assert report["firewall_modified"] is False
    assert report["ready"] is False
    assert not state.exists()


def test_apply_registers_exact_arguments_without_starting(tmp_path, monkeypatch):
    secret, state = _paths(tmp_path)
    create_coordinator_secret(secret)
    _fake_host(monkeypatch)
    monkeypatch.setattr(prepare.shutil, "which", lambda exe: "mock-powershell.exe")
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"Registered", stderr=b"")

    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    report = _call(secret, state, apply=True)
    assert report["status"] == "LAN_TASK_REGISTERED_NOT_STARTED"
    assert report["task_registered"] is True
    assert report["task_started"] is False
    assert report["two_host_verified"] is False
    assert report["ready"] is False
    assert len(seen) == 1
    cmd, kw = seen[0]
    assert cmd[0] == "mock-powershell.exe"
    assert cmd[cmd.index("-BindIp") + 1] == "192.168.1.10"
    assert cmd[cmd.index("-Port") + 1] == "8878"
    assert cmd[cmd.index("-SecretFile") + 1] == str(secret)
    assert cmd[cmd.index("-StatePath") + 1] == str(state)
    assert kw["capture_output"] is True
    assert kw["timeout"] == 45
    assert "Start-ScheduledTask" not in " ".join(cmd)
    assert secret.read_text().strip() not in json.dumps(report)


@pytest.mark.parametrize("firewall,inventory,acl,expected", [
    ("BROAD_INBOUND_ALLOW_RULE_OBSERVED", "LAN_TASK_ABSENT_OBSERVED", True,
     "LAN_TASK_FIREWALL_UNVERIFIED"),
    ("NO_EXACT_PEER_ALLOW_RULE", "LAN_TASK_ABSENT_OBSERVED", True,
     "LAN_TASK_FIREWALL_UNVERIFIED"),
    ("EXACT_PEER_ALLOW_RULE_OBSERVED", "LAN_TASK_ALREADY_PRESENT", True,
     "LAN_TASK_ALREADY_PRESENT"),
    ("EXACT_PEER_ALLOW_RULE_OBSERVED", "LAN_TASK_ENUMERATION_UNAVAILABLE", True,
     "LAN_TASK_ENUMERATION_UNAVAILABLE"),
    ("EXACT_PEER_ALLOW_RULE_OBSERVED", "LAN_TASK_ABSENT_OBSERVED", False,
     "LAN_TASK_ACL_UNVERIFIED"),
])
def test_no_install_when_prerequisite_fails(
    tmp_path, monkeypatch, firewall, inventory, acl, expected,
):
    secret, state = _paths(tmp_path)
    create_coordinator_secret(secret)
    _fake_host(monkeypatch, firewall=firewall, inventory=inventory, acl=acl)
    monkeypatch.setattr(prepare.subprocess, "run",
                        lambda *_a, **_kw: pytest.fail("must fail before install"))
    result = _call(secret, state, apply=True)
    assert result["status"] == "LAN_TASK_APPLY_BLOCKED"
    assert expected in result["blockers"].values()
    assert result["task_registered"] is False
    assert result["ready"] is False


def test_missing_secret_refuses_registration(tmp_path, monkeypatch):
    secret, state = _paths(tmp_path)
    _fake_host(monkeypatch)
    report = _call(secret, state, apply=True)
    assert report["status"] == "LAN_TASK_APPLY_BLOCKED"
    assert report["checks"]["credential"] == "LAN_TASK_CREDENTIAL_UNVERIFIED"
    assert not secret.exists()


def test_race_when_task_appears_after_preview_stops_install(tmp_path, monkeypatch):
    secret, state = _paths(tmp_path)
    create_coordinator_secret(secret)
    _fake_host(monkeypatch)
    calls = []
    def probe(name):
        calls.append(name)
        return "LAN_TASK_ABSENT_OBSERVED" if len(calls) == 1 else "LAN_TASK_ALREADY_PRESENT"
    monkeypatch.setattr(prepare, "_task_state", probe)
    monkeypatch.setattr(prepare.subprocess, "run",
                        lambda *_a, **_kw: pytest.fail("cannot clobber task"))
    result = _call(secret, state, apply=True)
    assert result["status"] == "LAN_TASK_APPLY_BLOCKED"
    assert result["task_registered"] is False
    assert len(calls) == 2


def test_failed_registration_keeps_credentials_and_does_not_claim_success(
    tmp_path, monkeypatch,
):
    secret, state = _paths(tmp_path)
    create_coordinator_secret(secret)
    credential_before = secret.read_bytes()
    _fake_host(monkeypatch)
    monkeypatch.setattr(prepare.shutil, "which", lambda exe: "mock-powershell.exe")
    monkeypatch.setattr(prepare.subprocess, "run",
                        lambda *_a, **_kw: SimpleNamespace(
                            returncode=1, stdout=b"", stderr=credential_before,
                        ))
    with pytest.raises(ValueError, match="LAN_TASK_INSTALL_FAILED"):
        _call(secret, state, apply=True)
    assert credential_before == secret.read_bytes()
    assert not state.exists()


def test_windows_inventory_absence_is_verified_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: "ps")
    monkeypatch.setattr(prepare, "_PROBE", tmp_path / "probe.ps1")
    prepare._PROBE.write_text("Mocked probe")
    def result(code, stdout):
        monkeypatch.setattr(prepare.subprocess, "run",
                            lambda *_a, **_kw: SimpleNamespace(returncode=code, stdout=stdout))
        return prepare._task_state("Holoclip yt-dlp LAN Coordinator")
    assert result(0, b'{"status":"LAN_TASK_ABSENT_OBSERVED","ready":false}') == (
        "LAN_TASK_ABSENT_OBSERVED"
    )
    assert result(0, b'{"status":"LAN_TASK_ALREADY_PRESENT","ready":false}') == (
        "LAN_TASK_ALREADY_PRESENT"
    )
    assert result(0, b'{"status":"LAN_TASK_ABSENT_OBSERVED","ready":true}') == (
        "LAN_TASK_ENUMERATION_UNAVAILABLE"
    )
    assert result(6, b'{"status":"LAN_TASK_ABSENT_OBSERVED","ready":false}') == (
        "LAN_TASK_ENUMERATION_UNAVAILABLE"
    )
    assert result(0, b"garbage") == "LAN_TASK_ENUMERATION_UNAVAILABLE"


@pytest.mark.parametrize("bad", [
    ("127.0.0.1", "192.168.1.21", 8878, "Valid Name"),
    ("192.168.1.10", "8.8.8.8", 8878, "Valid Name"),
    ("192.168.1.10", "192.168.1.10", 8878, "Valid Name"),
    ("192.168.1.10", "192.168.1.21", 80, "Valid Name"),
    ("192.168.1.10", "192.168.1.21", 8878, "Invalid;Name"),
])
def test_invalid_lan_parameters_fail_before_mutation(tmp_path, monkeypatch, bad):
    secret, state = _paths(tmp_path)
    monkeypatch.setattr(prepare, "_task_state", lambda *_: pytest.fail("no inventory"))
    with pytest.raises(ValueError):
        prepare.inspect_lan_task_registration(
            bind_ip=bad[0], peer_ip=bad[1], port=bad[2],
            task_name=bad[3], secret_file=secret, state_path=state,
        )


def test_cli_no_secret_exits_safely(tmp_path, monkeypatch):
    secret, state = _paths(tmp_path)
    _fake_host(monkeypatch)
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-task-prepare",
        "--bind-ip", "192.168.1.10", "--peer-ip", "192.168.1.21",
        "--secret-file", str(secret), "--state-path", str(state),
    ])
    assert result.exit_code == 6
    doc = json.loads(result.stdout)
    assert doc["status"] == "LAN_TASK_PREVIEW_BLOCKED"
    assert doc["ready"] is False
