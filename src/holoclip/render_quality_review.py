"""Deterministic non-destructive review ledger for measured edit anomalies.

No transcript text, source paths, arbitrary FFmpeg expressions, or creative
quality predictions. All suggested actions require inspection; no change to
a rendered MP4, subtitles, render state or publication eligibility.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from holoclip.domain import ClipTimeline
from holoclip.render_plan import _hash, _no_links

REVIEW_VERSION = "holoclip-review-ledger-v1"
SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_ITEMS = 300
_MAX_BYTES = 65536
_KINDS = frozenset({"blackout", "freeze", "fast_subtitle", "wide_subtitle"})
_ACTIONS = {
    "blackout": "INSPECT_DARK_SCENE_AND_SOURCE",
    "freeze": "VERIFY_INTENTIONAL_HOLD_VS_STALLED_FRAMES",
    "fast_subtitle": "CHECK_ASR_TIMING_AND_SUBTITLE_READABILITY",
    "wide_subtitle": "CHECK_SUBTITLE_WRAP_AND_LEGIBILITY",
}


def _boundaries(timeline: ClipTimeline) -> list[tuple[float, float, str]]:
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    ordered = sorted(timeline.segments, key=lambda item: item.output_order)
    if not 1 <= len(ordered) <= 32 or not 0 < timeline.estimated_duration <= 180:
        raise ValueError("LOCAL_EDIT_REVIEW_TIMELINE_UNTRUSTED")
    clock = 0.0
    windows = []
    for item in ordered:
        end = clock + item.source_end - item.source_start
        windows.append((clock, end, item.id))
        clock = end
    return windows


def candidates_for_review(
    timeline: ClipTimeline, observed: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Place verified technical anomalies on the output timeline only.

    Durations/rules are descriptive, not calibrated false-positive probabilities.
    Intentional black scenes, holding on still illustrations and quick reactions
    can be legitimate; never classify these as objectively incorrect cuts.
    """
    windows = _boundaries(timeline)
    if len(observed) > _MAX_ITEMS:
        raise ValueError("LOCAL_EDIT_REVIEW_ITEM_LIMIT")
    results = []
    for item in observed:
        kind = item.get("kind")
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        if (kind not in _KINDS
                or not isinstance(start, (float, int))
                or isinstance(start, bool)
                or not math.isfinite(start)
                or not 0 <= start <= windows[-1][1] + 0.15
                or end is not None and (
                    not isinstance(end, (float, int)) or isinstance(end, bool)
                    or not math.isfinite(end) or end < start
                    or end > windows[-1][1] + 1.1
                )):
            raise ValueError("LOCAL_EDIT_REVIEW_ITEM_UNTRUSTED")
        # Every implicated output cut is retained; crossing a seam should
        # not be silently attributed only to the earlier source.
        affected = [
            segment_id
            for left, right, segment_id in windows
            if start < right + 0.01 and (end is None or end > left - 0.01)
            and (end is None or start <= right + 0.01)
        ]
        if not affected:
            raise ValueError("LOCAL_EDIT_REVIEW_OUTSIDE_TIMELINE")
        position = (
            "cut_boundary" if len(affected) > 1
            else "inside_cut"
        )
        candidate = {
            "kind": kind,
            "start_seconds": round(float(start), 3),
            "end_seconds": round(float(end), 3) if end is not None else None,
            "segment_ids": affected,
            "position": position,
            "recommendation": _ACTIONS[kind],
            "auto_repair_allowed": False,
            "human_editorial_qa_completed": False,
        }
        cue_number = item.get("cue_number")
        if kind in {"fast_subtitle", "wide_subtitle"}:
            if not isinstance(cue_number, int) or not 1 <= cue_number <= 400:
                raise ValueError("LOCAL_EDIT_REVIEW_CUE_UNTRUSTED")
            candidate["cue_number"] = cue_number
        results.append(candidate)
    return results


def store_review_ledger(
    *, mp4_output: Path, video_sha256: str, final_sha256: str,
    diagnostic_status: str, timeline: ClipTimeline,
    observed: list[dict[str, object]],
) -> dict[str, object]:
    """Create-only SHA-bound diagnostic ledger; failure never alters base QA."""
    result: dict[str, object] = {
        "status": "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE",
        "sha256": "",
        "candidate_count": 0,
        "ready": False,
        "production_ready": False,
    }
    if not diagnostic_status.startswith("LOCAL_EDIT_DIAGNOSTIC_MEASURED_"):
        return result
    try:
        video = _no_links(mp4_output)
        target = _no_links(video.with_name(f"{video.stem}.review.json"))
        if (not SHA.fullmatch(video_sha256)
                or not SHA.fullmatch(final_sha256)
                or video.suffix.lower() != ".mp4"
                or not video.is_file() or target.exists()
                or not video.parent.is_dir()
                or _hash(video)[0] != video_sha256):
            raise ValueError("LOCAL_EDIT_REVIEW_MEDIA_UNTRUSTED")
        candidates = candidates_for_review(timeline, observed)
        ledger = {
            "version": REVIEW_VERSION,
            "diagnostic_status": diagnostic_status,
            "base_sha256": video_sha256,
            "final_sha256": final_sha256,
            "source_of_observations": "OFFLINE_FFMPEG_AND_HASH_VERIFIED_SRT",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "human_editorial_qa_completed": False,
            "ready": False,
            "production_ready": False,
            "publication_performed": False,
        }
        encoded = (json.dumps(
            ledger, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("LOCAL_EDIT_REVIEW_LEDGER_TOO_LARGE")
        with tempfile.TemporaryDirectory(
            prefix=".holoclip-review-", dir=video.parent,
        ) as work:
            staged = Path(work) / "review.json"
            staged.write_bytes(encoded)
            if _hash(video)[0] != video_sha256:
                raise ValueError("LOCAL_EDIT_REVIEW_MEDIA_CHANGED")
            try:
                os.link(staged, target)
            except OSError as exc:
                raise ValueError("LOCAL_EDIT_REVIEW_COMMIT_FAILED") from exc
        result.update({
            "status": "LOCAL_EDIT_REVIEW_LEDGER_CREATED_UNVERIFIED_EDITORIAL",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "candidate_count": len(candidates),
        })
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    return result
