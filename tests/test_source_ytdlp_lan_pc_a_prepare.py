"""PC-A three-stage setup coordinator: mock only, no Windows or LAN changes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_pc_a_prepare as sequence

BIND = "192.168.1.10"
PEER = "192.168.1.21"


def _paths(tmp_path: Path):
    root = tmp_path / "data" / "runtime"
    return root / "secret.txt", root / "client.json", root / "state.sqlite3"


def _run(tmp_path, **kwargs):
    secret, config, state = _paths(tmp_path)
    return sequence.prepare_pc_a_lan(
        bind_ip=BIND, peer_ip=PEER,
        secret_file=secret, config_file=config, state_path=state, **kwargs,
    )


def _mock_stages(
    monkeypatch, *, firewall_preview="LAN_FIREWALL_PREVIEW_ELIGIBLE",
    firewall_apply="LAN_FIREWALL_RULE_ADDED",
    task_state="LAN_TASK_ABSENT_OBSERVED",
    bootstrap_apply="LAN_BOOTSTRAP_LOCAL_PREPARED",
    task_apply="LAN_TASK_REGISTERED_NOT_STARTED",
):
    monkeypatch.setattr(sequence, "os", SimpleNamespace(name="nt"))
    seen = []

    def client(**kw):
        seen.append(("client", kw["apply"]))
        return {
            "status": bootstrap_apply if kw["apply"] else "LAN_BOOTSTRAP_PREVIEW",
            "credential_generated": bool(kw["apply"]),
        }

    def firewall(**kw):
        seen.append(("firewall", kw["apply"]))
        status = firewall_apply if kw["apply"] else firewall_preview
        return {
            "status": status,
            "rule_added": status == "LAN_FIREWALL_RULE_ADDED",
        }

    def task(**kw):
        seen.append(("task", kw["apply"]))
        return {
            "status": task_apply if kw["apply"] else "LAN_TASK_PREVIEW_BLOCKED",
            "checks": {
                "platform": "LAN_TASK_WINDOWS_OBSERVED",
                "task": task_state,
                "credential": "LAN_TASK_CREDENTIAL_UNVERIFIED",
                "credential_acl": "LAN_TASK_ACL_UNVERIFIED",
                "firewall": "LAN_TASK_FIREWALL_UNVERIFIED",
            },
        }

    monkeypatch.setattr(sequence, "bootstrap_local_lan", client)
    monkeypatch.setattr(sequence, "prepare_lan_firewall", firewall)
    monkeypatch.setattr(sequence, "inspect_lan_task_registration", task)
    return seen


def _invariants(report):
    assert report["ready"] is False
    assert report["two_host_verified"] is False
    assert report["firewall_effective_policy_verified"] is False
    assert report["task_started"] is False
    assert report["credential_transferred"] is False
    assert report["ssh_trust_modified"] is False
    assert report["youtube_contacted"] is False
    assert report["media_downloaded"] is False


def test_preview_is_always_read_only_even_with_missing_prerequisites(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch)
    report = _run(tmp_path)
    assert report["status"] == "LAN_PC_A_PREVIEW_ELIGIBLE"
    assert seen == [("client", False), ("firewall", False), ("task", False)]
    assert report["completed_steps"] == []
    assert report["credential_generated"] is False
    assert report["firewall_rule_added"] is False
    assert report["task_registered"] is False
    assert report["changes_may_be_partial"] is False
    _invariants(report)


def test_apply_preserves_order_and_never_starts_task(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch)
    report = _run(tmp_path, apply=True)
    assert seen == [
        ("client", False), ("firewall", False), ("task", False),
        ("client", True), ("firewall", True), ("task", True),
    ]
    assert report["status"] == "LAN_PC_A_PREPARED_NOT_STARTED"
    assert report["completed_steps"] == ["client", "firewall", "task"]
    assert report["blocking_step"] is None
    assert report["credential_generated"] is True
    assert report["firewall_rule_added"] is True
    assert report["task_registered"] is True
    assert report["changes_may_be_partial"] is False
    _invariants(report)


@pytest.mark.parametrize(("firewall_status", "task_state", "blocked_step"), [
    ("LAN_FIREWALL_PREVIEW_BLOCKED", "LAN_TASK_ABSENT_OBSERVED", "firewall"),
    ("LAN_FIREWALL_ADDED_UNVERIFIED", "LAN_TASK_ABSENT_OBSERVED", "firewall"),
    ("LAN_FIREWALL_PROBE_UNTRUSTED", "LAN_TASK_ABSENT_OBSERVED", "firewall"),
    ("LAN_FIREWALL_PREVIEW_ELIGIBLE", "LAN_TASK_ALREADY_PRESENT", "task"),
    ("LAN_FIREWALL_PREVIEW_ELIGIBLE", "LAN_TASK_ENUMERATION_UNAVAILABLE", "task"),
])
def test_bad_preflight_prevents_every_change(
    tmp_path, monkeypatch, firewall_status, task_state, blocked_step,
):
    seen = _mock_stages(monkeypatch, firewall_preview=firewall_status, task_state=task_state)
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_PREVIEW_BLOCKED"
    assert report["blocking_step"] == blocked_step
    assert all(not apply for _, apply in seen)
    assert report["completed_steps"] == []
    assert report["changes_may_be_partial"] is False
    _invariants(report)


def test_existing_narrow_firewall_is_noop_and_task_still_installed(tmp_path, monkeypatch):
    seen = _mock_stages(
        monkeypatch, firewall_preview="LAN_FIREWALL_ALREADY_SCOPED",
        firewall_apply="LAN_FIREWALL_ALREADY_SCOPED",
    )
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_PREPARED_NOT_STARTED"
    assert report["firewall_rule_added"] is False
    assert report["task_registered"] is True
    assert len(seen) == 6
    _invariants(report)


def test_firewall_apply_failure_keeps_completed_credential_stage(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch, firewall_apply="LAN_FIREWALL_APPLY_UNCONFIRMED")
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_APPLY_INCOMPLETE"
    assert report["completed_steps"] == ["client"]
    assert report["blocking_step"] == "firewall"
    assert report["changes_may_be_partial"] is True
    assert ("task", True) not in seen
    _invariants(report)


def test_task_apply_failure_keeps_completed_earlier_stages(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch, task_apply="LAN_TASK_APPLY_BLOCKED")
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_APPLY_INCOMPLETE"
    assert report["completed_steps"] == ["client", "firewall"]
    assert report["blocking_step"] == "task"
    assert report["task_registered"] is False
    assert report["changes_may_be_partial"] is True
    assert ("task", True) in seen
    _invariants(report)


def test_bootstrap_failure_stops_before_adding_rule(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch, bootstrap_apply="LAN_BOOTSTRAP_TRUSTED_CREDENTIAL_REQUIRED")
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_APPLY_INCOMPLETE"
    assert report["blocking_step"] == "client"
    assert ("firewall", True) not in seen
    assert ("task", True) not in seen
    _invariants(report)


def test_uncertain_exception_suppresses_secret_and_reports_partial_changes(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch)

    def bad_firewall(**kw):
        seen.append(("firewall", kw["apply"]))
        if kw["apply"]:
            raise ValueError("private-secret-DO-NOT-PRINT")
        return {"status": "LAN_FIREWALL_PREVIEW_ELIGIBLE"}

    monkeypatch.setattr(sequence, "prepare_lan_firewall", bad_firewall)
    report = _run(tmp_path, apply=True)
    assert report["status"] == "LAN_PC_A_APPLY_UNCONFIRMED"
    assert report["blocking_step"] == "firewall"
    assert report["completed_steps"] == ["client"]
    assert report["changes_may_be_partial"] is True
    assert "DO-NOT-PRINT" not in json.dumps(report)
    _invariants(report)


@pytest.mark.parametrize(("bind", "peer", "port", "name"), [
    ("127.0.0.1", PEER, 8878, "Holoclip Coordinator"),
    ("0.0.0.0", PEER, 8878, "Holoclip Coordinator"),
    ("8.8.8.8", PEER, 8878, "Holoclip Coordinator"),
    (BIND, BIND, 8878, "Holoclip Coordinator"),
    ("192.168.01.10", PEER, 8878, "Holoclip Coordinator"),
    (BIND, PEER, 80, "Holoclip Coordinator"),
    (BIND, PEER, 8878, "invalid;task"),
])
def test_invalid_inputs_stop_before_preflight(
    tmp_path, monkeypatch, bind, peer, port, name,
):
    seen = _mock_stages(monkeypatch)
    with pytest.raises(ValueError, match="LAN_PC_A_"):
        sequence.prepare_pc_a_lan(
            bind_ip=bind, peer_ip=peer, port=port, task_name=name,
            secret_file=_paths(tmp_path)[0],
            config_file=_paths(tmp_path)[1],
            state_path=_paths(tmp_path)[2],
            apply=True,
        )
    assert not seen


def test_overlapping_path_blocks_before_any_mutation(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch)
    secret, _, state = _paths(tmp_path)
    with pytest.raises(ValueError, match="LAN_PC_A_OVERLAPPING_STORAGE"):
        sequence.prepare_pc_a_lan(
            bind_ip=BIND, peer_ip=PEER, secret_file=secret,
            config_file=secret, state_path=state, apply=True,
        )
    assert seen == []


def test_preview_does_not_hide_missing_platform(tmp_path, monkeypatch):
    seen = _mock_stages(monkeypatch)
    monkeypatch.setattr(sequence, "os", SimpleNamespace(name="posix"))
    result = _run(tmp_path, apply=True)
    assert result["status"] == "LAN_PC_A_WINDOWS_REQUIRED"
    assert seen == []
    _invariants(result)


def test_cli_invalid_parameters_are_sanitized(tmp_path):
    secret, config, state = _paths(tmp_path)
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pc-a-prepare",
        "--bind-ip", "8.8.8.8", "--peer-ip", PEER,
        "--secret-file", str(secret), "--config-file", str(config),
        "--state-path", str(state),
    ])
    assert result.exit_code == 6
    doc = json.loads(result.stdout)
    assert doc["status"] == "LAN_PC_A_PREPARE_REJECTED"
    assert doc["reason"] == "LAN_PC_A_UNSAFE_ADDRESS"
    assert doc["ready"] is False
