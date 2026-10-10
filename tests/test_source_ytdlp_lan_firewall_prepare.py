"""Offline firewall preparation tests: never run real PowerShell or change OS rules."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_firewall_prepare as prepare


def _ps_report(status="LAN_FIREWALL_PREVIEW_ELIGIBLE", *, port=8878):
    return {
        "status": status,
        "check": {
            "LAN_FIREWALL_RULE_ADDED": "LAN_FIREWALL_EXACT_SCOPE_OBSERVED",
            "LAN_FIREWALL_ALREADY_SCOPED": "LAN_FIREWALL_ALREADY_SCOPED",
            "LAN_FIREWALL_APPLY_UNCONFIRMED": "LAN_FIREWALL_CMDLET_FAILURE",
        }.get(status, "LAN_FIREWALL_CAN_ADD_EXACT_RULE"),
        "rule_name": f"HoloclipYtDlpLanTcp-{port}",
        "rule_added": status in {"LAN_FIREWALL_RULE_ADDED", "LAN_FIREWALL_ADDED_UNVERIFIED"},
        "changes_may_be_partial": status == "LAN_FIREWALL_APPLY_UNCONFIRMED",
        "existing_rules_replaced": False,
        "existing_rules_deleted": False,
        "firewall_disabled": False,
        "task_modified": False,
        "youtube_contacted": False,
        "media_downloaded": False,
        "firewall_effective_policy_verified": False,
        "two_host_verified": False,
        "ready": False,
    }


def _mock_windows(tmp_path, monkeypatch, returned):
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="nt"))
    script = tmp_path / "prepare.ps1"
    script.write_text("# synthetic only")
    monkeypatch.setattr(prepare, "_SCRIPT", script)
    monkeypatch.setattr(prepare.shutil, "which", lambda name: "powershell.exe")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(returned).encode("ascii"), stderr=b"",
        )

    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    return seen


def _call(**kwargs):
    return prepare.prepare_lan_firewall(
        bind_ip="192.168.1.10", peer_ip="192.168.1.21", **kwargs,
    )


def test_windows_only_no_mutation(monkeypatch):
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        prepare.subprocess, "run",
        lambda *a, **k: pytest.fail("Windows-only commands must not execute"),
    )
    assert _call()["status"] == "LAN_FIREWALL_WINDOWS_REQUIRED"
    assert _call()["ready"] is False


@pytest.mark.parametrize(("bind", "peer", "port"), [
    ("127.0.0.1", "192.168.1.21", 8878),
    ("192.168.1.10", "192.168.1.10", 8878),
    ("192.168.1.10", "8.8.8.8", 8878),
    ("192.168.1.10", "192.168.1.021", 8878),
    ("192.168.1.10", "192.168.1.21;whoami", 8878),
    ("192.168.1.10", "192.168.1.21", 100),
])
def test_invalid_params_never_launch(bind, peer, port, monkeypatch):
    monkeypatch.setattr(
        prepare.subprocess, "run",
        lambda *a, **k: pytest.fail("Invalid params must stop before subprocess"),
    )
    with pytest.raises(ValueError, match="LAN_FIREWALL_"):
        prepare.prepare_lan_firewall(bind_ip=bind, peer_ip=peer, port=port)


def test_preview_and_apply_argv_are_fixed(tmp_path, monkeypatch):
    seen = _mock_windows(tmp_path, monkeypatch, _ps_report())
    assert _call()["status"] == "LAN_FIREWALL_PREVIEW_ELIGIBLE"
    cmd, kwargs = seen[-1]
    assert "-Apply" not in cmd
    assert cmd[cmd.index("-BindIp") + 1] == "192.168.1.10"
    assert cmd[cmd.index("-PeerIp") + 1] == "192.168.1.21"
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 45
    assert kwargs["check"] is False

    _mock_windows(tmp_path, monkeypatch, _ps_report("LAN_FIREWALL_RULE_ADDED"))
    report = _call(apply=True)
    assert report["status"] == "LAN_FIREWALL_RULE_ADDED"
    assert report["rule_added"] is True


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(ready=True),
    lambda x: x.update(two_host_verified=True),
    lambda x: x.update(firewall_effective_policy_verified=True),
    lambda x: x.update(existing_rules_replaced=True),
    lambda x: x.update(status="LAN_FIREWALL_RULE_ADDED", rule_added=False),
    lambda x: x.update(rule_name="Unexpected Windows rule"),
    lambda x: x.update(status="NOT_A_KNOWN_STATUS"),
    lambda x: x.update(raw_stderr="SECRET"),
])
def test_unknown_or_unsafe_ps_result_fails_closed(tmp_path, monkeypatch, mutation):
    doc = _ps_report()
    mutation(doc)
    _mock_windows(tmp_path, monkeypatch, doc)
    report = _call()
    assert report["status"] == "LAN_FIREWALL_PROBE_UNTRUSTED"
    assert report["ready"] is False
    assert "raw_stderr" not in report


def test_apply_never_accepts_preview_status(tmp_path, monkeypatch):
    _mock_windows(tmp_path, monkeypatch, _ps_report())
    assert _call(apply=True)["status"] == "LAN_FIREWALL_PROBE_UNTRUSTED"


def test_command_failure_does_not_echo_raw_errors(tmp_path, monkeypatch):
    _mock_windows(tmp_path, monkeypatch, _ps_report())
    monkeypatch.setattr(
        prepare.subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"secret=NEVER_SHOW_THIS",
        ),
    )
    report = _call(apply=True)
    assert report["status"] == "LAN_FIREWALL_PROBE_UNTRUSTED"
    assert report["changes_may_be_partial"] is True
    assert "NEVER_SHOW_THIS" not in json.dumps(report)


def test_cli_missing_windows_fails_without_changes(monkeypatch):
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="posix"))
    output = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-firewall-prepare",
        "--bind-ip", "192.168.1.10", "--peer-ip", "192.168.1.21",
    ])
    assert output.exit_code == 6
    report = json.loads(output.stdout)
    assert report["ready"] is False
    assert report["status"] == "LAN_FIREWALL_WINDOWS_REQUIRED"
