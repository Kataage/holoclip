"""Operator profile safety: no credentials, no task overwrite, no network preview."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_operator_profile as operator


def _inputs(tmp_path: Path) -> dict:
    pinned = tmp_path / "pc-b-pinned"
    pinned.write_text("192.168.1.21 ssh-ed25519 MOCK_ONLY\n", encoding="ascii")
    return {
        "host": "192.168.1.21",
        "user": "safeuser",
        "known_hosts": pinned,
        "remote_repo": r"D:\AI\holoclip",
        "profile_file": tmp_path / "operator.json",
        "evidence_db": tmp_path / "evidence.sqlite3",
        "alert_file": tmp_path / "alert.json",
    }


def _mock_local(monkeypatch):
    seen = []
    monkeypatch.setattr(operator, "_pinned_ssh_command",
                        lambda **kwargs: seen.append(("pin", kwargs["remote_probe"])) or ["ssh"])
    monkeypatch.setattr(operator, "_host_pin_state",
                        lambda *args: "HOST_PIN_ENTRY_OBSERVED")
    monkeypatch.setattr(operator, "_windows", lambda: True)
    monkeypatch.setattr(operator, "_secure_windows_path",
                        lambda path, *, apply: seen.append(("acl", apply)) or True)
    monkeypatch.setattr(operator, "_task_state",
                        lambda name: "LAN_TASK_ABSENT_OBSERVED")
    return seen


def _safe(result):
    assert result["ready"] is False
    assert result["two_host_verified"] is False
    assert result["lease_requested"] is False
    assert result["youtube_contacted"] is False
    assert result["credentials_stored_in_profile"] is False
    assert result["ssh_fingerprint_independently_verified"] is False


def test_profile_preview_is_readonly_and_credential_free(tmp_path, monkeypatch):
    seen = _mock_local(monkeypatch)
    opts = _inputs(tmp_path)
    report = operator.prepare_operator_profile(**opts)
    assert report["status"] == "LAN_OPERATOR_PREVIEW_ELIGIBLE"
    assert report["profile"] == "LAN_OPERATOR_PROFILE_ABSENT"
    assert report["profile_saved"] is False
    assert seen == [("pin", "onboard")]
    assert not opts["profile_file"].exists()
    _safe(report)


def test_profile_creation_is_atomic_nonsecret_and_idempotent(tmp_path, monkeypatch):
    seen = _mock_local(monkeypatch)
    opts = _inputs(tmp_path)
    first = operator.prepare_operator_profile(**opts, apply=True)
    assert first["status"] == "LAN_OPERATOR_PREPARED_NOT_STARTED"
    assert first["profile_saved"] is True
    assert ("acl", True) in seen
    assert first["monitor_task_registered"] is False
    raw = opts["profile_file"].read_text(encoding="utf-8")
    for forbidden in ("supersecret", "credential_bytes", "lease_token", "host_key_bytes"):
        assert forbidden not in raw
    assert json.loads(raw)["ready"] is False
    assert json.loads(raw)["host"] == "192.168.1.21"
    second = operator.prepare_operator_profile(**opts, apply=True)
    assert second["status"] == "LAN_OPERATOR_PREPARED_NOT_STARTED"
    assert second["profile_saved"] is False
    assert second["profile"] == "LAN_OPERATOR_PROFILE_SAVED"
    assert raw == opts["profile_file"].read_text(encoding="utf-8")
    _safe(first)


def test_profile_apply_and_monitor_task_are_ordered_after_full_preflight(
    tmp_path, monkeypatch,
):
    seen = _mock_local(monkeypatch)
    opts = _inputs(tmp_path)
    def monitor(**kwargs):
        seen.append(("monitor", kwargs["apply"]))
        return {
            "status": ("LAN_MONITOR_TASK_REGISTERED_NOT_STARTED"
                       if kwargs["apply"] else "LAN_MONITOR_TASK_PREVIEW_ELIGIBLE"),
            "task_registered": kwargs["apply"],
        }
    monkeypatch.setattr(operator, "prepare_lan_monitor_task", monitor)
    result = operator.prepare_operator_profile(
        **opts, apply=True, register_monitor=True,
    )
    assert result["status"] == "LAN_OPERATOR_PREPARED_NOT_STARTED"
    assert result["monitor_task_registered"] is True
    assert result["monitor_task_started"] is False
    assert seen == [
        ("pin", "onboard"), ("monitor", False),
        ("acl", True), ("monitor", True),
    ]
    _safe(result)


def test_monitor_preflight_blocked_never_writes_profile(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    monkeypatch.setattr(operator, "prepare_lan_monitor_task", lambda **kwargs: {
        "status": "LAN_MONITOR_TASK_PREVIEW_BLOCKED", "task_registered": False,
    })
    result = operator.prepare_operator_profile(
        **opts, apply=True, register_monitor=True,
    )
    assert result["status"] == "LAN_OPERATOR_PREVIEW_BLOCKED"
    assert result["monitor"] == "LAN_MONITOR_TASK_PREVIEW_BLOCKED"
    assert result["profile_saved"] is False
    assert not opts["profile_file"].exists()
    _safe(result)


def test_monitor_failure_after_profile_write_reports_partial(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    def monitor(**kw):
        if not kw["apply"]:
            return {"status": "LAN_MONITOR_TASK_PREVIEW_ELIGIBLE"}
        raise ValueError("sensitive path from PowerShell")
    monkeypatch.setattr(operator, "prepare_lan_monitor_task", monitor)
    result = operator.prepare_operator_profile(
        **opts, apply=True, register_monitor=True,
    )
    assert result["status"] == "LAN_OPERATOR_APPLY_UNCONFIRMED"
    assert result["profile_saved"] is True
    assert result["changes_may_be_partial"] is True
    assert "sensitive path" not in json.dumps(result)
    assert opts["profile_file"].exists()
    _safe(result)


def test_different_existing_profile_requires_explicit_update(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    original = opts["profile_file"].read_bytes()
    changed = {**opts, "host": "192.168.1.22"}
    report = operator.prepare_operator_profile(**changed, apply=True)
    assert report["status"] == "LAN_OPERATOR_PREVIEW_BLOCKED"
    assert report["profile"] == "LAN_OPERATOR_PROFILE_DIFFERENT"
    assert opts["profile_file"].read_bytes() == original
    _safe(report)


def test_update_fails_closed_if_old_monitor_task_present(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    original = opts["profile_file"].read_bytes()
    monkeypatch.setattr(operator, "_task_state", lambda name: "LAN_TASK_ALREADY_PRESENT")
    report = operator.prepare_operator_profile(
        **{**opts, "host": "192.168.1.22"}, apply=True, update=True,
    )
    assert report["status"] == "LAN_OPERATOR_PREVIEW_BLOCKED"
    assert report["profile"] == "LAN_OPERATOR_EXISTING_MONITOR_TASK_BLOCKS_UPDATE"
    assert opts["profile_file"].read_bytes() == original
    _safe(report)


def test_explicit_update_on_absent_monitor_changes_host_without_overwriting_task(
    tmp_path, monkeypatch,
):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    changed = {**opts, "host": "192.168.1.22", "interval_minutes": 120}
    report = operator.prepare_operator_profile(**changed, apply=True, update=True)
    assert report["status"] == "LAN_OPERATOR_PREPARED_NOT_STARTED"
    assert report["profile_saved"] is True
    loaded = operator.read_operator_profile(opts["profile_file"])
    assert loaded["host"] == "192.168.1.22"
    assert loaded["interval_minutes"] == 120
    _safe(report)


@pytest.mark.parametrize("mutation", [
    {"ready": True},
    {"host": "8.8.8.8"},
    {"user": "user;Remove-Item"},
    {"credential": "secret"},
    {"known_hosts": "relative-path"},
])
def test_tampered_or_malformed_profile_is_rejected_before_status(
    tmp_path, monkeypatch, mutation,
):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    payload = json.loads(opts["profile_file"].read_text(encoding="utf-8"))
    payload.update(mutation)
    opts["profile_file"].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="LAN_OPERATOR_PROFILE_CORRUPT"):
        operator.read_operator_profile(opts["profile_file"])


def test_missing_profile_offline_status_no_network_or_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operator, "prepare_lan_monitor_task",
        lambda **kw: pytest.fail("no monitor install without profile"),
    )
    report = operator.operator_status(profile_file=tmp_path / "absent.json")
    assert report["status"] == "LAN_OPERATOR_PROFILE_MISSING"
    assert report["lease_requested"] is False
    assert report["youtube_contacted"] is False
    assert not (tmp_path / "absent.json").exists()


def test_offline_status_uses_saved_profile_and_no_ssh(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    monkeypatch.setattr(operator, "read_lan_evidence_history",
                        lambda path: {"status": "LAN_EVIDENCE_HISTORY_EMPTY"})
    monkeypatch.setattr(operator, "read_lan_monitor_alert",
                        lambda **kw: {"alert": "NONE"})
    monkeypatch.setattr(operator, "_task_state",
                        lambda name: "LAN_TASK_ABSENT_OBSERVED")
    report = operator.operator_status(profile_file=opts["profile_file"])
    assert report["status"] == "LAN_OPERATOR_LOCAL_STATUS"
    assert report["monitor"] == "LAN_TASK_ABSENT_OBSERVED"
    assert report["history"] == "LAN_EVIDENCE_HISTORY_EMPTY"
    assert report["alert"] == "NONE"
    assert report["ready"] is False
    assert "safeuser" not in json.dumps(report)


def test_status_register_monitor_explicit_opt_in(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    operator.prepare_operator_profile(**opts, apply=True)
    calls = []
    monkeypatch.setattr(operator, "prepare_lan_monitor_task",
                        lambda **kw: calls.append(kw) or {
                            "status": "LAN_MONITOR_TASK_REGISTERED_NOT_STARTED",
                            "task_registered": True,
                        })
    monkeypatch.setattr(operator, "read_lan_evidence_history",
                        lambda path: {"status": "LAN_EVIDENCE_HISTORY_EMPTY"})
    monkeypatch.setattr(operator, "read_lan_monitor_alert",
                        lambda **kw: {"alert": "NONE"})
    status = operator.operator_status(
        profile_file=opts["profile_file"], register_monitor=True,
    )
    assert status["status"] == "LAN_OPERATOR_MONITOR_REGISTERED_NOT_STARTED"
    assert calls[0]["apply"] is True
    assert calls[0]["host"] == "192.168.1.21"
    assert status["ready"] is False


@pytest.mark.parametrize("bad", [
    "127.0.0.1", "8.8.8.8", "192.168.001.021", "evil;cmd",
])
def test_invalid_host_fails_before_pinned_check(tmp_path, monkeypatch, bad):
    opts = _inputs(tmp_path)
    monkeypatch.setattr(
        operator, "_pinned_ssh_command",
        lambda **kw: pytest.fail("invalid host must fail before SSH"),
    )
    with pytest.raises(ValueError, match="LAN_OPERATOR_UNSAFE_PC_B"):
        operator.prepare_operator_profile(**{**opts, "host": bad}, apply=True)


def test_profile_ssh_pin_missing_blocks_creation(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    monkeypatch.setattr(operator, "_host_pin_state",
                        lambda *args: "HOST_PIN_NOT_FOUND")
    with pytest.raises(ValueError, match="LAN_OPERATOR_PINNED_HOST_KEY_REQUIRED"):
        operator.prepare_operator_profile(**opts, apply=True)
    assert not opts["profile_file"].exists()


def test_explicit_update_or_monitor_without_apply_rejected(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    monkeypatch.setattr(
        operator, "_pinned_ssh_command",
        lambda **kwargs: pytest.fail("invalid flags should fail first"),
    )
    with pytest.raises(ValueError, match="LAN_OPERATOR_UPDATE_REQUIRES_APPLY"):
        operator.prepare_operator_profile(**opts, update=True)
    with pytest.raises(ValueError, match="LAN_OPERATOR_REGISTER_REQUIRES_APPLY"):
        operator.prepare_operator_profile(**opts, register_monitor=True)


def test_symlinked_profile_never_overwritten(tmp_path, monkeypatch):
    opts = _inputs(tmp_path)
    _mock_local(monkeypatch)
    real = tmp_path / "external.json"
    real.write_text("DO_NOT_OVERWRITE")
    try:
        opts["profile_file"].symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError, match="LAN_OPERATOR_UNSAFE_PROFILE_PATH"):
        operator.prepare_operator_profile(**opts, apply=True)
    assert real.read_text() == "DO_NOT_OVERWRITE"


def test_cli_invalid_host_reports_fixed_code_only(tmp_path):
    opts = _inputs(tmp_path)
    r = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-operator-setup", "--host", "8.8.8.8",
        "--user", "safeuser", "--known-hosts", str(opts["known_hosts"]),
        "--remote-repo", opts["remote_repo"],
        "--profile-file", str(opts["profile_file"]),
    ])
    assert r.exit_code == 6
    data = json.loads(r.stdout)
    assert data["reason"] == "LAN_OPERATOR_UNSAFE_PC_B"
    assert data["ready"] is False


def test_cli_missing_profile_does_not_claim_success(tmp_path):
    r = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-operator-status",
        "--profile-file", str(tmp_path / "missing.json"),
    ])
    assert r.exit_code == 6
    assert json.loads(r.stdout)["status"] == "LAN_OPERATOR_PROFILE_MISSING"
