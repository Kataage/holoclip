"""Read-only, bounded transcript snippets for inspection-only source windows.

Trusted same-source interval alignment is approximate, and local ASR is neither
verified speech truth nor a creative editing label. No downloads or inference.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from holoclip.models import TranscriptSegment
from holoclip.render_human_edit_context import _read_bounded
from holoclip.render_plan import _no_links

_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_BYTES = 8 * 1024 * 1024
_MAX_SEGMENTS = 20000


def _read_transcript(root: Path, stream: str) -> tuple[list[TranscriptSegment], str, str] | None:
    if not _ID.fullmatch(stream):
        raise ValueError("LOCAL_INSPECTION_SOURCE_ID_INVALID")
    candidates = (
        root / f"{stream}.json",
        root / "sources" / f"{stream}.json",
        root / "examples" / "sources" / f"{stream}.json",
    )
    matching = [p for p in (_no_links(q) for q in candidates) if p.exists()]
    if not matching:
        return None
    if len(matching) != 1 or not matching[0].is_file():
        raise ValueError("LOCAL_INSPECTION_TRANSCRIPT_AMBIGUOUS")
    raw = _read_bounded(matching[0], _MAX_BYTES)
    if not raw:
        raise ValueError("LOCAL_INSPECTION_TRANSCRIPT_EMPTY")
    payload = json.loads(raw)
    identity_basis = "FILENAME_ONLY_IDENTITY_UNVERIFIED"
    if isinstance(payload, dict):
        ids = [
            payload[name] for name in
            ("stream_id", "source_id", "video_id", "external_id")
            if name in payload
        ]
        if ids:
            if any(identifier != stream for identifier in ids):
                raise ValueError("LOCAL_INSPECTION_TRANSCRIPT_ID_MISMATCH")
            identity_basis = "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED"
        payload = payload.get("segments")
    if not isinstance(payload, list) or len(payload) > _MAX_SEGMENTS:
        raise ValueError("LOCAL_INSPECTION_TRANSCRIPT_STRUCTURE_INVALID")
    segments = [TranscriptSegment.model_validate(item) for item in payload]
    previous = -1.0
    for item in segments:
        if (not math.isfinite(item.start) or not math.isfinite(item.end)
                or not 0 <= item.start < item.end <= 86400
                or item.start < previous):
            raise ValueError("LOCAL_INSPECTION_TRANSCRIPT_CLOCK_INVALID")
        previous = item.start
    return segments, hashlib.sha256(raw).hexdigest(), identity_basis


def _utterances(
    segments: list[TranscriptSegment], start: float, end: float,
) -> list[dict]:
    selected = []
    for segment in segments:
        if segment.end <= start:
            continue
        if segment.start >= end:
            break
        text = " ".join(segment.text.split())
        if (not text or len(text) > 160
                or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text)):
            continue
        selected.append({
            "start_seconds": round(segment.start, 3),
            "end_seconds": round(segment.end, 3),
            "unverified_asr_text": text,
        })
        if len(selected) == 3:
            break
    return selected


def _context_window(segments: list[TranscriptSegment], selected: dict) -> dict:
    start = selected["source_start_seconds"]
    end = selected["source_end_seconds"]
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end)
            or not 0 <= start < end <= 86400):
        raise ValueError("LOCAL_INSPECTION_WINDOW_INVALID")
    middle = (start + end) / 2
    regions = {
        "preceding": (max(0.0, start - 8.0), start),
        "within": (max(start, middle - 4.0), min(end, middle + 4.0)),
        "following": (end, min(86400.0, end + 8.0)),
    }
    return {
        "source_start_seconds": start,
        "source_end_seconds": end,
        "asr": {
            key: _utterances(segments, lo, hi) if lo < hi else []
            for key, (lo, hi) in regions.items()
        },
        "human_edit_intent_inferred": False,
    }


def attach_inspection_context(
    inspection: dict, *, transcript_root: Path,
    stream_by_job: dict[str, str],
) -> None:
    """Attach context only AFTER every same-source and holdout safety gate."""
    root = _no_links(transcript_root)
    cache: dict[str, tuple[list[TranscriptSegment], str, str] | None] = {}
    invalid: set[str] = set()
    for item in inspection["results"]:
        stream = stream_by_job.get(item["job_id"])
        if stream is None:
            item["transcript_context"] = {"status": "UNVERIFIED_SOURCE"}
            continue
        if stream not in invalid and stream not in cache:
            try:
                cache[stream] = _read_transcript(root, stream)
            except (OSError, ValueError, TypeError, UnicodeError, KeyError):
                invalid.add(stream)
        if stream in invalid:
            item["transcript_context"] = {"status": "TRANSCRIPT_UNTRUSTED"}
            continue
        parsed = cache[stream]
        if parsed is None:
            item["transcript_context"] = {"status": "TRANSCRIPT_UNAVAILABLE"}
            continue
        segments, digest, basis = parsed
        try:
            windows = [
                {"selection_side": side, **_context_window(segments, sample)}
                for side, key in (
                    ("generated_only", "generated_only_top_source_windows"),
                    ("human_only", "human_only_top_source_windows"),
                )
                for sample in item[key][:1]
            ]
        except (ValueError, TypeError, KeyError):
            item["transcript_context"] = {"status": "TRANSCRIPT_WINDOW_INVALID"}
            continue
        item["transcript_context"] = {
            "status": "TRANSCRIPT_CONTEXT_AVAILABLE" if windows
            else "NO_SOURCE_WINDOW_TO_SAMPLE",
            "transcript_sha256": digest,
            "source_identity_basis": basis,
            "windows": windows,
            "local_asr_audio_independently_verified": False,
            "creative_quality_label": None,
        }
