"""Offline, fail-closed deployment diagnosis: never SSH or access media."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_deployment_diagnosis as diagnosis

A = "192.168.1.10"
B = "192.168.1.21"


def _codes(role):
    codes = {
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
        "coordinator_task": "RUNNING_OBSERVED",
        "coordinator_listener": "LAN_LISTENER_OBSERVED",
        "firewall_rule": "EXACT_PEER_ALLOW_RULE_OBSERVED",
        "sshd_service": "NOT_APPLICABLE",
        "ssh_host_pin": "HOST_PIN_NOT_SUPPLIED",
    }
    if role == "pc-b":
        codes.update({
            "coordinator_task": "NOT_APPLICABLE",
            "coordinator_listener": "NOT_APPLICABLE",
            "firewall_rule": "NOT_APPLICABLE",
        })
    return codes


def _paired(a=None, b=None, *, status="LAN_ONBOARD_OBSERVATIONS_COMPLETE",
            pair="LAN_PAIR_OBSERVATIONS_MATCH"):
    a = a or _codes("pc-a")
    b = b or _codes("pc-b")
    return {
        "status": status,
        "pc_a": {"role": "pc-a", "status": "LOCAL_SETUP_OBSERVATIONS_COMPLETE",
                 "checks": a},
        "pc_b": {"role": "pc-b", "status": "LOCAL_SETUP_OBSERVATIONS_COMPLETE",
                 "checks": b},
        "pair": None if pair is None else {"status": pair},
        "ready": False,
        "two_host_verified": False,
    }


def _local_doctor(*, firewall="EXACT_PEER_ALLOW_RULE_OBSERVED", credential="LAN_CREDENTIAL_VALID"):
    codes = _codes("pc-a")
    codes["firewall_rule"] = firewall
    codes["local_credential"] = credential
    return {
        "role": "pc-a",
        "status": "LOCAL_SETUP_INCOMPLETE",
        "ready": False,
        "production_ready": False,
        "two_host_verified": False,
        "firewall_effective_policy_verified": False,
        "config_modified": False,
        "youtube_contacted": False,
        "checks": {
            key: {"code": val, "action": "ATTACKER RAW SECRET"}
            for key, val in codes.items()
        },
    }


def _run(tmp_path):
    return diagnosis.diagnose_lan_deployment(
        host=B, user="safeuser", known_hosts=tmp_path / "known",
        remote_repo="D:\\AI\\holoclip",
    )


def _assert_safety(report):
    assert report["ready"] is False
    assert report["two_host_verified"] is False
    assert report["firewall_effective_policy_verified"] is False
    assert report["ssh_fingerprint_independently_verified"] is False
    assert report["credential_transferred"] is False
    assert report["firewall_modified"] is False
    assert report["task_started"] is False
    assert report["lease_requested"] is False
    assert report["youtube_contacted"] is False
    assert "ATTACKER RAW SECRET" not in json.dumps(report)


def test_two_pc_observation_match_never_becomes_production_ready(tmp_path, monkeypatch):
    seen = []

    def fake_pair(**kwargs):
        seen.append(kwargs)
        return _paired()

    monkeypatch.setattr(diagnosis, "check_lan_onboarding", fake_pair)
    monkeypatch.setattr(diagnosis, "inspect_lan_setup",
                        lambda **kwargs: pytest.fail("no duplicate local query"))
    report = _run(tmp_path)
    assert report["status"] == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
    assert report["remote_ssh_report_validated"] is True
    assert report["repair_plan"] == []
    assert report["pair_observation"] == "LAN_PAIR_OBSERVATIONS_MATCH"
    assert seen[0]["host"] == B
    _assert_safety(report)


def test_aggregates_ordered_local_and_remote_blockers(tmp_path, monkeypatch):
    a = _codes("pc-a")
    a["firewall_rule"] = "BROAD_INBOUND_ALLOW_RULE_OBSERVED"
    a["coordinator_task"] = "NOT_RUNNING"
    b = _codes("pc-b")
    b["credential_acl"] = "LAN_CREDENTIAL_ACL_UNVERIFIED"
    monkeypatch.setattr(
        diagnosis, "check_lan_onboarding",
        lambda **kwargs: _paired(a, b, status="LAN_ONBOARD_SETUP_INCOMPLETE"),
    )
    result = _run(tmp_path)
    assert result["status"] == "LAN_DEPLOYMENT_GAPS_OBSERVED"
    plan = result["repair_plan"]
    assert [(x["role"], x["check"]) for x in plan] == [
        ("pc-a", "firewall_rule"),
        ("pc-a", "coordinator_task"),
        ("pc-b", "credential_acl"),
    ]
    assert [x["priority"] for x in plan] == [1, 2, 3]
    assert plan[0]["observed"] == "BROAD_INBOUND_ALLOW_RULE_OBSERVED"
    assert plan[0]["suggested_command"] == "examples-ytdlp-lan-firewall-prepare"
    assert all(x["automatically_modified"] is False for x in plan)
    _assert_safety(result)


@pytest.mark.parametrize(("failure", "expected"), [
    ("LAN_ONBOARD_REMOTE_SSH_FAILED", "LAN_ONBOARD_REMOTE_SSH_FAILED"),
    ("LAN_PAIR_PINNED_HOST_KEY_REQUIRED", "LAN_PAIR_PINNED_HOST_KEY_REQUIRED"),
    ("LAN_PAIR_BAD_SIGNATURE", "LAN_DEPLOYMENT_REMOTE_REPORT_UNTRUSTED"),
    ("LAN_ONBOARD_UNTRUSTED_REMOTE_REPORT", "LAN_DEPLOYMENT_REMOTE_REPORT_UNTRUSTED"),
    ("private-machine-ip-and-password-in-error", "LAN_DEPLOYMENT_REMOTE_DIAGNOSIS_UNVERIFIED"),
])
def test_remote_failure_still_returns_local_observations_without_forging_pc_b(
    tmp_path, monkeypatch, failure, expected,
):
    def fake_pair(**kwargs):
        raise ValueError(failure)

    inspected = []
    monkeypatch.setattr(diagnosis, "check_lan_onboarding", fake_pair)

    def fake_doctor(**kwargs):
        inspected.append(kwargs)
        return _local_doctor(firewall="BROAD_INBOUND_ALLOW_RULE_OBSERVED")

    monkeypatch.setattr(diagnosis, "inspect_lan_setup", fake_doctor)
    result = _run(tmp_path)
    assert inspected[0]["role"] == "pc-a"
    assert inspected[0]["peer_ip"] == B
    assert result["status"] == "LAN_DEPLOYMENT_REMOTE_UNVERIFIED"
    assert result["pc_b"] is None
    assert result["pair_observation"] is None
    assert result["remote_ssh_report_validated"] is False
    assert result["remote_failure"] == expected
    assert [x["check"] for x in result["repair_plan"]] == ["firewall_rule", "remote_ssh"]
    assert "private-machine" not in json.dumps(result)
    assert "ATTACKER RAW SECRET" not in json.dumps(result)
    _assert_safety(result)


def test_invalid_local_fallback_does_not_make_fake_positive_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        diagnosis, "check_lan_onboarding",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("LAN_ONBOARD_REMOTE_SSH_FAILED")),
    )
    bad = _local_doctor()
    bad["ready"] = True
    monkeypatch.setattr(diagnosis, "inspect_lan_setup", lambda **kwargs: bad)
    report = _run(tmp_path)
    assert report["pc_a"] is None and report["pc_b"] is None
    assert [x["check"] for x in report["repair_plan"]] == ["remote_ssh"]
    _assert_safety(report)


def test_inconclusive_pair_cannot_be_promoted_to_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        diagnosis, "check_lan_onboarding",
        lambda **kwargs: _paired(
            status="LAN_ONBOARD_PAIR_INCONCLUSIVE",
            pair="LAN_PAIR_OBSERVATIONS_INCONCLUSIVE",
        ),
    )
    report = _run(tmp_path)
    assert report["status"] == "LAN_DEPLOYMENT_GAPS_OBSERVED"
    assert report["repair_plan"][-1]["check"] == "signed_pair"
    assert report["repair_plan"][-1]["suggested_command"] == "examples-ytdlp-lan-pair-collect"
    _assert_safety(report)


@pytest.mark.parametrize("peer", ["127.0.0.1", "8.8.8.8", "192.168.001.021", "evil;cmd"])
def test_unsafe_endpoint_rejected_before_network(tmp_path, monkeypatch, peer):
    monkeypatch.setattr(
        diagnosis, "check_lan_onboarding",
        lambda **kwargs: pytest.fail("no SSH or network calls"),
    )
    with pytest.raises(ValueError, match="LAN_DEPLOYMENT_INVALID_PEER"):
        diagnosis.diagnose_lan_deployment(
            host=peer, user="safeuser", known_hosts=tmp_path / "known",
            remote_repo="D:\\AI\\holoclip",
        )


def test_cli_reports_incomplete_with_nonzero_exit_and_no_raw_data(tmp_path, monkeypatch):
    def fake(**kwargs):
        return {
            "status": "LAN_DEPLOYMENT_REMOTE_UNVERIFIED",
            "pc_a": None,
            "pc_b": None,
            "remote_failure": "LAN_ONBOARD_REMOTE_SSH_FAILED",
            "repair_plan": [],
            "ready": False,
        }

    monkeypatch.setattr(cli, "diagnose_lan_deployment", fake)
    output = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-deployment-diagnose",
        "--host", B, "--user", "safeuser",
        "--known-hosts", str(tmp_path / "known"),
        "--remote-repo", "D:\\AI\\holoclip",
    ])
    assert output.exit_code == 6
    report = json.loads(output.stdout)
    assert report["ready"] is False
    assert report["status"] == "LAN_DEPLOYMENT_REMOTE_UNVERIFIED"


def test_cli_rejects_invalid_peer_with_sanitized_fixed_code(tmp_path):
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-deployment-diagnose",
        "--host", "8.8.8.8", "--user", "safeuser",
        "--known-hosts", str(tmp_path / "known"),
        "--remote-repo", "D:\\AI\\holoclip",
    ])
    assert result.exit_code == 6
    output = json.loads(result.stdout)
    assert output["status"] == "LAN_DEPLOYMENT_DIAGNOSIS_REJECTED"
    assert output["reason"] == "LAN_DEPLOYMENT_INVALID_PEER"
    assert output["ready"] is False
