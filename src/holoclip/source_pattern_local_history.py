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
    if not isinstance(event, dict) or set(event) != {
        "started_at_utc", "finished_at_utc", "status", "exit_code",
        "ran_collector", "error_class", "new_unique_video_samples",
        "new_distinct_candidate_streams", "collector_report_correlated",
        "ready", "production_ready",
    }:
        raise ValueError("Invalid or incomplete launcher history event")
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
    }
