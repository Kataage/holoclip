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


def _new_stage_event(index, *, count=0, streams=0,
                     pairs=1, sections=2, corroborations=0):
    result = event(index, count=count)
    result["new_distinct_candidate_streams"] = streams
    result.update(
        stage_metrics_correlated=True,
        last_run_visual_pair_attempts=pairs,
        last_run_source_window_records=sections,
        last_run_sampled_visual_correspondences=corroborations,
    )
    return result


@pytest.mark.parametrize(("statuses", "expected"), [
    (["FAILED_PRELAUNCH"] * 3, "REPEATED_NATIVE_LAUNCH_FAILURES"),
    (["FAILED_COLLECTOR_PROCESS"] * 3, "REPEATED_NATIVE_LAUNCH_FAILURES"),
])
def test_three_consecutive_failed_native_launches_trigger_only_local_warning(
    tmp_path, statuses, expected,
):
    path = tmp_path / "history.json"
    persist(path, [
        event(i, status=status) for i, status in enumerate(statuses)
    ])
    report = read_launcher_history(path)
    alert = report["operational_stall"]
    assert alert["primary_signal"] == expected
    assert alert["consecutive_failed_launcher_runs"] == 3
    assert alert["consecutive_correlated_zero_sample_runs"] == 0
    assert alert["source_identity_verified"] is False
    assert alert["ready"] is False


def test_five_correlated_zero_sample_runs_are_a_stall_not_a_false_negative(
    tmp_path,
):
    path = tmp_path / "history.json"
    persist(path, [_new_stage_event(i, count=0, pairs=0, sections=0)
                   for i in range(5)])
    report = read_launcher_history(path)
    signal = report["operational_stall"]
    assert signal["primary_signal"] == "REPEATED_ZERO_NEW_PATTERN_SAMPLES"
    assert signal["consecutive_correlated_zero_sample_runs"] == 5
    assert signal["consecutive_correlated_noncorroborated_visual_runs"] == 0
    assert signal["visual_negative_or_editor_gold_proven"] is False
    assert report["last_run"]["new_unique_video_samples"] == 0


def test_uncorrelated_and_failed_runs_break_zero_progress_streak(tmp_path):
    path = tmp_path / "history.json"
    records = [_new_stage_event(i, count=0, pairs=0, sections=0)
               for i in range(4)]
    records.insert(2, event(2))
    # Repair chronological positions after inserting the legacy unknown.
    for i, row in enumerate(records):
        row["started_at_utc"] = event(i)["started_at_utc"]
        row["finished_at_utc"] = event(i)["finished_at_utc"]
    persist(path, records)
    result = read_launcher_history(path)["operational_stall"]
    assert result["consecutive_correlated_zero_sample_runs"] == 2
    assert result["primary_signal"] is None


def test_visual_noncorroboration_requires_three_real_recorded_pairing_runs(
    tmp_path,
):
    path = tmp_path / "history.json"
    persist(path, [
        _new_stage_event(i, count=1, streams=1, pairs=2, sections=4)
        for i in range(3)
    ])
    report = read_launcher_history(path)
    alert = report["operational_stall"]
    assert alert["primary_signal"] == "REPEATED_BOUNDED_VISUAL_NONCORROBORATION"
    assert alert["consecutive_correlated_noncorroborated_visual_runs"] == 3
    assert alert["visual_negative_or_editor_gold_proven"] is False
    assert report["last_run"]["last_run_visual_pair_attempts"] == 2
    # A verified positive *bounded* sampled control breaks this streak but
    # still does not provide independent correct-original/editor gold.
    persist(path, [
        _new_stage_event(0, count=1, streams=1, pairs=2, sections=4),
        _new_stage_event(1, count=1, streams=1, pairs=2, sections=4),
        _new_stage_event(2, count=1, streams=1, pairs=2, sections=4,
                         corroborations=1),
    ])
    result = read_launcher_history(path)["operational_stall"]
    assert result["consecutive_correlated_noncorroborated_visual_runs"] == 0
    assert result["primary_signal"] is None


def test_visual_stall_does_not_infer_metadata_only_or_legacy_runs_as_pairs(
    tmp_path,
):
    path = tmp_path / "history.json"
    records = [
        _new_stage_event(0, count=1, streams=1, pairs=2, sections=2),
        event(1, count=1),  # Old v1 event with NO stage fields
        _new_stage_event(2, count=1, streams=1, pairs=2, sections=2),
        _new_stage_event(3, count=1, streams=1, pairs=2, sections=2),
    ]
    persist(path, records)
    result = read_launcher_history(path)
    assert result["legacy_runs_without_stage_metrics"] == 1
    assert result["operational_stall"][
        "consecutive_correlated_noncorroborated_visual_runs"
    ] == 2
    assert result["operational_stall"]["primary_signal"] is None
    assert result["last_seven_runs"][1]["stage_metrics_correlated"] is False


def test_distinct_source_stall_only_when_patterns_measured_but_streams_not_new(
    tmp_path,
):
    path = tmp_path / "history.json"
    persist(path, [
        _new_stage_event(i, count=1, streams=0, pairs=2, sections=3,
                         corroborations=1)
        for i in range(5)
    ])
    result = read_launcher_history(path)["operational_stall"]
    assert result["primary_signal"] == "REPEATED_NO_NEW_DISTINCT_SOURCE_CANDIDATES"
    assert result["consecutive_correlated_no_new_distinct_source_runs"] == 5
    assert result["consecutive_correlated_zero_sample_runs"] == 0


@pytest.mark.parametrize("malformed", [
    "partial", "positive_without_correlated", "above_pair_budget",
    "visual_correspondences_exceed_attempts", "correlated_without_samples",
    "negative", "promoted_editor_readiness",
])
def test_forged_visual_stage_history_fails_closed(tmp_path, malformed):
    path = tmp_path / "history.json"
    row = _new_stage_event(0, count=1, streams=1, pairs=2, sections=3)
    if malformed == "partial":
        del row["last_run_source_window_records"]
    elif malformed == "positive_without_correlated":
        row["stage_metrics_correlated"] = False
    elif malformed == "above_pair_budget":
        row["last_run_source_window_records"] = 7
    elif malformed == "visual_correspondences_exceed_attempts":
        row["last_run_sampled_visual_correspondences"] = 3
    elif malformed == "correlated_without_samples":
        row["collector_report_correlated"] = False
        row["new_unique_video_samples"] = None
        row["new_distinct_candidate_streams"] = None
    elif malformed == "negative":
        row["last_run_visual_pair_attempts"] = -1
    elif malformed == "promoted_editor_readiness":
        row["source_identity_verified"] = True
    persist(path, [row])
    with pytest.raises(ValueError):
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
