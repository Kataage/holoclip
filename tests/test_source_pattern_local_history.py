"""Read-only launcher history detects corruption, stale media and real deltas."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_pattern_local_history import (
    MAX_EVENTS, VERSION, read_launcher_history,
)


def event(index: int, *, status="COMPLETED_COLLECTOR_PROCESS", count=None):
    start = datetime(2026, 10, 10, tzinfo=timezone.utc) + timedelta(minutes=index)
    collected = count is not None
    return {
        "started_at_utc": start.isoformat(),
        "finished_at_utc": (start + timedelta(seconds=10)).isoformat(),
        "status": status,
        "exit_code": 0 if status == "COMPLETED_COLLECTOR_PROCESS" else 1,
        "ran_collector": status != "FAILED_PRELAUNCH",
        "error_class": "CommandNotFoundException" if status == "FAILED_PRELAUNCH" else None,
        "new_unique_video_samples": count,
        "new_distinct_candidate_streams": (count if collected else None),
        "collector_report_correlated": collected,
        "ready": False, "production_ready": False,
    }


def persist(path, records):
    path.write_text(json.dumps({
        "version": VERSION, "events": records,
        "ready": False, "production_ready": False,
    }), encoding="utf-8")


def test_no_native_history_is_unknown_not_one_fake_success(tmp_path):
    report = read_launcher_history(tmp_path / "missing.json")
    assert report["status"] == "UNKNOWN_NO_SAVED_NATIVE_LAUNCHER_HISTORY"
    assert report["retained_runs"] == 0
    assert report["last_run"] is None
    assert report["last_run_new_samples_delta"] is None
    assert report["ready"] is False


def test_history_retains_failure_success_and_true_per_run_deltas(tmp_path):
    path = tmp_path / "history.json"
    persist(path, [event(0, count=1), event(1, count=3), event(
        2, status="FAILED_PRELAUNCH",
    )])
    report = read_launcher_history(path)
    assert report["status"] == "RECORDED_NATIVE_LAUNCHER_HISTORY"
    assert report["retained_runs"] == 3
    assert report["status_counts"] == {
        "COMPLETED_COLLECTOR_PROCESS": 2, "FAILED_PRELAUNCH": 1,
    }
    assert report["consecutive_failure_runs"] == 1
    assert report["last_run"]["new_unique_video_samples"] is None
    assert report["last_run_new_samples_delta"] is None
    assert report["last_run"]["status"] == "FAILED_PRELAUNCH"
    persist(path, [event(0, count=1), event(1, count=3)])
    report = read_launcher_history(path)
    assert report["last_run_new_samples_delta"] == 2
    assert report["last_run_new_streams_delta"] == 2
    assert report["consecutive_failure_runs"] == 0
    assert report["ready"] is False


def test_stale_or_unvalidated_collector_never_produces_counts(tmp_path):
    path = tmp_path / "history.json"
    persist(path, [event(0), event(1)])
    report = read_launcher_history(path)
    assert report["last_run_new_samples_delta"] is None
    assert report["last_run"]["collector_report_correlated"] is False
    for index in range(5):
        invalid = event(1)
        invalid["new_unique_video_samples"] = index + 1
        persist(path, [invalid])
        with pytest.raises(ValueError, match="Uncorrelated"):
            read_launcher_history(path)


@pytest.mark.parametrize("tamper", [
    "promoted_ready", "invalid_version", "unknown_field", "forged_success",
    "wrong_counts", "out_of_order", "duplicate_run", "naive_time",
    "exit_inconsistent", "fake_correlated_failure",
])
def test_history_rejects_corruption_and_false_gold(tmp_path, tamper):
    path = tmp_path / "history.json"
    records = [event(0, count=2), event(1, count=1)]
    doc = {"version": VERSION, "events": records, "ready": False, "production_ready": False}
    if tamper == "promoted_ready":
        records[1]["ready"] = True
    elif tamper == "invalid_version":
        doc["version"] = "other"
    elif tamper == "unknown_field":
        records[1]["original_url"] = "https://youtube.com/private"
    elif tamper == "forged_success":
        records[1]["status"] = "MEASURED_VIDEO_GOLD"
    elif tamper == "wrong_counts":
        records[1]["new_unique_video_samples"] = True
    elif tamper == "out_of_order":
        records.reverse()
    elif tamper == "duplicate_run":
        records.append(event(1, count=1))
    elif tamper == "naive_time":
        records[1]["started_at_utc"] = "2026-10-10T00:01:00"
    elif tamper == "exit_inconsistent":
        records[1]["exit_code"] = 9
    elif tamper == "fake_correlated_failure":
        records[1]["status"] = "FAILED_PRELAUNCH"
        records[1]["ran_collector"] = False
        records[1]["exit_code"] = 1
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError):
        read_launcher_history(path)


def test_history_has_strict_retention_and_shows_last_seven(tmp_path):
    path = tmp_path / "history.json"
    persist(path, [event(i, count=i % 3) for i in range(MAX_EVENTS)])
    report = read_launcher_history(path)
    assert report["retained_runs"] == MAX_EVENTS
    assert report["history_reached_retention_cap"] is True
    assert len(report["last_seven_runs"]) == 7
    assert report["last_seven_runs"][0]["new_unique_video_samples"] == 83 % 3
    persist(path, [event(i) for i in range(MAX_EVENTS + 1)])
    with pytest.raises(ValueError, match="envelope"):
        read_launcher_history(path)


def test_history_symlink_and_oversized_file_are_rejected(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("x" * 262145, encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe"):
        read_launcher_history(path)
    path.unlink()
    target = tmp_path / "actual.json"
    persist(target, [])
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    with pytest.raises(ValueError, match="symlink"):
        read_launcher_history(path)


def test_one_command_health_discovers_local_launcher_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "data/examples"
    dest.mkdir(parents=True)
    persist(dest / "source-pattern-launcher-history.json", [
        event(0, status="FAILED_PRELAUNCH"),
        event(1, status="FAILED_PRELAUNCH"),
    ])
    response = CliRunner().invoke(
        cli.app, ["examples-collector-health", "--no-check-tools"],
    )
    assert response.exit_code == 0, response.output
    summary = json.loads(response.output)
    timeline = summary["native_launcher_history"]
    assert timeline["retained_runs"] == 2
    assert timeline["consecutive_failure_runs"] == 2
    assert timeline["last_run_new_samples_delta"] is None
    assert "data/examples" not in response.output
    assert "youtube.com" not in response.output
    assert summary["ready"] is False
