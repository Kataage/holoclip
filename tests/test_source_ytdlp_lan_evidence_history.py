"""LAN evidence history tests: fixed-code retention, no saved identity or media.

Tests use only synthetic reduced diagnostics and mocked optional live trial.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_evidence_history as evidence
from holoclip.source_ytdlp_lan_deployment_diagnosis import _COMMANDS, _EXPECTED
from holoclip.source_ytdlp_lan_pacing_trial import _base_report

HOST = "192.168.1.21"


def _codes(role: str) -> dict:
    return {
        "signed_coordinator_status": "AUTHENTICATED_LOCAL_LAN_COORDINATOR",
        "local_credential": "LAN_CREDENTIAL_VALID",
        "credential_acl": "LAN_CREDENTIAL_ACL_OBSERVED",
        "coordinator_task": "RUNNING_OBSERVED" if role == "pc-a" else "NOT_APPLICABLE",
        "coordinator_listener": "LAN_LISTENER_OBSERVED" if role == "pc-a" else "NOT_APPLICABLE",
        "firewall_rule": (
            "EXACT_PEER_ALLOW_RULE_OBSERVED" if role == "pc-a" else "NOT_APPLICABLE"
        ),
        "sshd_service": "NOT_APPLICABLE",
        "ssh_host_pin": "HOST_PIN_NOT_SUPPLIED",
    }


def _diagnosis(*, firewall: str | None = None, remote_failed: bool = False) -> dict:
    a_codes = _codes("pc-a")
    if firewall is not None:
        a_codes["firewall_rule"] = firewall
    a = {
        "role": "pc-a",
        "status": "LOCAL_SETUP_OBSERVATIONS_COMPLETE" if firewall is None else "LOCAL_SETUP_INCOMPLETE",
        "checks": a_codes,
    }
    b = None if remote_failed else {
        "role": "pc-b", "status": "LOCAL_SETUP_OBSERVATIONS_COMPLETE",
        "checks": _codes("pc-b"),
    }
    repairs = []
    if firewall is not None:
        repairs.append({
            "priority": len(repairs) + 1, "role": "pc-a",
            "check": "firewall_rule",
            "observed": firewall,
            "expected": _EXPECTED["pc-a"]["firewall_rule"],
            "suggested_command": _COMMANDS[("pc-a", "firewall_rule")],
            "automatically_modified": False,
        })
    if remote_failed:
        repairs.append({
            "priority": len(repairs) + 1, "role": "pc-b",
            "check": "remote_ssh",
            "observed": "LAN_ONBOARD_REMOTE_SSH_FAILED",
            "expected": "PINNED_PUBLIC_KEY_SSH_REPORT_VALIDATED",
            "suggested_command": "examples-ytdlp-lan-onboard-check",
            "automatically_modified": False,
        })
    return {
        "status": (
            "LAN_DEPLOYMENT_REMOTE_UNVERIFIED" if remote_failed else
            "LAN_DEPLOYMENT_GAPS_OBSERVED" if repairs else
            "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
        ),
        "pc_a": a, "pc_b": b,
        "pair_observation": None if remote_failed else "LAN_PAIR_OBSERVATIONS_MATCH",
        "remote_failure": "LAN_ONBOARD_REMOTE_SSH_FAILED" if remote_failed else None,
        "remote_ssh_report_validated": not remote_failed,
        "repair_plan": repairs,
        "local_state_modified": False,
        "lease_requested": False,
        "youtube_contacted": False,
        "two_host_verified": False,
        "firewall_effective_policy_verified": False,
        "ready": False,
    }


def _trial_success():
    trial = _base_report(applied=True)
    trial.update({
        "status": "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED",
        "lease_requested": True, "lease_acquired": True,
        "lease_release_confirmed": True,
        "regular_pacing_gap_created": True,
        "held_witness": "LAN_PACING_WITNESS_HELD_DENIED",
        "gap_witness": "LAN_PACING_WITNESS_GAP_DENIED",
        "same_coordinator_state_observed": True,
        "changes_may_be_partial": False,
    })
    return trial


def _args(tmp_path: Path):
    return {
        "host": HOST, "user": "ssh_name_private",
        "known_hosts": tmp_path / "private-hosts",
        "remote_repo": r"D:\PrivateUser\holoclip",
        "evidence_db": tmp_path / "history.sqlite3",
    }


def _safe(result):
    assert result["ready"] is False
    assert result["production_ready"] is False
    assert result["real_two_host_pacing_qualified"] is False
    assert result["physical_two_host_identity_verified"] is False


def test_preview_collects_only_redacted_codes_and_does_not_write(tmp_path, monkeypatch):
    inputs = _args(tmp_path)
    monkeypatch.setattr(evidence, "diagnose_lan_deployment", lambda **kwargs: _diagnosis())
    monkeypatch.setattr(
        evidence, "trial_lan_pacing",
        lambda **kwargs: pytest.fail("trial must not run in preview"),
    )
    result = evidence.capture_lan_evidence(**inputs)
    assert result["status"] == "LAN_EVIDENCE_PREVIEW_ONLY"
    assert result["diagnosis"] == "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
    assert result["observation_scope"] == "PAIRED"
    assert result["pacing_trial"] == "NOT_RUN"
    assert result["database_written"] is False
    assert result["lease_trial_requested"] is False
    assert not inputs["evidence_db"].exists()
    assert "ssh_name_private" not in json.dumps(result)
    assert "PrivateUser" not in json.dumps(result)
    _safe(result)


def test_sanitized_history_records_and_detects_repeated_risk_without_paths(tmp_path):
    db = tmp_path / "safe.sqlite3"
    insecure = _diagnosis(firewall="BROAD_INBOUND_ALLOW_RULE_OBSERVED")
    for iteration in range(3):
        result = evidence.record_lan_evidence(path=db, diagnosis=insecure)
        assert result["retained_observations"] == iteration + 1
    assert result["latest_observation"]["issue_keys"] == ["pc-a:firewall_rule"]
    assert result["consecutive_incomplete_observations"] == 3
    assert result["repeated_issue_keys"] == ["pc-a:firewall_rule"]
    assert result["alert"] == "REPEATED_SAME_LAN_DEFICITS"
    assert result["comparison"] == "NO_CHANGE_OBSERVED"
    assert result["calendar_uptime_verified"] is False
    _safe(result)
    raw = db.read_bytes()
    for banned in (b"192.168.", b"PrivateUser", b"ssh_name_private",
                   b"BROAD_INBOUND_ALLOW_RULE_OBSERVED", b"password", b"secret"):
        assert banned not in raw
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT diagnosis, scope, issues, trial FROM lan_evidence").fetchall()
    assert len(stored) == 3
    assert all(item[3] == "NOT_RUN" for item in stored)


def test_improved_check_count_only_when_same_observation_scope(tmp_path):
    db = tmp_path / "history.sqlite3"
    evidence.record_lan_evidence(
        path=db, diagnosis=_diagnosis(firewall="BROAD_INBOUND_ALLOW_RULE_OBSERVED"),
    )
    updated = evidence.record_lan_evidence(path=db, diagnosis=_diagnosis())
    assert updated["comparison"] == "FEWER_ISSUES_OBSERVED"
    assert updated["consecutive_incomplete_observations"] == 0
    assert updated["alert"] == "NONE"
    partial = evidence.record_lan_evidence(
        path=db, diagnosis=_diagnosis(remote_failed=True),
    )
    assert partial["comparison"] == "INCOMPARABLE_OBSERVATION_SCOPE"
    assert partial["latest_observation"]["scope"] == "PC_A_ONLY"
    _safe(partial)


def test_bounded_retention_on_many_observations(tmp_path):
    db = tmp_path / "history.sqlite3"
    for _ in range(evidence.MAX_EVENTS + 4):
        latest = evidence.record_lan_evidence(path=db, diagnosis=_diagnosis())
    assert latest["retained_observations"] == evidence.MAX_EVENTS
    assert len(latest["recent_observations"]) == 10
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM lan_evidence").fetchone()[0]
    assert count == evidence.MAX_EVENTS


def test_no_trial_when_diagnosis_incomplete_even_with_explicit_flags(tmp_path, monkeypatch):
    inputs = _args(tmp_path)
    monkeypatch.setattr(
        evidence, "diagnose_lan_deployment",
        lambda **kwargs: _diagnosis(remote_failed=True),
    )
    monkeypatch.setattr(
        evidence, "trial_lan_pacing",
        lambda **kwargs: pytest.fail("must not reserve on partial diagnostics"),
    )
    outcome = evidence.capture_lan_evidence(
        **inputs, record=True, include_pacing_trial=True,
    )
    assert outcome["status"] == "LAN_EVIDENCE_RECORDED"
    assert outcome["pacing_trial"] == "NOT_RUN"
    assert outcome["pacing_trial_skipped_for_incomplete_setup"] is True
    assert outcome["lease_trial_requested"] is False
    assert outcome["database_written"] is True
    assert outcome["history"]["latest_observation"]["scope"] == "PC_A_ONLY"
    _safe(outcome)


def test_successful_trial_can_be_saved_without_claiming_host_attestation(tmp_path, monkeypatch):
    inputs = _args(tmp_path)
    monkeypatch.setattr(evidence, "diagnose_lan_deployment", lambda **kwargs: _diagnosis())
    called = []
    def test_trial(**kwargs):
        called.append(kwargs)
        return _trial_success()
    monkeypatch.setattr(evidence, "trial_lan_pacing", test_trial)
    result = evidence.capture_lan_evidence(
        **inputs, record=True, include_pacing_trial=True,
    )
    assert called[0]["apply"] is True
    assert result["pacing_trial"] == "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED"
    assert result["history"]["latest_observation"]["pacing_trial"] == (
        "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED"
    )
    assert result["lease_trial_requested"] is True
    _safe(result)


def test_trial_is_refused_without_explicit_record_before_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setattr(
        evidence, "diagnose_lan_deployment",
        lambda **kwargs: pytest.fail("must reject before SSH"),
    )
    with pytest.raises(ValueError, match="LAN_EVIDENCE_TRIAL_REQUIRES_RECORD"):
        evidence.capture_lan_evidence(
            **_args(tmp_path), record=False, include_pacing_trial=True,
        )


@pytest.mark.parametrize("attack", [
    "ready_true", "secret_in_observed", "unknown_code", "positive_without_pair",
    "untrusted_remote_action", "invalid_flag",
])
def test_untrusted_reports_are_not_saved(tmp_path, attack):
    db = tmp_path / "events.sqlite3"
    doc = _diagnosis(firewall="BROAD_INBOUND_ALLOW_RULE_OBSERVED")
    if attack == "ready_true":
        doc["ready"] = True
    elif attack == "secret_in_observed":
        doc["repair_plan"][0]["observed"] = "password=PRIVATE_KEY"
    elif attack == "unknown_code":
        doc["pc_a"]["checks"]["firewall_rule"] = "CUSTOM_UNVERIFIED"
    elif attack == "positive_without_pair":
        doc["status"] = "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
    elif attack == "untrusted_remote_action":
        doc["repair_plan"][0]["suggested_command"] = "rm --force secret"
    elif attack == "invalid_flag":
        doc["lease_requested"] = True
    with pytest.raises(ValueError, match="LAN_EVIDENCE_UNTRUSTED_DIAGNOSIS"):
        evidence.record_lan_evidence(path=db, diagnosis=doc)
    assert not db.exists()


def test_false_claiming_trial_is_never_recorded(tmp_path):
    bad = _trial_success()
    bad["lease_release_confirmed"] = False
    with pytest.raises(ValueError, match="LAN_EVIDENCE_UNTRUSTED_TRIAL"):
        evidence.record_lan_evidence(
            path=tmp_path / "report.sqlite3", diagnosis=_diagnosis(), trial=bad,
        )


def test_corrupted_history_does_not_return_untrusted_code_or_mutate(tmp_path):
    db = tmp_path / "events.sqlite3"
    evidence.record_lan_evidence(path=db, diagnosis=_diagnosis())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE lan_evidence SET issues=? WHERE id=1",
            ('["password=attacker-secret"]',),
        )
    before = db.read_bytes()
    with pytest.raises(ValueError, match="LAN_EVIDENCE_HISTORY_CORRUPT"):
        evidence.read_lan_evidence_history(db)
    with pytest.raises(ValueError, match="LAN_EVIDENCE_RECORD_FAILED"):
        evidence.record_lan_evidence(path=db, diagnosis=_diagnosis())
    assert db.read_bytes() == before


def test_database_symlink_and_coordinator_state_paths_rejected(tmp_path):
    db = tmp_path / "events.sqlite3"
    db.write_bytes(b"")
    link = tmp_path / "symlink.sqlite3"
    try:
        link.symlink_to(db)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError, match="LAN_EVIDENCE_UNSAFE_STORAGE"):
        evidence.read_lan_evidence_history(link)
    with pytest.raises(ValueError, match="LAN_EVIDENCE_UNSAFE_STORAGE"):
        evidence.record_lan_evidence(
            path=evidence.DEFAULT_COORDINATOR_STATE, diagnosis=_diagnosis(),
        )


def test_empty_history_read_does_not_create_file(tmp_path):
    db = tmp_path / "missing.sqlite3"
    history = evidence.read_lan_evidence_history(db)
    assert history["status"] == "LAN_EVIDENCE_HISTORY_EMPTY"
    assert history["retained_observations"] == 0
    assert history["latest_observation"] is None
    assert not db.exists()
    _safe(history)


def test_cli_history_and_preview_expose_only_safe_codes(tmp_path, monkeypatch):
    inputs = _args(tmp_path)
    monkeypatch.setattr(cli, "capture_lan_evidence", lambda **kwargs: {
        "status": "LAN_EVIDENCE_PREVIEW_ONLY",
        "diagnosis": "LAN_DEPLOYMENT_OBSERVATIONS_MATCH",
        "ready": False,
    })
    cli_result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-evidence-capture",
        "--host", HOST, "--user", "testuser",
        "--known-hosts", str(inputs["known_hosts"]),
        "--remote-repo", inputs["remote_repo"],
    ])
    assert cli_result.exit_code == 0
    assert json.loads(cli_result.stdout)["ready"] is False
    hist = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-evidence-history",
        "--evidence-db", str(tmp_path / "absent.sqlite3"),
    ])
    assert hist.exit_code == 0
    assert json.loads(hist.stdout)["status"] == "LAN_EVIDENCE_HISTORY_EMPTY"


def test_cli_incomplete_capture_exits_nonzero_without_losing_sanitized_result(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(cli, "capture_lan_evidence", lambda **kwargs: {
        "status": "LAN_EVIDENCE_RECORDED", "diagnosis": "LAN_DEPLOYMENT_REMOTE_UNVERIFIED",
        "ready": False,
    })
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-evidence-capture",
        "--host", HOST, "--user", "testuser",
        "--known-hosts", str(tmp_path / "host-pin"),
        "--remote-repo", r"D:\AI\holoclip", "--record",
    ])
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "LAN_EVIDENCE_RECORDED"
