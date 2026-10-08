"""Conservative one-shot local video checks following sparse, weak image matches.

A weak hint is not a cut, source identity, confidence score or negative label.
Its only permitted effect is the *next* source-video sampling location. Every
refinement must be followed by the existing broad source search cursor.
"""
from __future__ import annotations

import math

VERSION = "adaptive-source-visual-refinement-v1"
MAX_REFINEMENT_WINDOWS = 3
WINDOW_SECONDS = 12.


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def make_weak_hint(
    *, source_id: str, duration: float, seed_start: float,
    completed_starts: list[float], next_cursor: int, finished_sha: str,
) -> dict:
    if (not source_id or not _number(duration)
            or not _number(seed_start) or not 300 <= duration <= 86400
            or not 0 <= seed_start <= duration - WINDOW_SECONDS
            or type(next_cursor) is not int or next_cursor < 0
            or not isinstance(completed_starts, list)
            or not 1 <= len(completed_starts) <= MAX_REFINEMENT_WINDOWS
            or any(not _number(t) or not 0 <= t <= duration - WINDOW_SECONDS
                   for t in completed_starts)
            or seed_start not in completed_starts
            or len(set(completed_starts)) != len(completed_starts)
            or not isinstance(finished_sha, str) or len(finished_sha) != 64):
        raise ValueError("Invalid nonpromotable weak visual search hint")
    return {
        "version": VERSION, "source_id": source_id,
        "duration_seconds": duration,
        "seed_start": seed_start,
        "completed_coarse_starts": completed_starts,
        "next_cursor": next_cursor,
        "finished_media_sha256": finished_sha,
        "source_identity_verified": False,
        "qualification_allowed": False,
        "reviewed_gold_available": False,
    }


def adaptive_refinement_starts(
    hint: dict, *, source_id: str, duration: float,
    cursor: int, finished_sha: str,
) -> list[float]:
    """Return up to three non-repeated adjacent windows or reject stale hints.

    The clip hash, source ID/duration, and coarse cursor bind the hints to the
    exact existing media epoch. Never turn unsafe hints into new video seeks.
    """
    if (not isinstance(hint, dict)
            or hint.get("version") != VERSION
            or hint.get("source_id") != source_id
            or hint.get("duration_seconds") != duration
            or hint.get("next_cursor") != cursor
            or hint.get("finished_media_sha256") != finished_sha
            or hint.get("source_identity_verified") is not False
            or hint.get("qualification_allowed") is not False
            or hint.get("reviewed_gold_available") is not False):
        raise ValueError("Weak visual hint has stale or promoted identity")
    # Repeat the same validator for persisted hint fields.
    trusted = make_weak_hint(
        source_id=source_id, duration=duration,
        seed_start=hint.get("seed_start"),
        completed_starts=hint.get("completed_coarse_starts"),
        next_cursor=cursor, finished_sha=finished_sha,
    )
    if trusted != hint:
        raise ValueError("Visual refinement hint contains unsupported fields")

    seed = hint["seed_start"]
    excluded = hint["completed_coarse_starts"]
    result = []
    # Search around a plausible coarse source interval, not across the
    # entire livestream. Alternate before/after rather than following a
    # false-positive hint indefinitely.
    for distance in (1, -1, 2, -2, 3, -3, 4, -4):
        candidate = round(seed + distance * WINDOW_SECONDS, 3)
        if (not 0 <= candidate <= duration - WINDOW_SECONDS
                or any(abs(candidate - t) < WINDOW_SECONDS - .001
                       for t in excluded + result)):
            continue
        result.append(candidate)
        if len(result) == MAX_REFINEMENT_WINDOWS:
            break
    return result
