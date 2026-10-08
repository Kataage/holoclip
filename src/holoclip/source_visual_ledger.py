"""Bounded, cross-run, two-sided independent visual evidence ledger.

Evidence is reference-free and *never* establishes original-video identity.
Neither repeated processing of identical media nor multiple matches to the
same 12-second finished clip sample are independent corroboration.
"""
from __future__ import annotations

import math
import re

VERSION = "unreviewed-independent-video-window-ledger-v1"
MAX_EVENTS = 32
MAX_CANDIDATES = 4
_FLAGS = {
    "source_identity_verified": False,
    "original_video_absolute_pts_verified": False,
    "frame_accurate_cuts_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "event_split_verified": False,
    "used_in_learning_or_holdout": False,
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _range(span):
    return (type(span) is list and len(span) == 2
            and all(_finite(x) for x in span)
            and 0 <= span[0] < span[1] <= 86400
            and span[1] - span[0] <= 15)


def _times(values):
    return (type(values) is list and 3 <= len(values) <= 12
            and all(_finite(x) and 0 <= x <= 14 for x in values)
            and all(b - a >= .4 for a, b in zip(values, values[1:])))


def _event_ok(e: dict, clip_id: str, candidates: set[str]) -> bool:
    if not isinstance(e, dict) or set(e) != {
        "clip_id", "source_stream_id", "finished_range_seconds",
        "source_requested_range_seconds", "finished_media_sha256",
        "source_media_sha256", "finished_frame_times_local",
        "source_frame_times_local", "matched_frame_count",
        "source_identity_verified", "reviewed_gold_available",
    }:
        return False
    if (e["clip_id"] != clip_id or e["source_stream_id"] not in candidates
            or e["source_identity_verified"] is not False
            or e["reviewed_gold_available"] is not False
            or not _range(e["finished_range_seconds"])
            or not _range(e["source_requested_range_seconds"])
            or not _sha(e["finished_media_sha256"])
            or not _sha(e["source_media_sha256"])
            or not _times(e["finished_frame_times_local"])
            or not _times(e["source_frame_times_local"])
            or type(e["matched_frame_count"]) is not int
            or e["matched_frame_count"] != len(e["finished_frame_times_local"])
            or len(e["source_frame_times_local"]) != e["matched_frame_count"]):
        return False
    return (max(abs((b-a) - (d-c)) for a, b, c, d in zip(
        e["source_frame_times_local"], e["source_frame_times_local"][1:],
        e["finished_frame_times_local"], e["finished_frame_times_local"][1:],
    )) <= .9)


def _nonoverlap(a, b):
    return a[1] <= b[0] + .001 or b[1] <= a[0] + .001


def _summary(events: list[dict]) -> dict:
    by_source: dict[str, list] = {}
    for event in events:
        by_source.setdefault(event["source_stream_id"], []).append(event)
    support = {}
    for source, records in sorted(by_source.items()):
        independent = []
        for e in sorted(records, key=lambda v: (
            v["finished_range_seconds"][0],
            v["source_requested_range_seconds"][0],
        )):
            if all(
                _nonoverlap(e["finished_range_seconds"], existing["finished_range_seconds"])
                and _nonoverlap(
                    e["source_requested_range_seconds"],
                    existing["source_requested_range_seconds"],
                )
                for existing in independent
            ):
                independent.append(e)
        support[source] = len(independent)
    if len(support) > 1:
        status = "UNKNOWN_DIFFERENT_CANDIDATES_SHARE_VISUAL_EVIDENCE"
    elif any(count >= 2 for count in support.values()):
        status = "UNREVIEWED_TWO_SIDED_DIVERSE_VISUAL_HYPOTHESIS"
    elif support:
        status = "UNREVIEWED_SINGLE_VISUAL_WINDOW_HYPOTHESIS"
    else:
        status = "UNKNOWN_NO_CORROBORATED_VISUAL_WINDOWS"
    return {
        "status": status,
        "recorded_observations": len(events),
        "independent_two_sided_window_counts": support,
        "corroborated_candidate_count": len(support),
        **_FLAGS,
    }


def checked_ledger(value: dict | None, *, clip_id: str,
                   candidate_ids: list[str]) -> dict:
    """Validate *all* persisted data before acquisition, including no-gold flags."""
    if (not isinstance(clip_id, str) or not clip_id
            or not isinstance(candidate_ids, list)
            or not 1 <= len(candidate_ids) <= MAX_CANDIDATES
            or len(set(candidate_ids)) != len(candidate_ids)
            or any(not isinstance(k, str) or not k for k in candidate_ids)):
        raise ValueError("Invalid source-visual evidence owner")
    empty = {
        "version": VERSION, "clip_id": clip_id, "events": [],
        "summary": _summary([]), **_FLAGS,
    }
    if value is None:
        return empty
    if (not isinstance(value, dict)
            or set(value) != set(empty)
            or value.get("version") != VERSION
            or value.get("clip_id") != clip_id
            or any(value.get(k) is not False for k in _FLAGS)):
        raise ValueError("Stale or promoted source-visual evidence ledger")
    events = value.get("events")
    if (not isinstance(events, list) or len(events) > MAX_EVENTS
            or any(not _event_ok(e, clip_id, set(candidate_ids)) for e in events)
            or len({(
                e["source_stream_id"], tuple(e["finished_range_seconds"]),
                tuple(e["source_requested_range_seconds"]),
            ) for e in events}) != len(events)
            or value.get("summary") != _summary(events)):
        raise ValueError("Inconsistent or forged visual-source evidence ledger")
    # The same requested finished interval MUST refer to the same sampled
    # bytes regardless of which original candidate it was compared with.
    # Likewise the same original section must not silently mutate across
    # finished-video intervals. This check is global across all events.
    finished_hashes = {}
    original_hashes = {}
    for event in events:
        finished_key = tuple(event["finished_range_seconds"])
        source_key = (
            event["source_stream_id"],
            tuple(event["source_requested_range_seconds"]),
        )
        for seen, key, sha in (
            (finished_hashes, finished_key, event["finished_media_sha256"]),
            (original_hashes, source_key, event["source_media_sha256"]),
        ):
            if key in seen and seen[key] != sha:
                raise ValueError("Video bytes changed across source candidates or runs")
            seen[key] = sha
    return value


def append_visual_observation(
    existing: dict, *, clip_id: str, candidate_ids: list[str],
    source_stream_id: str, finished_range: list[float],
    source_ranges: list[list[float]], finished_sha: str,
    source_shas: list[str], observed: dict,
) -> tuple[dict, bool]:
    """Append a measured 3+-frame event; ignore incomplete synthetic/status data.

    Matched window index MUST refer to a media-measured original section.
    No saved event supplies a globally accurate source PTS.
    """
    ledger = checked_ledger(existing, clip_id=clip_id, candidate_ids=candidate_ids)
    if (not isinstance(observed, dict)
            or observed.get("status") != "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"):
        return ledger, False
    index = observed.get("matched_source_window")
    clip_times = observed.get("finished_frame_times_local")
    source_times = observed.get("source_frame_times_local")
    if (type(index) is not int or not 0 <= index < len(source_ranges)
            or len(source_ranges) != len(source_shas)
            or not _range(finished_range)
            or not _range(source_ranges[index])
            or not _sha(finished_sha) or not _sha(source_shas[index])
            or not _times(clip_times) or not _times(source_times)
            or len(clip_times) != len(source_times)
            or observed.get("matched_frames") != len(clip_times)):
        return ledger, False
    event = {
        "clip_id": clip_id, "source_stream_id": source_stream_id,
        "finished_range_seconds": finished_range,
        "source_requested_range_seconds": source_ranges[index],
        "finished_media_sha256": finished_sha,
        "source_media_sha256": source_shas[index],
        "finished_frame_times_local": clip_times,
        "source_frame_times_local": source_times,
        "matched_frame_count": len(clip_times),
        "source_identity_verified": False,
        "reviewed_gold_available": False,
    }
    if not _event_ok(event, clip_id, set(candidate_ids)):
        return ledger, False
    for previous in ledger["events"]:
        if (previous["source_stream_id"] == source_stream_id
                and previous["finished_range_seconds"] == finished_range
                and previous["source_requested_range_seconds"] == source_ranges[index]):
            # Repeating the same 12s clip/source section MUST not grow evidence.
            if (previous["finished_media_sha256"] != finished_sha
                    or previous["source_media_sha256"] != source_shas[index]):
                raise ValueError("Media changed under an already-counted visual interval")
            return ledger, False
    if len(ledger["events"]) >= MAX_EVENTS:
        return ledger, False
    events = [*ledger["events"], event]
    updated = {**ledger, "events": events, "summary": _summary(events)}
    checked_ledger(updated, clip_id=clip_id, candidate_ids=candidate_ids)
    return updated, True
