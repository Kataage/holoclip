"""Read-only local diagnostics for the unattended public-video collector.

All evidence remains unreviewed. This command never fetches URLs, changes
corpus state, invokes cloud models, or asserts actual source identity.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.providers.source.yt_dlp import detect_js_runtime
from holoclip.source_pattern_worker import VERSION as WORKER_VERSION
from holoclip.source_pattern_local_health import summarize_local_collection_health
from holoclip.source_visual_pattern_report import report_saved_patterns
from holoclip.source_metadata_failure_summary import _saved_metadata_probe_failures
from holoclip.source_video_cache_audit import (
    audit_cached_source_video_sections, checked_cached_video_attestation,
)

VERSION = "source-pattern-collector-doctor-v1"
LAUNCHER_VERSION = "source-pattern-windows-launcher-v1"
NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
}
_ALLOWED_LAUNCHER_STATUSES = {
    "FAILED_PRELAUNCH", "FAILED_COLLECTOR_PROCESS",
    "COMPLETED_COLLECTOR_PROCESS",
}


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("Missing source-pattern audit timestamp")
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Source-pattern audit timestamp must include timezone")
    return stamp


_STAGE_COUNT_FIELDS = (
    "attempted_clip_records",
    "candidate_observation_records",
    "finished_video_sha_recorded_clips",
    "source_window_section_records",
    "constructed_pattern_execution_failures",
    "cross_revisit_attempted_clips",
)
_STAGE_STATUS_FIELDS = (
    "clip_status_counts",
    "candidate_visual_status_counts",
    "constructed_pattern_status_counts",
    "source_probe_mode_counts",
)


def _checked_stage_funnel(result: dict) -> dict | None:
    """Verify record-only stage metrics; legacy audits remain explicitly unknown."""
    rows = result.get("cycle_diagnostics")
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ValueError("Malformed collection cycle diagnostics")
    has_stage = [
        isinstance(row, dict) and "stage_diagnostics" in row for row in rows
    ]
    if not any(has_stage):
        return None  # old audits cannot supply a retrospective stage funnel
    if not all(has_stage):
        raise ValueError("Incomplete mixed-version collection stage diagnostics")
    totals = dict.fromkeys(_STAGE_COUNT_FIELDS, 0)
    tallies = {name: Counter() for name in _STAGE_STATUS_FIELDS}
    for row in rows:
        stage = row["stage_diagnostics"]
        if not isinstance(stage, dict):
            raise ValueError("Malformed collection stage diagnostics")
        if (stage.get("measured_original_match_gold") is not False
                or stage.get("record_counts_are_verified_public_media") is not False):
            raise ValueError("Stage counts cannot claim gold or verified real media")
        for field in _STAGE_COUNT_FIELDS:
            value = stage.get(field)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid collection stage record count")
            totals[field] += value
        for field in _STAGE_STATUS_FIELDS:
            value = stage.get(field)
            if (not isinstance(value, dict)
                    or any(not isinstance(k, str) or not k or type(n) is not int
                           or n <= 0 for k, n in value.items())):
                raise ValueError("Invalid collection stage status tally")
            tallies[field].update(value)
        if (type(row.get("attempted_clips")) is not int
                or row["attempted_clips"] != stage["attempted_clip_records"]
                or sum(stage["clip_status_counts"].values())
                    != stage["attempted_clip_records"]
                or sum(stage["candidate_visual_status_counts"].values())
                    != stage["candidate_observation_records"]
                or sum(stage["constructed_pattern_status_counts"].values())
                    + stage["constructed_pattern_execution_failures"]
                    > stage["candidate_observation_records"]
                or stage["finished_video_sha_recorded_clips"]
                    > stage["attempted_clip_records"]
                or stage["cross_revisit_attempted_clips"]
                    > stage["attempted_clip_records"]
                or stage["source_window_section_records"]
                    > stage["candidate_observation_records"] * 3):
            raise ValueError("Contradictory collection stage diagnostics")
    summary = {**totals, **{
        field: dict(sorted(counter.items())) for field, counter in tallies.items()
    }}
    # Never translate saved section counts or constructed control tests into
    # acquired, independently verified Hololive video/match totals.
    summary["record_counts_are_verified_public_media"] = False
    summary["measured_original_match_gold"] = False
    return summary


def _stage_evidence_gap(summary: dict | None) -> str:
    if summary is None:
        return "UNKNOWN_NO_STAGE_DIAGNOSTICS"
    if summary["attempted_clip_records"] == 0:
        return "UNKNOWN_NO_VISUAL_CLIP_ATTEMPTS"
    if summary["finished_video_sha_recorded_clips"] == 0:
        return "UNKNOWN_NO_FINISHED_VIDEO_RECORDS"
    if summary["source_window_section_records"] == 0:
        return "UNKNOWN_NO_SOURCE_VIDEO_SECTION_RECORDS"
    if not summary["constructed_pattern_status_counts"]:
        return "UNKNOWN_NO_CONSTRUCTED_PATTERN_OUTCOMES"
    statuses = summary["candidate_visual_status_counts"]
    if not statuses.get("VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"):
        return "UNKNOWN_NO_VISUAL_SOURCE_CORROBORATION"
    return "UNREVIEWED_SOURCE_VISUAL_CORROBORATION_NOT_GOLD"


def _read_audit(path: Path, *, launcher: bool) -> dict | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or any(raw.get(k) is not v
                                        for k, v in NO_GOLD.items()):
        raise ValueError("Unsafe source-pattern audit readiness flags")
    if _timestamp(raw.get("started_at_utc")) > _timestamp(raw.get("finished_at_utc")):
        raise ValueError("Source-pattern audit completion precedes start")
    if launcher:
        if (raw.get("version") != LAUNCHER_VERSION
                or raw.get("status") not in _ALLOWED_LAUNCHER_STATUSES
                or type(raw.get("exit_code")) is not int
                or not 0 <= raw["exit_code"] <= 255):
            raise ValueError("Malformed Windows source-pattern launcher audit")
        if (raw["status"] == "COMPLETED_COLLECTOR_PROCESS") != (raw["exit_code"] == 0):
            raise ValueError("Contradictory Windows launcher exit code")
        if (raw["status"] == "FAILED_PRELAUNCH" and
                raw.get("ran_collector") is not False):
            raise ValueError("Prelaunch failure cannot have invoked collector")
        if (raw["status"] != "FAILED_PRELAUNCH" and
                raw.get("ran_collector") is not True):
            raise ValueError("Launcher must identify actual collector invocation")
        return raw
    if raw.get("version") != WORKER_VERSION:
        raise ValueError("Unknown collector audit version")
    if raw.get("status") == "UNKNOWN_COLLECTION_EXECUTION_FAILURE":
        if raw.get("result") is not None:
            raise ValueError("Failed collector audit claims successful result")
        return raw
    result = raw.get("result")
    # An intentional scheduled cooldown/other-process deferral is not a
    # measurement, even when the Python process exited successfully.
    if raw["status"] in {
        "DEFERRED_LOCAL_YTDLP_ACCESS", "BLOCKED_LOCAL_YTDLP_PACING_STATE",
    }:
        allowed_result_fields = {
            "status", "new_unique_video_samples_this_run",
            "new_distinct_candidate_streams_this_run", "local_access_state",
            "local_cooldown_reason", "next_attempt_not_before_utc",
            "collection_started", *NO_GOLD,
            "editor_cut_precision", "editor_cut_recall",
            "real_original_match_false_positive_rate",
        }
        allowed_audit_fields = {
            "version", "status", "started_at_utc", "finished_at_utc",
            "result", *allowed_result_fields - {
                "new_unique_video_samples_this_run",
                "new_distinct_candidate_streams_this_run",
                "local_access_state", "local_cooldown_reason",
                "next_attempt_not_before_utc", "collection_started",
            },
        }
        allowed_audit_fields.difference_update({"editor_cut_precision",
            "editor_cut_recall", "real_original_match_false_positive_rate"})
        allowed_audit_fields.update({"editor_cut_precision",
            "editor_cut_recall", "real_original_match_false_positive_rate"})
        allowed_statuses = {
            "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION",
            "ACTIVE_OTHER_LOCAL_YTDLP_PROCESS",
        }
        expected = (
            "BLOCKED_UNREADABLE_LOCAL_PACING_STATE"
            if raw["status"] == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
            else None
        )
        if (not isinstance(result, dict)
                or set(result) != allowed_result_fields
                or set(raw) != allowed_audit_fields
                or any(result.get(key) is not None for key in (
                    "editor_cut_precision", "editor_cut_recall",
                    "real_original_match_false_positive_rate",
                ))
                or any(raw.get(key) is not None for key in (
                    "editor_cut_precision", "editor_cut_recall",
                    "real_original_match_false_positive_rate",
                ))
                or result.get("status") != raw["status"]
                or any(result.get(k) is not v for k, v in NO_GOLD.items())
                or result.get("collection_started") is not False
                or result.get("new_unique_video_samples_this_run") is not None
                or result.get("new_distinct_candidate_streams_this_run") is not None
                or (result.get("local_access_state") not in allowed_statuses
                    if expected is None else
                    result.get("local_access_state") != expected)
                or "cycle_diagnostics" in result
                or "source_cache_byte_attestation" in result):
            raise ValueError("Deferred source-pattern audit claims measurements")
        cooldown_reason = result.get("local_cooldown_reason")
        if cooldown_reason not in (
            None, "HTTP_RATE_LIMIT", "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE",
            "UNKNOWN_HISTORICAL_COOLDOWN_REASON",
        ):
            raise ValueError("Unsafe deferred cooldown category")
        not_before = result.get("next_attempt_not_before_utc")
        if not_before is not None:
            _timestamp(not_before)
        return raw
    if (not isinstance(result, dict)
            or result.get("status") != raw.get("status")
            or any(result.get(k) is not v for k, v in NO_GOLD.items())
            or type(result.get("new_unique_video_samples_this_run")) is not int
            or result["new_unique_video_samples_this_run"] < 0
            or type(result.get("new_distinct_candidate_streams_this_run")) is not int
            or result["new_distinct_candidate_streams_this_run"] < 0):
        raise ValueError("Malformed saved source-pattern run result")
    _checked_stage_funnel(result)
    if "source_cache_byte_attestation" in result:
        checked_cached_video_attestation(result["source_cache_byte_attestation"])
    if raw["status"] == "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS":
        if result["new_unique_video_samples_this_run"] == 0:
            raise ValueError("Historical samples cannot prove fresh measurement")
    elif (not isinstance(raw.get("status"), str)
          or not raw["status"].startswith("UNKNOWN_")
          or result["new_unique_video_samples_this_run"] != 0):
        raise ValueError("Unknown/unqualified source-pattern run status")
    return raw


def _local_tools(check: bool) -> dict:
    if not check:
        return {"status": "NOT_CHECKED", "missing": []}
    missing = [name for name in ("uv", "ffmpeg", "ffprobe")
               if shutil.which(name) is None]
    javascript = None
    try:
        javascript, _path = detect_js_runtime()
    except (OSError, RuntimeError, ValueError):
        missing.append("deno>=2.3-or-node>=22")
    return {
        "status": "AVAILABLE" if not missing else "MISSING_LOCAL_DEPENDENCIES",
        "missing": missing,
        "javascript_runtime": javascript,
    }


def diagnose_source_pattern_collector(
    *, state_path: Path, report_path: Path, launcher_path: Path,
    check_tools: bool = True,
    media_output: Path = Path("data/sources/source_visual_scout"),
    audit_media_cache: bool = True,
) -> dict:
    """Summarize the exact persisted state without manufacturing new samples."""
    recorded = _read_audit(report_path, launcher=False)
    launcher = _read_audit(launcher_path, launcher=True)
    state = (
        CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.is_file() else None
    )
    summary = report_saved_patterns(state_path)
    # This revalidates the actual existing section bytes, unlike a historical
    # collector audit which only records what was checked at execution time.
    cache_audit = (
        checked_cached_video_attestation(audit_cached_source_video_sections(
            state_path=state_path, output_root=media_output,
        ))
        if audit_media_cache else None
    )
    status_counts = Counter(item.status for item in state.items.values()) if state else Counter()
    last_probe_counts = Counter()
    if state:
        for item in state.items.values():
            if item.status != "source_visual_pending":
                continue
            probe = (item.source_scout or {}).get("visual_probe")
            if isinstance(probe, dict) and isinstance(probe.get("status"), str):
                last_probe_counts[probe["status"]] += 1
    tools = _local_tools(check_tools)
    latest_report = recorded["status"] if recorded else "UNKNOWN_NO_COLLECTOR_AUDIT"
    latest_launcher = launcher["status"] if launcher else "UNKNOWN_NO_LAUNCHER_AUDIT"
    discovery = (
        recorded["result"].get("discovery")
        if recorded and isinstance(recorded.get("result"), dict) else None
    )
    metadata_paused = (
        isinstance(discovery, dict)
        and discovery.get("metadata_access_paused") is True
    )
    metadata_failures = _saved_metadata_probe_failures(state_path)
    failure_in_latest_launcher = (
        launcher is not None
        and launcher["status"] != "COMPLETED_COLLECTOR_PROCESS"
        and (recorded is None or _timestamp(launcher["finished_at_utc"])
             >= _timestamp(recorded["finished_at_utc"]))
    )
    # Only elevate a blocked pacing receipt over the generic failed
    # launcher when that *specific* collector audit belongs to the same
    # Windows invocation, not an older stale failure on disk.
    pacing_state_current = (
        latest_report == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
        and launcher is not None and recorded is not None
        and _timestamp(launcher["started_at_utc"])
            <= _timestamp(recorded["started_at_utc"])
        and _timestamp(recorded["finished_at_utc"])
            <= _timestamp(launcher["finished_at_utc"])
    )
    if failure_in_latest_launcher and pacing_state_current:
        status = "BLOCKED_LOCAL_YTDLP_PACING_STATE"
        next_action = "REPAIR_LOCAL_YTDLP_PACING_STATE_BEFORE_NEXT_ATTEMPT"
    elif failure_in_latest_launcher:
        status = "BLOCKED_LATEST_WINDOWS_LAUNCH"
        next_action = "INSPECT_LAUNCHER_LOG_AND_LOCAL_TOOLCHAIN"
    elif tools["status"] == "MISSING_LOCAL_DEPENDENCIES":
        status = "BLOCKED_LOCAL_DEPENDENCIES"
        next_action = "INSTALL_MISSING_LOCAL_TOOLCHAIN"
    elif recorded is None:
        status = "UNKNOWN_COLLECTION_NOT_YET_RECORDED"
        next_action = "INSTALL_OR_INVOKE_LOCAL_COLLECTOR"
    elif latest_report == "UNKNOWN_COLLECTION_EXECUTION_FAILURE":
        status = "UNKNOWN_COLLECTION_FAILED"
        next_action = "INSPECT_LOCAL_COLLECTION_LOG"
    elif latest_report == "DEFERRED_LOCAL_YTDLP_ACCESS":
        status = "DEFERRED_LOCAL_YTDLP_ACCESS"
        next_action = "CONTINUE_AT_NEXT_LOCAL_SCHEDULE_AFTER_PACING_GATE"
    elif latest_report == "BLOCKED_LOCAL_YTDLP_PACING_STATE":
        status = "BLOCKED_LOCAL_YTDLP_PACING_STATE"
        next_action = "REPAIR_LOCAL_YTDLP_PACING_STATE_BEFORE_NEXT_ATTEMPT"
    elif latest_report == "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS":
        status = "RECORDED_UNREVIEWED_NEW_SOURCE_VIDEO_SAMPLES"
        next_action = "CONTINUE_BOUNDED_UNREVIEWED_EVIDENCE_COLLECTION"
    elif metadata_paused:
        status = "BLOCKED_LOCAL_YOUTUBE_METADATA_ACCESS"
        next_action = "DEFER_LOCAL_YOUTUBE_REQUESTS_UNTIL_NEXT_SCHEDULE"
    else:
        status = "UNKNOWN_NO_NEW_SOURCE_VIDEO_SAMPLES"
        next_action = ("INSPECT_DISCOVERY_AND_SOURCE_PROBE_OUTCOMES"
                       if state else "CHECK_DISCOVERY_AND_CORPUS_STATE")
    result = recorded.get("result") if recorded else None
    diagnostics = (
        result.get("cycle_diagnostics", []) if isinstance(result, dict) else []
    )
    stage_funnel = _checked_stage_funnel(result) if isinstance(result, dict) else None
    outcomes = Counter()
    for cycle in diagnostics:
        for key, count in cycle.get("source_pattern_attempt_outcomes", {}).items():
            if isinstance(key, str) and type(count) is int and count >= 0:
                outcomes[key] += count
    result_summary = {
        "version": VERSION,
        "status": status,
        "next_action_code": next_action,
        "tools": tools,
        "corpus_state_found": state is not None,
        "corpus_queue_counts": dict(sorted(status_counts.items())),
        "pending_clip_last_probe_counts": dict(sorted(last_probe_counts.items())),
        "discovery_search_error_count": len(state.search_errors) if state else 0,
        "local_retryable_metadata_failure_summary": metadata_failures,
        "last_run_metadata_access_paused": metadata_paused,
        "last_run_metadata_pause_reason": (
            discovery.get("metadata_access_pause_reason")
            if metadata_paused else None
        ),
        "last_run_metadata_failure_category_counts": (
            discovery.get("metadata_failure_category_counts")
            if isinstance(discovery, dict) else None
        ),
        "last_collector_status": latest_report,
        "last_run_access_deferred": latest_report in {
            "DEFERRED_LOCAL_YTDLP_ACCESS",
            "BLOCKED_LOCAL_YTDLP_PACING_STATE",
        },
        "last_run_deferred_access_status": (
            recorded["result"].get("local_access_state")
            if latest_report in {
                "DEFERRED_LOCAL_YTDLP_ACCESS",
                "BLOCKED_LOCAL_YTDLP_PACING_STATE",
            } else None
        ),
        "last_collector_error_class": (
            recorded.get("error_class") if recorded else None
        ),
        "last_launcher_status": latest_launcher,
        "last_launcher_exit_code": launcher.get("exit_code") if launcher else None,
        "last_launcher_error_class": launcher.get("error_class") if launcher else None,
        "last_source_pattern_attempt_outcomes": dict(sorted(outcomes.items())),
        "last_run_stage_funnel": stage_funnel,
        "last_run_saved_cache_attestation": (
            result.get("source_cache_byte_attestation")
            if isinstance(result, dict) else None
        ),
        "current_source_cache_byte_attestation": cache_audit,
        "last_run_stage_evidence_gap": _stage_evidence_gap(stage_funnel),
        "new_unique_video_samples_last_run": (
            result.get("new_unique_video_samples_this_run")
            if isinstance(result, dict) else None
        ),
        "new_distinct_candidate_streams_last_run": (
            result.get("new_distinct_candidate_streams_this_run")
            if isinstance(result, dict) else None
        ),
        "cumulative_unreviewed_source_video_samples": (
            summary["real_measured_source_video_samples"]
        ),
        "cumulative_distinct_candidate_sources": (
            summary["distinct_candidate_source_streams_measured"]
        ),
        **NO_GOLD,
    }
    result_summary["local_collection_health"] = summarize_local_collection_health(result_summary)
    return result_summary
