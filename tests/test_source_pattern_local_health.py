"""Native local-only health summary and optional settings never call yt-dlp."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_pattern_local_health import (
    resolve_local_diagnostic_paths, summarize_local_collection_health,
)


FLAGS = dict(
    source_identity_verified=False, reviewed_gold_available=False,
    qualification_allowed=False, ready=False, production_ready=False,
)


def doctor_fixture() -> dict:
    return {
        **FLAGS,
        "status": "RECORDED_UNREVIEWED_NEW_SOURCE_VIDEO_SAMPLES",
        "tools": {"status": "AVAILABLE", "missing": []},
        "last_run_metadata_access_paused": False,
        "local_retryable_metadata_failure_summary": {"retryable_clip_records": 0},
        "new_unique_video_samples_last_run": 2,
        "last_run_stage_funnel": {
            "attempted_clip_records": 2,
            "finished_video_sha_recorded_clips": 2,
            "source_window_section_records": 3,
            "constructed_pattern_status_counts": {"MEASURED_UNREVIEWED": 3},
            "candidate_visual_status_counts": {
                "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD": 1,
            },
        },
        "current_source_cache_byte_attestation": {
            "complete_pending_queue_audited": True,
            "cache_section_status_counts": {
                "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD": 3,
            },
        },
    }


@pytest.mark.parametrize(("mutation", "expected"), [
    ({}, "REVIEWED_GOLD_STILL_MISSING"),
    ({"status": "BLOCKED_LATEST_WINDOWS_LAUNCH"}, "WINDOWS_LAUNCH"),
    ({"status": "BLOCKED_LOCAL_DEPENDENCIES"}, "LOCAL_TOOLS"),
    ({"status": "UNKNOWN_COLLECTION_NOT_YET_RECORDED"}, "NO_LOCAL_RUN"),
    ({"status": "UNKNOWN_COLLECTION_FAILED"}, "COLLECTOR_EXECUTION"),
    ({"last_run_metadata_access_paused": True}, "YOUTUBE_ACCESS"),
    ({"last_run_stage_funnel": None}, "NO_STAGE_DIAGNOSTICS"),
])
def test_health_triages_saved_run_without_claiming_gold(mutation, expected):
    doc = doctor_fixture()
    doc.update(mutation)
    health = summarize_local_collection_health(doc)
    assert health["primary_blocker"] == expected
    assert health["ready"] is False
    assert health["production_ready"] is False
    assert health["human_editor_gold_verified"] is False
    assert health["independent_original_to_finished_match_verified"] is False
    assert health["record_counts_are_verified_public_video"] is False
    assert health["health_status"] == "UNREVIEWED_DIAGNOSTIC_ONLY"
    assert "https://" not in json.dumps(health)


@pytest.mark.parametrize(("field", "replacement", "expected"), [
    ("attempted_clip_records", 0, "NO_VISUAL_ATTEMPTS"),
    ("finished_video_sha_recorded_clips", 0, "NO_FINISHED_VIDEO_RECORD"),
    ("source_window_section_records", 0, "NO_SOURCE_VIDEO_RECORD"),
    ("candidate_visual_status_counts", {}, "NO_VISUAL_SOURCE_CORROBORATION"),
    ("constructed_pattern_status_counts", {}, "NO_CONSTRUCTED_PATTERN_CONTROL"),
])
def test_health_reports_exact_missing_stage(field, replacement, expected):
    doc = doctor_fixture()
    doc["last_run_stage_funnel"][field] = replacement
    health = summarize_local_collection_health(doc)
    assert health["primary_blocker"] == expected


def test_health_distinguishes_corrupt_partial_and_missing_current_disk_bytes():
    doc = doctor_fixture()
    cache = doc["current_source_cache_byte_attestation"]
    cache["cache_section_status_counts"]["BLOCKED_CACHE_MEDIA_BYTES_MISMATCH"] = 1
    assert summarize_local_collection_health(doc)["primary_blocker"] == (
        "LOCAL_CACHE_INTEGRITY"
    )
    del cache["cache_section_status_counts"]["BLOCKED_CACHE_MEDIA_BYTES_MISMATCH"]
    cache["complete_pending_queue_audited"] = False
    assert summarize_local_collection_health(doc)["primary_blocker"] == (
        "PARTIAL_CACHE_SCOPE"
    )
    cache["complete_pending_queue_audited"] = True
    cache["cache_section_status_counts"].clear()
    assert summarize_local_collection_health(doc)["primary_blocker"] == (
        "SOURCE_CACHE_NOT_REVALIDATED"
    )
    doc["new_unique_video_samples_last_run"] = 0
    assert summarize_local_collection_health(doc)["primary_blocker"] == (
        "NO_REVALIDATED_SOURCE_BYTES"
    )


def test_health_rejects_positive_readiness_and_forged_counts():
    doc = doctor_fixture()
    doc["ready"] = True
    with pytest.raises(ValueError, match="Unsafe"):
        summarize_local_collection_health(doc)
    doc = doctor_fixture()
    doc["last_run_stage_funnel"]["source_window_section_records"] = -1
    with pytest.raises(ValueError, match="Invalid"):
        summarize_local_collection_health(doc)
    doc = doctor_fixture()
    doc["current_source_cache_byte_attestation"][
        "cache_section_status_counts"
    ]["VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD"] = True
    with pytest.raises(ValueError, match="Invalid"):
        summarize_local_collection_health(doc)


def test_optional_local_settings_are_resolved_without_mutation(tmp_path):
    paths, loaded = resolve_local_diagnostic_paths(tmp_path)
    assert loaded is False
    assert paths["state"] == tmp_path / "data/examples/corpus_growth.json"
    file = tmp_path / "data/examples/source-pattern-local-settings.json"
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({
        "version": "source-pattern-local-settings-v1",
        "state": "local state/custom corpus.json",
        "output": "local video/sections",
        "report_path": "local state/run audit.json",
        "launcher_audit_path": "local state/launcher.json",
        "cycles": 2, "max_clips": 1, "discover": False,
    }), encoding="utf-8")
    paths, loaded = resolve_local_diagnostic_paths(tmp_path)
    assert loaded is True
    assert paths["state"] == tmp_path / "local state/custom corpus.json"
    assert paths["output"] == tmp_path / "local video/sections"
    assert paths["launcher_audit_path"] == tmp_path / "local state/launcher.json"
    assert not paths["state"].exists()
    assert file.is_file()


@pytest.mark.parametrize("extra", [
    {"cycles": True},
    {"max_candidates": 7},
    {"discover": "false"},
    {"state": ""},
    {"output": "new\nfile.json"},
    {"report_path": 'a"b.json'},
    {"unknown_key": 1},
    {"version": "other"},
])
def test_local_config_rejects_malformed_or_unsafe_fields(tmp_path, extra):
    values = {"version": "source-pattern-local-settings-v1", **extra}
    file = tmp_path / "settings.json"
    file.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_local_diagnostic_paths(tmp_path, config_path=file)


def test_explicit_missing_or_symlink_settings_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        resolve_local_diagnostic_paths(
            tmp_path, config_path=Path("missing-local-settings.json"),
        )
    actual = tmp_path / "actual.json"
    actual.write_text('{"version":"source-pattern-local-settings-v1"}')
    link = tmp_path / "shortcut.json"
    try:
        link.symlink_to(actual)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="symlink"):
        resolve_local_diagnostic_paths(tmp_path, config_path=link)


def test_local_health_cli_uses_selected_windows_settings_without_network(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "data/examples/source-pattern-local-settings.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "version": "source-pattern-local-settings-v1",
        "state": "custom folder/corpus.json",
        "report_path": "custom folder/collector.json",
        "launcher_audit_path": "custom folder/launcher.json",
        "output": "custom folder/media",
    }))
    data = tmp_path / "custom folder"
    data.mkdir()
    # Collection reports contain no reviewed gold; the one-command status
    # should not require model, credentials, video URLs, yt-dlp or Windows.
    from datetime import datetime, timedelta, timezone

    from holoclip.source_pattern_worker import VERSION as WORKER_VERSION

    now = datetime.now(timezone.utc)
    result = {
        "version": WORKER_VERSION,
        "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
        "started_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "finished_at_utc": now.isoformat(),
        "result": {
            "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
            "new_unique_video_samples_this_run": 0,
            "new_distinct_candidate_streams_this_run": 0,
            **FLAGS,
        },
        **FLAGS,
    }
    (data / "collector.json").write_text(json.dumps(result), encoding="utf-8")
    response = CliRunner().invoke(
        cli.app, ["examples-collector-health", "--no-check-tools"],
    )
    assert response.exit_code == 0, response.output
    summary = json.loads(response.stdout)
    assert summary["local_settings_loaded"] is True
    assert summary["primary_blocker"] == "NO_STAGE_DIAGNOSTICS"
    assert summary["ready"] is False
    assert "custom folder" not in response.stdout
    assert "youtube.com" not in response.stdout


def test_local_health_cli_defaults_do_not_request_public_media(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli.app, ["examples-collector-health", "--no-check-tools"],
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["local_settings_loaded"] is False
    assert value["primary_blocker"] == "NO_LOCAL_RUN"
    assert value["ready"] is False
