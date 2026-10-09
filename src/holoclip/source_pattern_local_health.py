"""Offline, bounded and non-gold operational health summary for Windows collection.

The health result NEVER claims source identity or editorial accuracy. All codes
are fixed enums; no clip URLs, raw yt-dlp stderr or local file paths are emitted.
"""
from __future__ import annotations

import json
from pathlib import Path

VERSION = "local-source-pattern-health-v1"
_SETTINGS_VERSION = "source-pattern-local-settings-v1"
_PATH_KEYS = (
    "store", "state", "output", "policy_file", "report_path",
    "launcher_audit_path",
)
_INTEGER_LIMITS = {
    "cycles": (1, 20), "max_clips": (1, 2),
    "max_candidates": (1, 2), "scan_limit": (1, 100),
    "target_distinct_streams": (1, 50),
}
_DEFAULT_PATHS = {
    "state": "data/examples/corpus_growth.json",
    "output": "data/sources/source_visual_scout",
    "report_path": "data/examples/source-pattern-last-run.json",
    "launcher_audit_path": "data/examples/source-pattern-launcher-last-run.json",
}


def resolve_local_diagnostic_paths(
    root: Path, *, config_path: Path | None = None,
) -> tuple[dict[str, Path], bool]:
    """Read the *same optional* settings as the Windows launcher, without I/O
    beyond the small JSON settings file. Caller-supplied paths are never eval'd.
    """
    root = root.resolve(strict=True)
    explicit = config_path is not None
    config = (config_path if config_path is not None else
              root / "data/examples/source-pattern-local-settings.json")
    if not config.is_absolute():
        config = root / config
    if config.is_symlink():
        raise ValueError("Unsafe local settings symlink")
    values = dict(_DEFAULT_PATHS)
    if not config.exists():
        if explicit:
            raise ValueError("Explicit local settings file does not exist")
        return {key: root / value for key, value in values.items()}, False
    if not config.is_file() or config.stat().st_size > 32768:
        raise ValueError("Invalid local settings file")
    try:
        settings = json.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Invalid local settings JSON") from exc
    if (not isinstance(settings, dict)
            or settings.get("version") != _SETTINGS_VERSION
            or set(settings) - (
                set(_PATH_KEYS) | set(_INTEGER_LIMITS) | {"version", "discover"}
            )):
        raise ValueError("Unsupported local settings version or keys")
    for key in _PATH_KEYS:
        if key not in settings:
            continue
        value = settings[key]
        if (not isinstance(value, str) or not value.strip()
                or any(char in value for char in (chr(0), chr(10), chr(13), '"'))):
            raise ValueError("Invalid local settings path")
        if key in values:
            values[key] = value
    for key, (minimum, maximum) in _INTEGER_LIMITS.items():
        if key not in settings:
            continue
        value = settings[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("Invalid local settings work budget")
    if "discover" in settings and type(settings["discover"]) is not bool:
        raise ValueError("Invalid local settings discover flag")
    paths = {}
    for key, value in values.items():
        # On Windows, pathlib handles drive-absolute paths. Relative paths
        # consistently resolve against the repository root, not the task's
        # interactive-shell working directory.
        path = Path(value)
        paths[key] = path if path.is_absolute() else root / path
    return paths, True


def summarize_local_collection_health(doctor: dict) -> dict:
    """Operational triage only, not an independently checked video/clip pair."""
    if not isinstance(doctor, dict) or any(
        doctor.get(flag) is not False for flag in (
            "source_identity_verified", "reviewed_gold_available",
            "qualification_allowed", "ready", "production_ready",
        )
    ):
        raise ValueError("Unsafe or incomplete local collector doctor")
    tools = doctor.get("tools")
    funnel = doctor.get("last_run_stage_funnel")
    cache = doctor.get("current_source_cache_byte_attestation")
    if not isinstance(tools, dict) or (
        funnel is not None and not isinstance(funnel, dict)
    ) or (cache is not None and not isinstance(cache, dict)):
        raise ValueError("Malformed collection health input")
    stage = funnel or {}
    patterns = stage.get("constructed_pattern_status_counts", {})
    matches = stage.get("candidate_visual_status_counts", {})
    if not isinstance(patterns, dict) or not isinstance(matches, dict):
        raise ValueError("Malformed collection stage counts")
    counts = cache.get("cache_section_status_counts", {}) if cache else {}
    if not isinstance(counts, dict):
        raise ValueError("Malformed cache status counts")
    verified_local = counts.get(
        "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD", 0,
    )
    if type(verified_local) is not int or verified_local < 0:
        raise ValueError("Invalid local byte attestation")
    corrupted_local = sum(
        number for key, number in counts.items()
        if key.startswith("BLOCKED_") and type(number) is int and number > 0
    )
    def count(key: str) -> int:
        number = stage.get(key, 0)
        if type(number) is not int or number < 0:
            raise ValueError("Invalid source-stage observation count")
        return number
    def statuses_total(raw: dict) -> int:
        if any(type(n) is not int or n < 0 for n in raw.values()):
            raise ValueError("Malformed source-stage category count")
        return sum(raw.values())

    attempted = count("attempted_clip_records")
    original_sections = count("source_window_section_records")
    finished_hashes = count("finished_video_sha_recorded_clips")
    measured_controls = statuses_total(patterns)
    sampled_correspondences = matches.get(
        "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD", 0,
    )
    if type(sampled_correspondences) is not int or sampled_correspondences < 0:
        raise ValueError("Malformed source-visual correspondence count")
    last_samples = doctor.get("new_unique_video_samples_last_run")
    if last_samples is not None and (
        type(last_samples) is not int or last_samples < 0
    ):
        raise ValueError("Malformed newly measured pattern count")

    status = doctor.get("status")
    if status == "BLOCKED_LATEST_WINDOWS_LAUNCH":
        blocker, action = "WINDOWS_LAUNCH", "REPAIR_LOCAL_LAUNCH_CONFIGURATION"
    elif status == "BLOCKED_LOCAL_DEPENDENCIES":
        blocker, action = "LOCAL_TOOLS", "REPAIR_UV_FFMPEG_AND_JS_RUNTIME"
    elif status == "UNKNOWN_COLLECTION_NOT_YET_RECORDED":
        blocker, action = "NO_LOCAL_RUN", "CHECK_NATIVE_WINDOWS_COLLECTOR_TASK"
    elif status == "UNKNOWN_COLLECTION_FAILED":
        blocker, action = "COLLECTOR_EXECUTION", "INSPECT_SANITIZED_LOCAL_RUN_DIAGNOSTICS"
    elif status == "DEFERRED_LOCAL_YTDLP_ACCESS":
        blocker, action = (
            "LOCAL_YTDLP_ACCESS_PAUSED", "CONTINUE_AT_NEXT_LOCAL_SCHEDULE"
        )
    elif status == "BLOCKED_LOCAL_YTDLP_PACING_STATE":
        blocker, action = (
            "LOCAL_YTDLP_PACING_STORAGE_BLOCKED", "REPAIR_LOCAL_PACING_STORAGE"
        )
    elif doctor.get("last_run_metadata_access_paused") is True:
        blocker, action = "YOUTUBE_ACCESS", "DEFER_TO_NEXT_LOCAL_SCHEDULE"
    elif corrupted_local:
        blocker, action = "LOCAL_CACHE_INTEGRITY", "RECHECK_DAMAGED_LOCAL_MEDIA_CACHE"
    elif last_samples is not None and last_samples > 0 and verified_local == 0:
        blocker, action = (
            "SOURCE_CACHE_NOT_REVALIDATED", "CHECK_LOCAL_CACHE_BYTE_ATTESTATION"
        )
    elif funnel is None:
        blocker, action = "NO_STAGE_DIAGNOSTICS", "RUN_BOUNDED_NATIVE_COLLECTION"
    elif attempted == 0:
        blocker, action = "NO_VISUAL_ATTEMPTS", "INSPECT_DISCOVERY_AND_PENDING_QUEUE"
    elif finished_hashes == 0:
        blocker, action = "NO_FINISHED_VIDEO_RECORD", "INSPECT_FINISHED_VIDEO_ACQUISITION"
    elif original_sections == 0:
        blocker, action = "NO_SOURCE_VIDEO_RECORD", "INSPECT_SOURCE_VIDEO_ACQUISITION"
    elif verified_local == 0:
        blocker, action = "NO_REVALIDATED_SOURCE_BYTES", "CHECK_LOCAL_CACHE_BYTE_ATTESTATION"
    elif cache is None or cache.get("complete_pending_queue_audited") is not True:
        blocker, action = "PARTIAL_CACHE_SCOPE", "CHECK_FULL_BOUNDED_LOCAL_CACHE_SCOPE"
    elif sampled_correspondences == 0:
        blocker, action = "NO_VISUAL_SOURCE_CORROBORATION", "REVIEW_BOUNDED_PAIRING_DIAGNOSTICS"
    elif measured_controls == 0:
        blocker, action = "NO_CONSTRUCTED_PATTERN_CONTROL", "REVIEW_LOCAL_PATTERN_CONTROL_FAILURES"
    else:
        blocker, action = "REVIEWED_GOLD_STILL_MISSING", "CONTINUE_INDEPENDENT_QUALIFICATION"
    return {
        "version": VERSION,
        "health_status": "UNREVIEWED_DIAGNOSTIC_ONLY",
        "primary_blocker": blocker,
        "next_action_code": action,
        "last_collector_status": status if status in {
            "BLOCKED_LATEST_WINDOWS_LAUNCH", "BLOCKED_LOCAL_DEPENDENCIES",
            "UNKNOWN_COLLECTION_NOT_YET_RECORDED", "UNKNOWN_COLLECTION_FAILED",
            "BLOCKED_LOCAL_YOUTUBE_METADATA_ACCESS",
            "RECORDED_UNREVIEWED_NEW_SOURCE_VIDEO_SAMPLES",
            "UNKNOWN_NO_NEW_SOURCE_VIDEO_SAMPLES",
            "DEFERRED_LOCAL_YTDLP_ACCESS", "BLOCKED_LOCAL_YTDLP_PACING_STATE",
        } else "UNKNOWN_LOCAL_COLLECTOR_STATUS",
        "last_run_new_pattern_samples": last_samples,
        "last_run_access_deferred": doctor.get("last_run_access_deferred") is True,
        "last_run_deferred_access_status": doctor.get(
            "last_run_deferred_access_status"
        ),
        "last_run_attempted_clip_records": attempted if funnel is not None else None,
        "last_run_finished_sha_records": finished_hashes if funnel is not None else None,
        "last_run_source_section_records": original_sections if funnel is not None else None,
        "current_local_verified_section_records": verified_local,
        "current_local_corrupt_section_records": corrupted_local,
        "current_cache_audit_complete": (
            cache.get("complete_pending_queue_audited") is True
            if cache else False
        ),
        "last_run_sampled_source_correspondence_records": (
            sampled_correspondences if funnel is not None else None
        ),
        "last_run_constructed_pattern_control_records": (
            measured_controls if funnel is not None else None
        ),
        "metadata_access_paused": doctor.get("last_run_metadata_access_paused") is True,
        "metadata_retryable_clip_records": (
            doctor.get("local_retryable_metadata_failure_summary") or {}
        ).get("retryable_clip_records", 0),
        "record_counts_are_verified_public_video": False,
        "independent_original_to_finished_match_verified": False,
        "human_editor_gold_verified": False,
        "ready": False,
        "production_ready": False,
    }
