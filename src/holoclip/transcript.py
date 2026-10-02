from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from holoclip.models import TranscriptChunk, TranscriptSegment


def load_transcript(path: Path) -> list[TranscriptSegment]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("segments")
    if not isinstance(raw, list):
        raise ValueError("transcript JSON must be a list or an object with a segments list")
    return [TranscriptSegment.model_validate(item) for item in raw]


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def chunk_segments(
    segments: list[TranscriptSegment],
    *,
    window_seconds: float = 600.0,
    overlap_seconds: float = 120.0,
) -> list[TranscriptChunk]:
    if not segments:
        return []
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("overlap_seconds must be >= 0 and smaller than window_seconds")

    ordered = sorted(segments, key=lambda item: (item.start, item.end))
    final_end = max(item.end for item in ordered)
    cursor = ordered[0].start
    step = window_seconds - overlap_seconds
    chunks: list[TranscriptChunk] = []
    chunk_index = 0

    while cursor < final_end:
        boundary = cursor + window_seconds
        indices = [
            index
            for index, segment in enumerate(ordered)
            if segment.end > cursor and segment.start < boundary
        ]

        if indices:
            selected = [ordered[index] for index in indices]
            lines = []
            for segment in selected:
                speaker = f"{segment.speaker}: " if segment.speaker else ""
                lines.append(f"[{_timestamp(segment.start)}] {speaker}{segment.text}")
            chunks.append(
                TranscriptChunk(
                    id=f"chunk-{chunk_index:04d}",
                    start=min(item.start for item in selected),
                    end=max(item.end for item in selected),
                    text="\n".join(lines),
                    segment_indices=indices,
                )
            )
            chunk_index += 1

        cursor += step

    return chunks
