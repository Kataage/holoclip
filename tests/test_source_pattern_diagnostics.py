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
    assert report["current_source_cache_byte_attestation"]["status"] == (
        "UNKNOWN_NO_SOURCE_CACHE_EVIDENCE"
    )
    assert report["last_run_saved_cache_attestation"] is None
    assert report["ready"] is False


def test_local_doctor_redacts_retryable_youtube_challenges_and_explains_pause(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState

    state = CorpusGrowthState()
    for index in range(3):
        state.items[f"retry-{index}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{index:011d}",
            query="hololive",
            status="retryable",
            reason=(
                "YtDlpError: Sign in to confirm you're not a bot "
                "https://www.youtube.com/watch?v=SECRET_PRIVATE_CONTENT"
            ),
        )
    (tmp_path / "state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    report = audit(now, status="UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS")
    report["result"]["discovery"] = {
        "metadata_access_paused": True,
        "metadata_access_pause_reason": "REPEATED_YOUTUBE_CHALLENGE_OR_RATE_LIMIT",
        "metadata_failure_category_counts": {
            "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE": 3,
        },
    }
    (tmp_path / "collector.json").write_text(
        json.dumps(report), encoding="utf-8",
    )
    result = diagnose(tmp_path)
    assert result["status"] == "BLOCKED_LOCAL_YOUTUBE_METADATA_ACCESS"
    assert result["next_action_code"] == (
        "DEFER_LOCAL_YOUTUBE_REQUESTS_UNTIL_NEXT_SCHEDULE"
    )
    assert result["last_run_metadata_access_paused"] is True
    assert result["local_retryable_metadata_failure_summary"][
        "failure_category_counts"
    ] == {"YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE": 3}
    assert result["local_retryable_metadata_failure_summary"][
        "raw_error_text_included"
    ] is False
    assert result["ready"] is False
    assert "youtu.be" not in json.dumps(result)
    assert "SECRET_PRIVATE_CONTENT" not in json.dumps(result)


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


def test_doctor_exposes_unreviewed_video_stage_gap_even_when_controls_pass(
    tmp_path,
):
    now = datetime.now(timezone.utc)
    report = audit(now, status="UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS")
    report["result"]["cycle_diagnostics"] = [{
        "cycle": 1,
        "attempted_clips": 1,
        "source_pattern_attempt_outcomes": {
            "pattern:MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE": 1,
        },
        "stage_diagnostics": {
            "attempted_clip_records": 1,
            "candidate_observation_records": 1,
            "finished_video_sha_recorded_clips": 1,
            "source_window_section_records": 3,
            "constructed_pattern_execution_failures": 0,
            "cross_revisit_attempted_clips": 1,
            "clip_status_counts": {
                "UNKNOWN_NO_VISUAL_MATCH_IN_BOUNDED_SAMPLES": 1,
            },
            "candidate_visual_status_counts": {
                "UNKNOWN_NO_VISUAL_SEQUENCE": 1,
            },
            "constructed_pattern_status_counts": {
                "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE": 1,
            },
            "source_probe_mode_counts": {"bounded_cross_revisit": 1},
            "measured_original_match_gold": False,
            "record_counts_are_verified_public_media": False,
        },
    }]
    (tmp_path / "collector.json").write_text(json.dumps(report), encoding="utf-8")
    result = diagnose(tmp_path)
    assert result["last_run_stage_evidence_gap"] == (
        "UNKNOWN_NO_VISUAL_SOURCE_CORROBORATION"
    )
    funnel = result["last_run_stage_funnel"]
    assert funnel["source_window_section_records"] == 3
    assert funnel["finished_video_sha_recorded_clips"] == 1
    assert funnel["candidate_visual_status_counts"] == {
        "UNKNOWN_NO_VISUAL_SEQUENCE": 1,
    }
    assert funnel["constructed_pattern_status_counts"] == {
        "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE": 1,
    }
    assert funnel["record_counts_are_verified_public_media"] is False
    assert result["ready"] is False


@pytest.mark.parametrize("corruption", [
    "promoted_gold", "excess_candidate_status", "negative_count",
    "excess_pattern_records", "mixed_schema",
])
def test_doctor_rejects_forged_or_incomplete_stage_funnels(tmp_path, corruption):
    now = datetime.now(timezone.utc)
    report = audit(now)
    stage = {
        "attempted_clip_records": 1,
        "candidate_observation_records": 1,
        "finished_video_sha_recorded_clips": 0,
        "source_window_section_records": 0,
        "constructed_pattern_execution_failures": 0,
        "cross_revisit_attempted_clips": 0,
        "clip_status_counts": {"UNKNOWN_NO_VISUAL_MATCH": 1},
        "candidate_visual_status_counts": {"UNKNOWN_NO_VISUAL_SEQUENCE": 1},
        "constructed_pattern_status_counts": {},
        "source_probe_mode_counts": {},
        "measured_original_match_gold": False,
        "record_counts_are_verified_public_media": False,
    }
    if corruption == "promoted_gold":
        stage["measured_original_match_gold"] = True
    elif corruption == "excess_candidate_status":
        stage["candidate_visual_status_counts"]["UNKNOWN_NO_VISUAL_SEQUENCE"] = 3
    elif corruption == "negative_count":
        stage["source_window_section_records"] = -1
    elif corruption == "excess_pattern_records":
        stage["constructed_pattern_status_counts"]["MEASURED_OTHER"] = 2
    row = {"attempted_clips": 1, "stage_diagnostics": stage}
    rows = [row]
    if corruption == "mixed_schema":
        rows.append({"attempted_clips": 1})
    report["result"]["cycle_diagnostics"] = rows
    (tmp_path / "collector.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        diagnose(tmp_path)


def test_legacy_audit_cannot_claim_retroactive_stage_data(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "collector.json").write_text(
        json.dumps(audit(now)), encoding="utf-8",
    )
    result = diagnose(tmp_path)
    assert result["last_run_stage_funnel"] is None
    assert result["last_run_stage_evidence_gap"] == (
        "UNKNOWN_NO_STAGE_DIAGNOSTICS"
    )


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
