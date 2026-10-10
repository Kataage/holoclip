"""Fail-closed first-run LAN monitoring audit and scheduler evidence tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_first_run_check as first

_NOW = 1800003600


def _doc(tmp_path: Path):
    return {
        "host": "192.168.1.21", "user": "remote_user",
        "known_hosts": str(tmp_path / "pinned"),
        "remote_repo": r"D:\Private\holoclip",
        "ssh_port": 22, "coordinator_port": 8878,
        "interval_minutes": 60, "task_name": "Holoclip yt-dlp LAN Monitor",
        "evidence_db": str(tmp_path / "history.sqlite3"),
        "alert_file": str(tmp_path / "alert.json"),
    }


def _history(*, when=_NOW - 60, positive=True):
    return {
        "status": "LAN_EVIDENCE_HISTORY_RECORDED",
        "latest_observation": {
            "observed_at_unix": when,
            "diagnosis": ("LAN_DEPLOYMENT_OBSERVATIONS_MATCH"
                          if positive else "LAN_DEPLOYMENT_REMOTE_UNVERIFIED"),
            "scope": "PAIRED" if positive else "PC_A_ONLY",
            "issue_keys": [] if positive else ["pc-b:remote_ssh"],
        },
    }


def _task(*, status="LAN_FIRST_RUN_TASK_CONFIG_MATCHED",
          execution="LAN_FIRST_RUN_LAST_RUN_SUCCEEDED",
          when=_NOW - 100):
    return {"status": status, "task_execution": execution, "last_run_unix": when}


def _arrange(tmp_path, monkeypatch, *, task=None, hist=None, alert="NONE"):
    doc = _doc(tmp_path)
    monkeypatch.setattr(first, "read_operator_profile", lambda path: doc)
    monkeypatch.setattr(first, "_task_observation", lambda d: task if task is not None else _task())
    monkeypatch.setattr(
        first, "read_lan_evidence_history", lambda path: hist if hist is not None else _history(),
    )
    monkeypatch.setattr(
        first, "read_lan_monitor_alert", lambda **kwargs: {
            "status": "LAN_MONITOR_ALERT_PRESENT" if alert != "NONE"
            else "LAN_MONITOR_NO_ALERT_RECORDED",
            "alert": alert,
        },
    )
    monkeypatch.setattr(first.time, "time", lambda: _NOW)
    return doc


def _safety(report):
    assert report["ready"] is False
    assert report["production_ready"] is False
    assert report["lease_requested"] is False
    assert report["youtube_contacted"] is False
    assert report["changes_applied"] is False
    assert report["continuous_monitoring_verified"] is False
    assert report["physical_two_host_attested"] is False
    as_json = json.dumps(report)
    assert "remote_user" not in as_json
    assert "192.168." not in as_json
    assert r"D:\Private" not in as_json


def test_healthy_local_evidence_correlates_without_peer_or_media(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    monkeypatch.setattr(
        first, "read_operator_profile",
        lambda path: _doc(tmp_path),
    )
    result = first.inspect_lan_first_run(profile_file=tmp_path / "operator.json")
    assert result["status"] == "LAN_FIRST_RUN_LOCAL_MONITOR_OBSERVED"
    assert result["task"] == "LAN_FIRST_RUN_TASK_CONFIG_MATCHED"
    assert result["task_execution"] == "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED"
    assert result["history"] == "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE"
    assert result["scheduler_and_history_consistent"] is True
    assert result["local_monitor_observations_current"] is True
    assert result["signed_peer"] == "NOT_REQUESTED"
    assert result["next_actions"] == []
    _safety(result)


def test_explicit_signed_peer_verified_separately(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    seen = []
    from holoclip import source_ytdlp_lan_deployment_diagnosis as diagnosis
    monkeypatch.setattr(diagnosis, "diagnose_lan_deployment", lambda **kw: (
        seen.append(kw) or {
            "status": "LAN_DEPLOYMENT_OBSERVATIONS_MATCH", "ready": False,
        }
    ))
    result = first.inspect_lan_first_run(check_remote=True)
    assert result["status"] == "LAN_FIRST_RUN_LOCAL_AND_PEER_OBSERVED"
    assert result["signed_peer"] == "LAN_FIRST_RUN_SIGNED_PEER_OBSERVED"
    assert seen[0]["host"] == "192.168.1.21"
    assert seen[0]["user"] == "remote_user"
    assert result["commands_run"] == [
        "READ_ONLY_LOCAL_INSPECTION", "PINNED_SIGNED_PC_B_DIAGNOSIS",
    ]
    _safety(result)


def test_remote_failure_retains_positive_local_and_reports_next_action(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    from holoclip import source_ytdlp_lan_deployment_diagnosis as diagnosis
    monkeypatch.setattr(
        diagnosis, "diagnose_lan_deployment",
        lambda **kw: (_ for _ in ()).throw(ValueError("SSH_SECRET_RAW_SHOULD_NOT_LEAK")),
    )
    result = first.inspect_lan_first_run(check_remote=True)
    assert result["status"] == "LAN_FIRST_RUN_LOCAL_MONITOR_OBSERVED"
    assert result["signed_peer"] == "LAN_FIRST_RUN_SIGNED_PEER_UNVERIFIED"
    assert "examples-ytdlp-lan-operator-status --diagnose" in result["next_actions"]
    assert "SSH_SECRET" not in json.dumps(result)
    _safety(result)


@pytest.mark.parametrize(("task", "expected"), [
    (_task(status="LAN_FIRST_RUN_TASK_ABSENT",
           execution="LAN_FIRST_RUN_NEVER_RUN", when=None),
     "examples-ytdlp-lan-operator-status --register-monitor"),
    (_task(status="LAN_FIRST_RUN_TASK_DISABLED",
           execution="LAN_FIRST_RUN_LAST_RUN_UNVERIFIED", when=None),
     "INSPECT_EXISTING_TASK_IN_WINDOWS_TASK_SCHEDULER"),
    (_task(status="LAN_FIRST_RUN_TASK_CONFIG_MISMATCH",
           execution="LAN_FIRST_RUN_LAST_RUN_UNVERIFIED", when=None),
     "INSPECT_EXISTING_TASK_IN_WINDOWS_TASK_SCHEDULER"),
    (_task(status="LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE",
           execution="LAN_FIRST_RUN_LAST_RUN_UNVERIFIED", when=None),
     "INSPECT_EXISTING_TASK_IN_WINDOWS_TASK_SCHEDULER"),
    (_task(execution="LAN_FIRST_RUN_LAST_RUN_FAILED"),
     "INSPECT_MONITOR_TASK_LAST_RUN_AND_LOCAL_WINDOWS_SESSION"),
    (_task(execution="LAN_FIRST_RUN_NEVER_RUN", when=None),
     "INSPECT_MONITOR_TASK_LAST_RUN_AND_LOCAL_WINDOWS_SESSION"),
])
def test_task_problems_cannot_become_success(tmp_path, monkeypatch, task, expected):
    _arrange(tmp_path, monkeypatch, task=task)
    report = first.inspect_lan_first_run()
    assert report["status"] == "LAN_FIRST_RUN_ACTION_REQUIRED"
    assert report["scheduler_and_history_consistent"] is False
    assert expected in report["next_actions"]
    _safety(report)


@pytest.mark.parametrize(("history", "expected"), [
    (_history(when=_NOW - 8_000), "LAN_FIRST_RUN_HISTORY_STALE"),
    (_history(when=_NOW + 180), "LAN_FIRST_RUN_HISTORY_CLOCK_UNTRUSTED"),
    (_history(when=_NOW - 60, positive=False), "LAN_FIRST_RUN_HISTORY_FRESH_NEGATIVE"),
    ({"status": "LAN_EVIDENCE_HISTORY_EMPTY"}, "LAN_FIRST_RUN_HISTORY_MISSING"),
    ({"status": "LAN_EVIDENCE_HISTORY_RECORDED",
      "latest_observation": {"observed_at_unix": "not-a-time"}},
     "LAN_FIRST_RUN_HISTORY_UNTRUSTED"),
])
def test_history_freshness_or_negative_state_blocks_positive_result(
    tmp_path, monkeypatch, history, expected,
):
    _arrange(tmp_path, monkeypatch, hist=history)
    report = first.inspect_lan_first_run()
    assert report["history"] == expected
    assert report["local_monitor_observations_current"] is False
    assert report["status"] == "LAN_FIRST_RUN_ACTION_REQUIRED"
    assert "examples-ytdlp-lan-evidence-history" in report["next_actions"]
    _safety(report)


def test_last_task_run_and_history_timestamp_mismatch(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch, task=_task(when=_NOW - 1800), hist=_history(when=_NOW - 30))
    report = first.inspect_lan_first_run()
    assert report["history"] == "LAN_FIRST_RUN_HISTORY_FRESH_POSITIVE"
    assert report["scheduler_and_history_consistent"] is False
    assert report["status"] == "LAN_FIRST_RUN_ACTION_REQUIRED"
    _safety(report)


def test_active_repeated_alert_not_reported_healthy(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch, alert="REPEATED_SAME_LAN_DEFICITS")
    report = first.inspect_lan_first_run()
    assert report["status"] == "LAN_FIRST_RUN_ACTION_REQUIRED"
    assert report["alert"] == "REPEATED_SAME_LAN_DEFICITS"
    assert "examples-ytdlp-lan-monitor-alert" in report["next_actions"]
    _safety(report)


def test_missing_profile_does_not_probe_task_network_or_history(tmp_path, monkeypatch):
    monkeypatch.setattr(first, "read_operator_profile", lambda path: None)
    monkeypatch.setattr(first, "_task_observation",
                        lambda d: pytest.fail("task probe cannot run without profile"))
    monkeypatch.setattr(first, "read_lan_evidence_history",
                        lambda p: pytest.fail("no history without config"))
    report = first.inspect_lan_first_run(check_remote=True)
    assert report["status"] == "LAN_FIRST_RUN_PROFILE_MISSING"
    assert report["next_actions"] == ["examples-ytdlp-lan-operator-setup"]
    assert report["commands_run"] == ["READ_ONLY_LOCAL_INSPECTION"]
    _safety(report)


def test_corrupted_profile_sanitized_without_raw_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        first, "read_operator_profile",
        lambda path: (_ for _ in ()).throw(ValueError("SECRET_MACHINE_PATH")),
    )
    report = first.inspect_lan_first_run(check_remote=True)
    assert report["status"] == "LAN_FIRST_RUN_PROFILE_UNTRUSTED"
    assert "SECRET_MACHINE_PATH" not in json.dumps(report)
    _safety(report)


@pytest.mark.parametrize("raw", [
    b"invalid",
    b'{"status":"LAN_FIRST_RUN_TASK_CONFIG_MATCHED","task_execution":"LAN_FIRST_RUN_LAST_RUN_SUCCEEDED","last_run_unix":1800003500,"ready":true}',
    b'{"status":"LAN_FIRST_RUN_TASK_CONFIG_MATCHED","task_execution":"LAN_FIRST_RUN_LAST_RUN_SUCCEEDED","last_run_unix":null,"ready":false}',
    b'{"status":"LAN_FIRST_RUN_TASK_CONFIG_MATCHED","task_execution":"UNEXPECTED","last_run_unix":null,"ready":false}',
    b'{"status":"LAN_FIRST_RUN_TASK_CONFIG_MATCHED","task_execution":"LAN_FIRST_RUN_LAST_RUN_SUCCEEDED","last_run_unix":1800003500,"ready":false,"password":"secret"}',
    b"x" * 3000,
])
def test_invalid_windows_task_probe_never_trusted(tmp_path, monkeypatch, raw):
    probe = tmp_path / "read-only-probe.ps1"
    probe.write_text("fake", encoding="ascii")
    monkeypatch.setattr(first, "_PROBE", probe)
    monkeypatch.setattr(first, "_windows", lambda: True)
    monkeypatch.setattr(first.shutil, "which", lambda exe: "mock-powershell.exe")
    monkeypatch.setattr(first.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=raw, stderr=b"SECRET_SHOULD_NOT_APPEAR",
    ))
    result = first._task_observation(_doc(tmp_path))
    assert result["status"] == "LAN_FIRST_RUN_TASK_INSPECTION_UNAVAILABLE"
    assert "SECRET" not in json.dumps(result)


def test_valid_bounded_windows_task_probe_receipt(tmp_path, monkeypatch):
    probe = tmp_path / "probe.ps1"
    probe.write_text("fake")
    monkeypatch.setattr(first, "_PROBE", probe)
    monkeypatch.setattr(first, "_windows", lambda: True)
    monkeypatch.setattr(first.shutil, "which", lambda exe: "mock-powershell.exe")
    seen = []
    doc = {
        "status": "LAN_FIRST_RUN_TASK_CONFIG_MATCHED",
        "task_execution": "LAN_FIRST_RUN_LAST_RUN_SUCCEEDED",
        "last_run_unix": _NOW - 100,
        "ready": False,
    }
    monkeypatch.setattr(first.subprocess, "run", lambda command, **kw: (
        seen.append((command, kw)) or SimpleNamespace(
            returncode=0, stdout=json.dumps(doc).encode("ascii"), stderr=b"",
        )
    ))
    response = first._task_observation(_doc(tmp_path))
    assert response["status"] == "LAN_FIRST_RUN_TASK_CONFIG_MATCHED"
    assert response["last_run_unix"] == _NOW - 100
    assert "-HostIp" in seen[0][0]
    assert "-SshUser" in seen[0][0]
    assert seen[0][1]["timeout"] == 30


def test_cli_reports_negative_check_as_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(first, "read_operator_profile", lambda path: None)
    outcome = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-first-run-check", "--profile-file",
        str(tmp_path / "missing.json"),
    ])
    assert outcome.exit_code == 6
    assert json.loads(outcome.stdout)["status"] == "LAN_FIRST_RUN_PROFILE_MISSING"


def test_cli_local_positive_without_remote(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    outcome = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-first-run-check"])
    assert outcome.exit_code == 0, outcome.output
    assert json.loads(outcome.stdout)["ready"] is False
