"""Read-only local source collector diagnosis never invents fresh measurement."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_pattern_diagnostics import (
    LAUNCHER_VERSION, diagnose_source_pattern_collector,
)
from holoclip.source_pattern_worker import VERSION as WORKER_VERSION

FLAGS = dict(
    source_identity_verified=False,
    reviewed_gold_available=False,
    qualification_allowed=False,
    ready=False,
    production_ready=False,
)


def audit(now, *, status="UNKNOWN_NO_NEW_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
          samples=0):
    result = {
        "status": status, "new_unique_video_samples_this_run": samples,
        "new_distinct_candidate_streams_this_run": int(samples > 0),
        **FLAGS,
    }
    return {
        "version": WORKER_VERSION, "status": status,
        "started_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "finished_at_utc": now.isoformat(),
        "result": result if status != "UNKNOWN_COLLECTION_EXECUTION_FAILURE" else None,
        **FLAGS,
    }


def launch(now, *, status, code, ran):
    return {
        "version": LAUNCHER_VERSION, "status": status, "exit_code": code,
        "error_class": "CommandNotFoundException" if status == "FAILED_PRELAUNCH" else None,
        "ran_collector": ran,
        "started_at_utc": (now - timedelta(seconds=10)).isoformat(),
        "finished_at_utc": now.isoformat(), **FLAGS,
    }


def diagnose(tmp_path, **options):
    args = dict(
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "collector.json",
        launcher_path=tmp_path / "launcher.json",
        check_tools=False,
    )
    args.update(options)
    return diagnose_source_pattern_collector(**args)


def test_missing_state_and_audits_are_unknown_not_ready(tmp_path):
    report = diagnose(tmp_path)
    assert report["status"] == "UNKNOWN_COLLECTION_NOT_YET_RECORDED"
    assert report["next_action_code"] == "INSTALL_OR_INVOKE_LOCAL_COLLECTOR"
    assert report["cumulative_unreviewed_source_video_samples"] == 0
    assert report["new_unique_video_samples_last_run"] is None
    assert report["tools"]["status"] == "NOT_CHECKED"
    assert report["ready"] is False


def test_later_launcher_failure_overrides_stale_collector_success(tmp_path):
    now = datetime.now(timezone.utc)
    prior = audit(now, status="MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS",
                  samples=2)
    prior["finished_at_utc"] = (now - timedelta(minutes=1)).isoformat()
    (tmp_path / "collector.json").write_text(json.dumps(prior), encoding="utf-8")
    (tmp_path / "launcher.json").write_text(json.dumps(
        launch(now, status="FAILED_PRELAUNCH", code=1, ran=False)
    ), encoding="utf-8")
    report = diagnose(tmp_path)
    assert report["status"] == "BLOCKED_LATEST_WINDOWS_LAUNCH"
    assert report["last_launcher_error_class"] == "CommandNotFoundException"
    assert report["new_unique_video_samples_last_run"] == 2
    assert report["ready"] is False


def test_stale_launcher_failure_does_not_override_new_collector_run(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "collector.json").write_text(
        json.dumps(audit(now)), encoding="utf-8",
    )
    (tmp_path / "launcher.json").write_text(
        json.dumps(launch(now - timedelta(minutes=5),
                          status="FAILED_PRELAUNCH", code=1, ran=False)),
        encoding="utf-8",
    )
    assert diagnose(tmp_path)["status"] == "UNKNOWN_NO_NEW_SOURCE_VIDEO_SAMPLES"


def test_corrupt_or_promoted_saved_audit_is_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    doc = audit(now)
    doc["ready"] = True
    (tmp_path / "collector.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="readiness"):
        diagnose(tmp_path)
    doc["ready"] = False
    doc["status"] = "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS"
    doc["result"]["status"] = doc["status"]
    (tmp_path / "collector.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="Historical"):
        diagnose(tmp_path)


def test_launcher_exit_code_and_prelaunch_flags_must_agree(tmp_path):
    now = datetime.now(timezone.utc)
    doc = launch(now, status="COMPLETED_COLLECTOR_PROCESS", code=1, ran=True)
    (tmp_path / "launcher.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="Contradictory"):
        diagnose(tmp_path)
    doc = launch(now, status="FAILED_PRELAUNCH", code=1, ran=True)
    (tmp_path / "launcher.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="Prelaunch"):
        diagnose(tmp_path)


def test_tool_missing_is_explicit_and_does_not_call_network(tmp_path, monkeypatch):
    from holoclip import source_pattern_diagnostics as doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "detect_js_runtime",
                        lambda: (_ for _ in ()).throw(RuntimeError("no-js")))
    report = diagnose(tmp_path, check_tools=True)
    assert report["status"] == "BLOCKED_LOCAL_DEPENDENCIES"
    assert report["tools"]["missing"] == [
        "uv", "ffmpeg", "ffprobe", "deno>=2.3-or-node>=22",
    ]
    assert report["ready"] is False


def test_cli_doctor_can_read_status_with_no_install_or_download(tmp_path):
    result = CliRunner().invoke(cli.app, [
        "examples-collector-doctor", "--no-check-tools",
        "--state", str(tmp_path / "state.json"),
        "--report-path", str(tmp_path / "collector.json"),
        "--launcher-path", str(tmp_path / "launcher.json"),
    ])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["ready"] is False
    assert doc["status"] == "UNKNOWN_COLLECTION_NOT_YET_RECORDED"
