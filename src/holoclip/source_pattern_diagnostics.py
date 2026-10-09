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
from holoclip.source_visual_pattern_report import report_saved_patterns

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
    if (not isinstance(result, dict)
            or result.get("status") != raw.get("status")
            or any(result.get(k) is not v for k, v in NO_GOLD.items())
            or type(result.get("new_unique_video_samples_this_run")) is not int
            or result["new_unique_video_samples_this_run"] < 0
            or type(result.get("new_distinct_candidate_streams_this_run")) is not int
            or result["new_distinct_candidate_streams_this_run"] < 0):
        raise ValueError("Malformed saved source-pattern run result")
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
) -> dict:
    """Summarize the exact persisted state without manufacturing new samples."""
    recorded = _read_audit(report_path, launcher=False)
    launcher = _read_audit(launcher_path, launcher=True)
    state = (
        CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.is_file() else None
    )
    summary = report_saved_patterns(state_path)
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
    failure_in_latest_launcher = (
        launcher is not None
        and launcher["status"] != "COMPLETED_COLLECTOR_PROCESS"
        and (recorded is None or _timestamp(launcher["finished_at_utc"])
             >= _timestamp(recorded["finished_at_utc"]))
    )
    if failure_in_latest_launcher:
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
    elif latest_report == "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS":
        status = "RECORDED_UNREVIEWED_NEW_SOURCE_VIDEO_SAMPLES"
        next_action = "CONTINUE_BOUNDED_UNREVIEWED_EVIDENCE_COLLECTION"
    else:
        status = "UNKNOWN_NO_NEW_SOURCE_VIDEO_SAMPLES"
        next_action = ("INSPECT_DISCOVERY_AND_SOURCE_PROBE_OUTCOMES"
                       if state else "CHECK_DISCOVERY_AND_CORPUS_STATE")
    result = recorded.get("result") if recorded else None
    diagnostics = (
        result.get("cycle_diagnostics", []) if isinstance(result, dict) else []
    )
    outcomes = Counter()
    for cycle in diagnostics:
        for key, count in cycle.get("source_pattern_attempt_outcomes", {}).items():
            if isinstance(key, str) and type(count) is int and count >= 0:
                outcomes[key] += count
    return {
        "version": VERSION,
        "status": status,
        "next_action_code": next_action,
        "tools": tools,
        "corpus_state_found": state is not None,
        "corpus_queue_counts": dict(sorted(status_counts.items())),
        "pending_clip_last_probe_counts": dict(sorted(last_probe_counts.items())),
        "discovery_search_error_count": len(state.search_errors) if state else 0,
        "last_collector_status": latest_report,
        "last_collector_error_class": (
            recorded.get("error_class") if recorded else None
        ),
        "last_launcher_status": latest_launcher,
        "last_launcher_exit_code": launcher.get("exit_code") if launcher else None,
        "last_launcher_error_class": launcher.get("error_class") if launcher else None,
        "last_source_pattern_attempt_outcomes": dict(sorted(outcomes.items())),
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
