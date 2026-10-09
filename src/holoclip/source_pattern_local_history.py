"""Offline validation and safe trend reporting for native Windows launcher history.

Written by Windows PowerShell without requiring Python/uv to have started.
No URLs, paths, stderr, uploaded video, or human editorial gold are present.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = "source-pattern-launcher-history-v1"
MAX_EVENTS = 90
_ALLOWED = {
    "FAILED_PRELAUNCH", "FAILED_COLLECTOR_PROCESS", "COMPLETED_COLLECTOR_PROCESS",
}

# The existing v1 ring stays readable: these are optional *additive*
# measurements on NEW events, not fabricated fields on legacy events.
_STAGE_FIELDS = {
    "stage_metrics_correlated", "last_run_visual_pair_attempts",
    "last_run_source_window_records", "last_run_sampled_visual_correspondences",
}
_BASE_FIELDS = {
    "started_at_utc", "finished_at_utc", "status", "exit_code",
    "ran_collector", "error_class", "new_unique_video_samples",
    "new_distinct_candidate_streams", "collector_report_correlated",
    "ready", "production_ready",
}
FAILURE_THRESHOLD = 3
ZERO_SAMPLE_THRESHOLD = 5
ZERO_NEW_SOURCE_THRESHOLD = 5
VISUAL_STALL_THRESHOLD = 3


def _trailing_streak(events: list[dict], predicate) -> int:
    streak = 0
    for event in reversed(events):
        if not predicate(event):
            break
        streak += 1
    return streak


def _stall_signals(events: list[dict]) -> dict:
    """Observed *runs*, never assumed days, editorial negatives or source gold."""
    failure = _trailing_streak(
        events, lambda e: e["status"] != "COMPLETED_COLLECTOR_PROCESS",
    )
    zero_samples = _trailing_streak(
        events, lambda e: (
            e["collector_report_correlated"]
            and e["new_unique_video_samples"] == 0
        ),
    )
    zero_new_sources = _trailing_streak(
        events, lambda e: (
            e["collector_report_correlated"]
            and e["new_unique_video_samples"] > 0
            and e["new_distinct_candidate_streams"] == 0
        ),
    )
    # Each observation must come from a SAME-RUN non-gold stage report with
    # actual original interval records AND paired comparison attempts. A
    # legacy event or a failed/missing source section breaks the streak.
    visual = _trailing_streak(
        events, lambda e: (
            e["stage_metrics_correlated"]
            and e["last_run_visual_pair_attempts"] > 0
            and e["last_run_source_window_records"] > 0
            and e["last_run_sampled_visual_correspondences"] == 0
        ),
    )
    signals = []
    if failure >= FAILURE_THRESHOLD:
        signals.append("REPEATED_NATIVE_LAUNCH_FAILURES")
    if visual >= VISUAL_STALL_THRESHOLD:
        signals.append("REPEATED_BOUNDED_VISUAL_NONCORROBORATION")
    if zero_samples >= ZERO_SAMPLE_THRESHOLD:
        signals.append("REPEATED_ZERO_NEW_PATTERN_SAMPLES")
    if zero_new_sources >= ZERO_NEW_SOURCE_THRESHOLD:
        signals.append("REPEATED_NO_NEW_DISTINCT_SOURCE_CANDIDATES")
    priority = signals[0] if signals else None
    action_map = {
        "REPEATED_NATIVE_LAUNCH_FAILURES": "INSPECT_LOCAL_WINDOWS_LAUNCH_FAILURES",
        "REPEATED_BOUNDED_VISUAL_NONCORROBORATION":
            "INSPECT_NATIVE_BOUNDED_PAIRING_AND_SOURCE_WINDOWS",
        "REPEATED_ZERO_NEW_PATTERN_SAMPLES":
            "INSPECT_LOCAL_DISCOVERY_AND_MEASUREMENT_FUNNEL",
        "REPEATED_NO_NEW_DISTINCT_SOURCE_CANDIDATES":
            "INSPECT_LOCAL_CANDIDATE_STREAM_DIVERSITY",
    }
    return {
        "status": ("OPERATIONAL_STALL_SIGNAL_RECORDED" if priority else
                   "UNKNOWN_INSUFFICIENT_LOCAL_STALL_EVIDENCE"),
        "primary_signal": priority,
        "signals": signals,
        "next_action_code": action_map.get(
            priority, "CONTINUE_BOUNDED_LOCAL_DIAGNOSIS",
        ),
        "consecutive_failed_launcher_runs": failure,
        "consecutive_correlated_zero_sample_runs": zero_samples,
        "consecutive_correlated_no_new_distinct_source_runs": zero_new_sources,
        "consecutive_correlated_noncorroborated_visual_runs": visual,
        "thresholds": {
            "failed_launcher_runs": FAILURE_THRESHOLD,
            "zero_sample_runs": ZERO_SAMPLE_THRESHOLD,
            "no_new_distinct_source_runs": ZERO_NEW_SOURCE_THRESHOLD,
            "noncorroborated_visual_runs": VISUAL_STALL_THRESHOLD,
        },
        "calendar_duration_inferred": False,
        "visual_negative_or_editor_gold_proven": False,
        "source_identity_verified": False,
        "ready": False,
        "production_ready": False,
    }


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid launcher history timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Malformed launcher history timestamp") from exc
    if stamp.tzinfo is None:
        raise ValueError("Naive launcher history timestamp")
    return stamp.astimezone(timezone.utc)


def _nonnegative_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 100000


def _validate_event(event: object) -> dict:
    if not isinstance(event, dict) or set(event) not in (
        _BASE_FIELDS, _BASE_FIELDS | _STAGE_FIELDS,
    ):
        raise ValueError("Invalid or incomplete launcher history event")
    # Allow older v1 events unchanged on disk, but NEVER synthesize known
    # per-run stage measurements from historical launches.
    event = {
        **{
            "stage_metrics_correlated": False,
            "last_run_visual_pair_attempts": None,
            "last_run_source_window_records": None,
            "last_run_sampled_visual_correspondences": None,
        },
        **event,
    }
    if (event["status"] not in _ALLOWED
            or event["ready"] is not False
            or event["production_ready"] is not False
            or type(event["ran_collector"]) is not bool
            or type(event["collector_report_correlated"]) is not bool
            or type(event["exit_code"]) is not int
            or not 0 <= event["exit_code"] <= 255):
        raise ValueError("Unsafe launcher history provenance")
    began = _time(event["started_at_utc"])
    finished = _time(event["finished_at_utc"])
    if finished < began:
        raise ValueError("Launcher history time ordering reversed")
    success = event["status"] == "COMPLETED_COLLECTOR_PROCESS"
    if (success != (event["exit_code"] == 0)
            or event["ran_collector"] != (
                event["status"] != "FAILED_PRELAUNCH"
            )):
        raise ValueError("Contradictory native launcher result")
    name = event["error_class"]
    if (name is not None and
            (not isinstance(name, str) or
             not name.isascii() or
             len(name) > 64 or
             not name.replace("_", "").isalnum())):
        raise ValueError("Unsafe launcher exception class")
    if event["collector_report_correlated"]:
        if (not success
                or not _nonnegative_count(event["new_unique_video_samples"])
                or not _nonnegative_count(event["new_distinct_candidate_streams"])):
            raise ValueError("Contradictory fresh-video history counts")
    elif (event["new_unique_video_samples"] is not None
          or event["new_distinct_candidate_streams"] is not None):
        raise ValueError("Uncorrelated old collection report was promoted")
    if type(event["stage_metrics_correlated"]) is not bool:
        raise ValueError("Invalid visual stage evidence flag")
    stage_fields = (
        event["last_run_visual_pair_attempts"],
        event["last_run_source_window_records"],
        event["last_run_sampled_visual_correspondences"],
    )
    if event["stage_metrics_correlated"]:
        if (not success or not event["collector_report_correlated"]
                or not all(_nonnegative_count(value) for value in stage_fields)
                or stage_fields[1] > stage_fields[0] * 3
                or stage_fields[2] > stage_fields[0]):
            raise ValueError("Invalid or promoted per-run visual stage counts")
    elif any(value is not None for value in stage_fields):
        raise ValueError("Uncorrelated visual pairing was promoted")
    return event


def read_launcher_history(path: Path) -> dict:
    """Fail closed on corrupted files. Missing history is not a fabricated run."""
    unknown = {
        "version": VERSION,
        "status": "UNKNOWN_NO_SAVED_NATIVE_LAUNCHER_HISTORY",
        "retained_runs": 0,
        "max_retained_runs": MAX_EVENTS,
        "history_reached_retention_cap": False,
        "last_run": None,
        "previous_run": None,
        "last_run_new_samples_delta": None,
        "last_run_new_streams_delta": None,
        "consecutive_failure_runs": 0,
        "status_counts": {},
        "last_seven_runs": [],
        "legacy_runs_without_stage_metrics": 0,
        "operational_stall": _stall_signals([]),
        "ready": False, "production_ready": False,
    }
    if path.is_symlink():
        raise ValueError("Unsafe launcher history symlink")
    if not path.exists():
        return unknown
    if not path.is_file() or path.stat().st_size > 262144:
        raise ValueError("Unsafe launcher history file")
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Invalid launcher history JSON") from exc
    if (not isinstance(saved, dict) or set(saved) != {
        "version", "events", "ready", "production_ready",
    } or saved["version"] != VERSION or saved["ready"] is not False
            or saved["production_ready"] is not False
            or not isinstance(saved["events"], list)
            or len(saved["events"]) > MAX_EVENTS):
        raise ValueError("Invalid launcher history envelope")
    legacy_rows = sum(set(row) == _BASE_FIELDS for row in saved["events"]
                      if isinstance(row, dict))
    records = [_validate_event(row) for row in saved["events"]]
    starts = [_time(row["started_at_utc"]) for row in records]
    if any(second <= first for first, second in zip(starts, starts[1:])):
        raise ValueError("Launcher history out of order or duplicated")
    latest = records[-1] if records else None
    prior = records[-2] if len(records) > 1 else None
    counters = Counter(row["status"] for row in records)
    failure_streak = 0
    for row in reversed(records):
        if row["status"] == "COMPLETED_COLLECTOR_PROCESS":
            break
        failure_streak += 1

    def compact(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {
            "started_at_utc": row["started_at_utc"],
            "finished_at_utc": row["finished_at_utc"],
            "status": row["status"],
            "exit_code": row["exit_code"],
            "error_class": row["error_class"],
            "new_unique_video_samples": row["new_unique_video_samples"],
            "new_distinct_candidate_streams": row["new_distinct_candidate_streams"],
            "collector_report_correlated": row["collector_report_correlated"],
            "stage_metrics_correlated": row["stage_metrics_correlated"],
            "last_run_visual_pair_attempts": row["last_run_visual_pair_attempts"],
            "last_run_source_window_records": row["last_run_source_window_records"],
            "last_run_sampled_visual_correspondences":
                row["last_run_sampled_visual_correspondences"],
        }

    def delta(key: str) -> int | None:
        if (not latest or not prior or
                not latest["collector_report_correlated"] or
                not prior["collector_report_correlated"]):
            return None
        # The change is between per-invocation acquisition increments, *not*
        # cumulative new video proof or independently verified accuracy.
        return latest[key] - prior[key]

    return {
        **unknown,
        "status": ("RECORDED_NATIVE_LAUNCHER_HISTORY"
                   if records else "UNKNOWN_EMPTY_NATIVE_LAUNCHER_HISTORY"),
        "retained_runs": len(records),
        "history_reached_retention_cap": len(records) == MAX_EVENTS,
        "last_run": compact(latest),
        "previous_run": compact(prior),
        "last_run_new_samples_delta": delta("new_unique_video_samples"),
        "last_run_new_streams_delta": delta("new_distinct_candidate_streams"),
        "consecutive_failure_runs": failure_streak,
        "status_counts": dict(sorted(counters.items())),
        "last_seven_runs": [compact(row) for row in records[-7:]],
        "legacy_runs_without_stage_metrics": legacy_rows,
        "operational_stall": _stall_signals(records),
    }
