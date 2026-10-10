"""Cut-aligned Japanese SRT sidecars from existing, traceable local ASR data.

SRT is an *optional* timed transcript companion to an already QA-checked
technical MP4, not burned into video and not evidence of transcript accuracy.
No source audio, remote transcription, network or LLM access occurs here.
Never fabricate word timing for partial utterances across edit boundaries.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from holoclip.domain import ClipTimeline
from holoclip.models import TranscriptSegment
from holoclip.render_plan import LocalRenderSource, _no_links

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SEGMENTS = 20000
_MAX_CUES = 400
_MAX_SECONDS = 1800
_SUPPORTED_META = {"stream_id", "source_id", "video_id", "external_id"}


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


def _load_source_transcript(root: Path, external_id: str) -> list[TranscriptSegment] | None:
    if not _VIDEO_ID.fullmatch(external_id):
        raise ValueError("LOCAL_SUBTITLE_SOURCE_ID_UNTRUSTED")
    paths = (
        root / f"{external_id}.json",
        root / "examples" / "sources" / f"{external_id}.json",
    )
    matches = []
    for path in paths:
        path = _no_links(path)
        if not path.exists():
            continue
        if not path.is_file() or not 1 <= path.stat().st_size <= _MAX_FILE_BYTES:
            raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_UNTRUSTED")
        matches.append(path)
    if len(matches) > 1:
        raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_AMBIGUOUS")
    if not matches:
        return None
    try:
        payload = json.loads(matches[0].read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for key in _SUPPORTED_META:
                if key in payload and payload[key] != external_id:
                    raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_ID_MISMATCH")
            payload = payload.get("segments")
        if not isinstance(payload, list) or len(payload) > _MAX_SEGMENTS:
            raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_UNTRUSTED")
        segments = [TranscriptSegment.model_validate(item) for item in payload]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_UNTRUSTED") from exc
    prior = -1.0
    for item in segments:
        if (not math.isfinite(item.start) or not math.isfinite(item.end)
                or item.start < prior or item.end > 86400):
            raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_TIMING_UNTRUSTED")
        prior = item.start
    return segments


def _caption_text(raw: str) -> str | None:
    # No rich text/HTML markup from untrusted ASR; preserve Japanese text.
    normalized = " ".join(raw.split())
    if (not normalized or len(normalized) > 120
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                   for char in normalized)):
        return None
    return html.escape(normalized, quote=False)


def _timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    if milliseconds < 0 or milliseconds > 99 * 3600 * 1000:
        raise ValueError("LOCAL_SUBTITLE_TIME_UNTRUSTED")
    hours, rem = divmod(milliseconds, 3600000)
    minutes, rem = divmod(rem, 60000)
    sec, milli = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{sec:02d},{milli:03d}"


def compose_cut_aligned_srt(
    timeline: ClipTimeline,
    sources: list[LocalRenderSource],
    *, transcript_root: Path,
) -> tuple[str, int, int]:
    """Only fully contained utterances get time-shifted; no word-time guesses.

    Return SRT bytes-as-string, kept cue count and dropped-boundary cue count.
    All edited source IDs must have an unambiguous local transcript, or nothing
    is exported to avoid false partial coverage for multi-source clips.
    """
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    root = _no_links(transcript_root)
    if not root.is_dir():
        raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_ROOT_UNAVAILABLE")
    ids = {segment.source_asset_id for segment in timeline.segments}
    mapping = {}
    for source in sources:
        if source.asset.id not in ids or source.asset.id in mapping:
            raise ValueError("LOCAL_SUBTITLE_SOURCE_MAP_UNTRUSTED")
        mapping[source.asset.id] = source.asset.external_id
    if ids != set(mapping) or len(ids) > 32 or not 0 < timeline.estimated_duration <= _MAX_SECONDS:
        raise ValueError("LOCAL_SUBTITLE_SOURCE_MAP_UNTRUSTED")
    transcripts: dict[str, list[TranscriptSegment]] = {}
    for asset_id, external_id in mapping.items():
        segments = _load_source_transcript(root, external_id)
        if segments is None:
            raise ValueError("LOCAL_SUBTITLE_TRANSCRIPT_NOT_AVAILABLE")
        transcripts[asset_id] = segments
    output: list[SubtitleCue] = []
    dropped = 0
    position = 0.0
    sorted_cuts = sorted(timeline.segments, key=lambda item: item.output_order)
    for cut in sorted_cuts:
        for item in transcripts[cut.source_asset_id]:
            if item.end <= cut.source_start or item.start >= cut.source_end:
                continue
            if item.start < cut.source_start - 0.001 or item.end > cut.source_end + 0.001:
                dropped += 1
                continue
            duration = item.end - item.start
            text = _caption_text(item.text)
            if text is None or not 0.25 <= duration <= 8.0:
                dropped += 1
                continue
            start = position + max(0, item.start - cut.source_start)
            end = position + min(cut.source_end - cut.source_start,
                                 item.end - cut.source_start)
            if (not math.isfinite(start) or not math.isfinite(end)
                    or end <= start or end > _MAX_SECONDS + 0.001):
                raise ValueError("LOCAL_SUBTITLE_TIME_UNTRUSTED")
            # Overlapping ASR utterances cannot be correctly composed into a
            # single-screen subtitle without speaker/layer attribution.
            if output and start < output[-1].end - 0.001:
                dropped += 1
                continue
            if len(output) >= _MAX_CUES:
                raise ValueError("LOCAL_SUBTITLE_CUE_LIMIT")
            output.append(SubtitleCue(start, end, text))
        position += cut.source_end - cut.source_start
    if abs(position - timeline.estimated_duration) > 0.05:
        raise ValueError("LOCAL_SUBTITLE_TIMELINE_DURATION_MISMATCH")
    blocks = [
        f"{n}\n{_timecode(c.start)} --> {_timecode(c.end)}\n{c.text}"
        for n, c in enumerate(output, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else ""), len(output), dropped


def export_cut_aligned_srt(
    timeline: ClipTimeline,
    sources: list[LocalRenderSource],
    *, transcript_root: Path,
    mp4_output: Path,
) -> dict[str, object]:
    """Idempotent *create-only* optional subtitle companion for local preview."""
    base = {
        "status": "LOCAL_SUBTITLE_NOT_AVAILABLE",
        "cue_count": 0,
        "dropped_cue_count": 0,
        "output_sha256": None,
        "ready": False,
        "production_ready": False,
    }
    try:
        video = _no_links(mp4_output)
        target = _no_links(video.with_suffix(".srt"))
        if video.suffix.lower() != ".mp4" or not video.is_file():
            raise ValueError("LOCAL_SUBTITLE_VIDEO_NOT_READY")
        if target.exists():
            raise ValueError("LOCAL_SUBTITLE_OUTPUT_EXISTS_UNVERIFIED")
        srt, count, dropped = compose_cut_aligned_srt(
            timeline, sources, transcript_root=transcript_root,
        )
        base["dropped_cue_count"] = dropped
        if count == 0:
            base["status"] = "LOCAL_SUBTITLE_NO_FULL_UTTERANCES"
            return base
        data = srt.encode("utf-8")
        if len(data) > 131072:
            raise ValueError("LOCAL_SUBTITLE_OUTPUT_TOO_LARGE")
        with tempfile.TemporaryDirectory(prefix=".holoclip-captions-", dir=video.parent) as tmp:
            temp = Path(tmp) / "captions.srt"
            temp.write_bytes(data)
            # create-only, does not clobber any existing operator subtitle
            os.link(temp, target)
        base["status"] = "LOCAL_SUBTITLE_SRT_CREATED_UNVERIFIED_ASR"
        base["cue_count"] = count
        base["output_sha256"] = hashlib.sha256(data).hexdigest()
    except (ValueError, OSError, UnicodeError, TypeError):
        # These are optional ASR captions and must not corrupt the already
        # measured media QA evidence. Never log transcript text or local paths.
        base["status"] = "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    return base
