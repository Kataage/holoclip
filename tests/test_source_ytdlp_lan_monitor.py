"""Offline LAN periodic monitor tests. No scheduler, live LAN or YouTube."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_monitor as monitor


def _args(tmp_path):
    return {
        "host": "192.168.1.21",
        "user": "safeuser",
        "known_hosts": tmp_path / "pc-b-pinned",
        "remote_repo": r"D:\AI\holoclip",
        "evidence_db": tmp_path / "evidence.sqlite3",
        "alert_file": tmp_path / "lan-alert.json",
    }


def _capture(alert, *, issues=None, count=3, incomplete=True):
    return {
        "diagnosis": ("LAN_DEPLOYMENT_GAPS_OBSERVED" if incomplete
                      else "LAN_DEPLOYMENT_OBSERVATIONS_MATCH"),
        "history": {
            "alert": alert,
            "repeated_issue_keys": issues or [],
            "retained_observations": count,
            "latest_observation": {"observed_at_unix": 1791633000 + count},
            "ready": False,
        },
    }


def test_one_shot_healthy_does_not_popup_or_touch_production_gate(tmp_path, monkeypatch):
    params = _args(tmp_path)
    calls = []
    monkeypatch.setattr(monitor, "capture_lan_evidence", lambda **kw: (
        calls.append(kw) or _capture("NONE", count=1, incomplete=False)
    ))
    monkeypatch.setattr(
        monitor, "_notify_windows", lambda: pytest.fail("healthy run must not notify"),
    )
    result = monitor.run_lan_monitor_once(**params)
    assert result["status"] == "LAN_MONITOR_OBSERVATION_RECORDED"
    assert result["desktop_notification_attempted"] is False
    assert result["alert_state_changed"] is True
    assert calls[0]["record"] is True
    assert calls[0]["include_pacing_trial"] is False
    assert result["lease_requested"] is False
    assert result["youtube_contacted"] is False
    assert result["ready"] is False
    alert = monitor.read_lan_monitor_alert(
        alert_file=params["alert_file"], evidence_db=params["evidence_db"],
    )
    assert alert["alert"] == "NONE"
    assert alert["status"] == "LAN_MONITOR_NO_ALERT_RECORDED"


def test_three_recorded_failures_notify_once_and_keep_durable_marker(tmp_path, monkeypatch):
    params = _args(tmp_path)
    stages = [
        _capture("NONE", count=1),
        _capture("NONE", count=2),
        _capture("REPEATED_SAME_LAN_DEFICITS", issues=["pc-b:remote_ssh"], count=3),
        _capture("REPEATED_SAME_LAN_DEFICITS", issues=["pc-b:remote_ssh"], count=4),
    ]
    monkeypatch.setattr(monitor, "capture_lan_evidence", lambda **kw: stages.pop(0))
    shown = []
    monkeypatch.setattr(monitor, "_notify_windows", lambda: shown.append("attempt") or True)
    a = monitor.run_lan_monitor_once(**params)
    b = monitor.run_lan_monitor_once(**params)
    c = monitor.run_lan_monitor_once(**params)
    d = monitor.run_lan_monitor_once(**params)
    assert a["alert"] == "NONE" and b["alert"] == "NONE"
    assert c["alert_state_changed"] is True
    assert c["desktop_notification_delivered"] is True
    assert d["alert_state_changed"] is False
    assert d["desktop_notification_attempted"] is False
    assert shown == ["attempt"]
    alert = monitor.read_lan_monitor_alert(
        alert_file=params["alert_file"], evidence_db=params["evidence_db"],
    )
    assert alert["alert"] == "REPEATED_SAME_LAN_DEFICITS"
    assert alert["issue_keys"] == ["pc-b:remote_ssh"]
    raw = params["alert_file"].read_bytes()
    for forbidden in (b"192.168.", b"safeuser", b"holoclip", b"known_hosts",
                      b"secret", b"password", b"D:\\"):
        assert forbidden not in raw
    assert c["ready"] is False and d["ready"] is False


def test_popup_failure_does_not_remove_local_record(tmp_path, monkeypatch):
    params = _args(tmp_path)
    monkeypatch.setattr(
        monitor, "capture_lan_evidence",
        lambda **kw: _capture("REPEATED_INCOMPLETE_LAN_DIAGNOSIS"),
    )
    monkeypatch.setattr(monitor, "_notify_windows", lambda: False)
    result = monitor.run_lan_monitor_once(**params)
    assert result["desktop_notification_attempted"] is True
    assert result["desktop_notification_delivered"] is False
    assert result["durable_alert_marker_updated"] is True
    assert monitor.read_lan_monitor_alert(
        alert_file=params["alert_file"], evidence_db=params["evidence_db"],
    )["alert"] == "REPEATED_INCOMPLETE_LAN_DIAGNOSIS"


def test_recovery_resets_alert_without_unwanted_popup(tmp_path, monkeypatch):
    params = _args(tmp_path)
    sequence = [
        _capture("REPEATED_INCOMPLETE_LAN_DIAGNOSIS"),
        _capture("NONE", count=5, incomplete=False),
        _capture("REPEATED_INCOMPLETE_LAN_DIAGNOSIS", count=8),
    ]
    monkeypatch.setattr(monitor, "capture_lan_evidence", lambda **kw: sequence.pop(0))
    shown = []
    monkeypatch.setattr(monitor, "_notify_windows", lambda: shown.append("show") or True)
    first = monitor.run_lan_monitor_once(**params)
    recovered = monitor.run_lan_monitor_once(**params)
    again = monitor.run_lan_monitor_once(**params)
    assert first["desktop_notification_attempted"] is True
    assert recovered["alert"] == "NONE"
    assert recovered["desktop_notification_attempted"] is False
    assert again["desktop_notification_attempted"] is True
    assert shown == ["show", "show"]


def test_corrupt_existing_marker_rejected_before_ssh(tmp_path, monkeypatch):
    params = _args(tmp_path)
    params["alert_file"].write_text('{"alert":"password=bad"}')
    monkeypatch.setattr(
        monitor, "capture_lan_evidence",
        lambda **kw: pytest.fail("corrupted alert blocks network capture"),
    )
    with pytest.raises(ValueError, match="LAN_MONITOR_ALERT_CORRUPT"):
        monitor.run_lan_monitor_once(**params)


@pytest.mark.parametrize("unknown", [
    {"alert": "LAN_WATCHER_CLAIMED_PRODUCTION_READY"},
    {"issue_keys": ["pc-b:ssh-secret"]},
    {"ready": True},
    {"observed_at_unix": "today"},
])
def test_untrusted_marker_fields_rejected(tmp_path, unknown):
    params = _args(tmp_path)
    marker = {
        "version": monitor._VERSION,
        "alert": "NONE",
        "issue_keys": [],
        "observed_at_unix": 1800000000,
        "ready": False,
    }
    marker.update(unknown)
    params["alert_file"].write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="LAN_MONITOR_ALERT_CORRUPT"):
        monitor.read_lan_monitor_alert(
            alert_file=params["alert_file"], evidence_db=params["evidence_db"],
        )


def test_missing_alert_does_not_invent_monitoring(tmp_path):
    params = _args(tmp_path)
    report = monitor.read_lan_monitor_alert(
        alert_file=params["alert_file"], evidence_db=params["evidence_db"],
    )
    assert report["status"] == "LAN_MONITOR_NEVER_RECORDED"
    assert report["continuous_monitoring_verified"] is False
    assert not params["alert_file"].exists()


def test_alert_path_cannot_overwrite_credential_or_history(tmp_path):
    with pytest.raises(ValueError, match="LAN_MONITOR_UNSAFE_ALERT_PATH"):
        monitor.read_lan_monitor_alert(
            alert_file=monitor.DEFAULT_SECRET_PATH,
            evidence_db=tmp_path / "evidence.sqlite3",
        )
    with pytest.raises(ValueError, match="LAN_MONITOR_UNSAFE_ALERT_PATH"):
        monitor.read_lan_monitor_alert(
            alert_file=tmp_path / "safe.sqlite3",
            evidence_db=tmp_path / "safe.sqlite3",
        )


def test_monitor_task_preview_never_registers_or_starts(tmp_path, monkeypatch):
    params = _args(tmp_path)
    calls = []
    monkeypatch.setattr(monitor, "_windows", lambda: True)
    monkeypatch.setattr(monitor, "_task_state", lambda name: "LAN_TASK_ABSENT_OBSERVED")
    monkeypatch.setattr(monitor, "_host_pin_state", lambda *a: "HOST_PIN_ENTRY_OBSERVED")
    monkeypatch.setattr(monitor, "_pinned_ssh_command", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        monitor.subprocess, "run", lambda *a, **kw: pytest.fail("preview cannot install"),
    )
    report = monitor.prepare_lan_monitor_task(**params)
    assert report["status"] == "LAN_MONITOR_TASK_PREVIEW_ELIGIBLE"
    assert report["task_registered"] is False
    assert report["task_started"] is False
    assert report["pacing_trial_automatically_enabled"] is False
    assert calls[0]["remote_probe"] == "onboard"


def test_opt_in_registers_once_after_second_absence_check(tmp_path, monkeypatch):
    params = _args(tmp_path)
    checks = []
    args = []
    monkeypatch.setattr(monitor, "_windows", lambda: True)
    monkeypatch.setattr(
        monitor, "_task_state",
        lambda name: checks.append(name) or "LAN_TASK_ABSENT_OBSERVED",
    )
    monkeypatch.setattr(monitor, "_host_pin_state", lambda *a: "HOST_PIN_ENTRY_OBSERVED")
    monkeypatch.setattr(monitor, "_pinned_ssh_command", lambda **kw: ["ssh"])
    monkeypatch.setattr(monitor.shutil, "which", lambda name: "mock-powershell")
    monkeypatch.setattr(
        monitor.subprocess, "run",
        lambda cmd, **kw: args.append((cmd, kw)) or SimpleNamespace(returncode=0),
    )
    report = monitor.prepare_lan_monitor_task(**params, apply=True)
    assert report["status"] == "LAN_MONITOR_TASK_REGISTERED_NOT_STARTED"
    assert report["task_registered"] is True
    assert report["task_started"] is False
    assert report["existing_task_overwritten"] is False
    assert report["pacing_trial_automatically_enabled"] is False
    assert len(checks) == 2
    assert len(args) == 1
    cmd = args[0][0]
    assert "-HostIp" in cmd and "-RemoteRepo" in cmd
    assert "-IntervalMinutes" in cmd
    assert "--include-pacing-trial" not in cmd
    assert args[0][1]["timeout"] == 45


@pytest.mark.parametrize("second", [
    "LAN_TASK_ALREADY_PRESENT", "LAN_TASK_ENUMERATION_UNAVAILABLE",
])
def test_task_conflict_blocks_even_if_first_check_succeeded(
    tmp_path, monkeypatch, second,
):
    params = _args(tmp_path)
    states = iter(["LAN_TASK_ABSENT_OBSERVED", second])
    monkeypatch.setattr(monitor, "_windows", lambda: True)
    monkeypatch.setattr(monitor, "_task_state", lambda name: next(states))
    monkeypatch.setattr(monitor, "_host_pin_state", lambda *a: "HOST_PIN_ENTRY_OBSERVED")
    monkeypatch.setattr(monitor, "_pinned_ssh_command", lambda **kw: ["ssh"])
    monkeypatch.setattr(
        monitor.subprocess, "run",
        lambda *a, **k: pytest.fail("Task conflict must never install"),
    )
    report = monitor.prepare_lan_monitor_task(**params, apply=True)
    assert report["status"] == "LAN_MONITOR_TASK_APPLY_BLOCKED"
    assert report["task_registered"] is False


def test_missing_pinned_host_key_fails_before_task_enumeration(tmp_path, monkeypatch):
    params = _args(tmp_path)
    monkeypatch.setattr(monitor, "_windows", lambda: True)
    monkeypatch.setattr(monitor, "_host_pin_state", lambda *a: "HOST_PIN_NOT_FOUND")
    monkeypatch.setattr(monitor, "_pinned_ssh_command", lambda **kw: ["ssh"])
    monkeypatch.setattr(
        monitor, "_task_state",
        lambda *a: pytest.fail("must reject host pin first"),
    )
    with pytest.raises(ValueError, match="LAN_MONITOR_PINNED_HOST_KEY_REQUIRED"):
        monitor.prepare_lan_monitor_task(**params, apply=True)


@pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "192.168.001.021", "1.2.3.4;run"])
def test_task_invalid_peer_rejected_before_ssh(tmp_path, monkeypatch, ip):
    params = _args(tmp_path)
    params["host"] = ip
    monkeypatch.setattr(
        monitor, "_pinned_ssh_command",
        lambda **kw: pytest.fail("invalid peer cannot reach SSH preparation"),
    )
    with pytest.raises(ValueError):
        monitor.prepare_lan_monitor_task(**params, apply=True)


def test_cli_alert_read_and_sanitized_monitor_failure(tmp_path, monkeypatch):
    params = _args(tmp_path)
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-monitor-alert",
        "--alert-file", str(params["alert_file"]),
        "--evidence-db", str(params["evidence_db"]),
    ])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "LAN_MONITOR_NEVER_RECORDED"
    monkeypatch.setattr(
        cli, "run_lan_monitor_once",
        lambda **kw: (_ for _ in ()).throw(ValueError("secret from SSH!")),
    )
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-monitor-run",
        "--host", params["host"], "--user", params["user"],
        "--known-hosts", str(params["known_hosts"]),
        "--remote-repo", params["remote_repo"],
        "--evidence-db", str(params["evidence_db"]),
        "--alert-file", str(params["alert_file"]),
    ])
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "LAN_MONITOR_RUN_REJECTED"
    assert "secret from SSH" not in result.stdout
