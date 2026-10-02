from __future__ import annotations

import math
import statistics
import unicodedata
from collections import defaultdict

from pydantic import BaseModel, Field

from holoclip.models import TranscriptSegment


class AlignmentAnchor(BaseModel):
    clip_start: float
    source_start: float
    similarity: float = Field(ge=0, le=1)
    clip_text: str
    source_text: str


class ClipSourceAlignment(BaseModel):
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    matched_anchors: int = Field(ge=0)
    total_anchors: int = Field(ge=0)
    median_offset: float
    anchors: list[AlignmentAnchor]


def normalize_match_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower()
    kept: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char.isspace() or category.startswith(("P", "Z", "S")):
            continue
        kept.append(char)
    return "".join(kept)


def _ngrams(text: str, size: int = 3) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _similarity(left: str, right: str) -> float:
    left_norm = normalize_match_text(left)
    right_norm = normalize_match_text(right)
    if not left_norm or not right_norm:
        return 0.0

    left_grams = _ngrams(left_norm)
    right_grams = _ngrams(right_norm)
    if not left_grams or not right_grams:
        return 0.0

    intersection = len(left_grams & right_grams)
    dice = (2.0 * intersection) / (len(left_grams) + len(right_grams))

    shorter = min(len(left_norm), len(right_norm))
    if shorter <= 0:
        return 0.0
    containment = intersection / min(len(left_grams), len(right_grams))

    return max(0.0, min(1.0, (0.65 * dice) + (0.35 * containment)))


def _source_variants(
    source: list[TranscriptSegment],
    index: int,
) -> list[tuple[float, str]]:
    current = source[index]
    variants = [(current.start, current.text)]
    if index + 1 < len(source):
        following = source[index + 1]
        if following.start - current.end <= 4.0:
            variants.append((current.start, f"{current.text}{following.text}"))
    if index + 2 < len(source):
        second = source[index + 1]
        third = source[index + 2]
        if second.start - current.end <= 4.0 and third.start - second.end <= 4.0:
            variants.append(
                (
                    current.start,
                    f"{current.text}{second.text}{third.text}",
                )
            )
    return variants


def align_clip_to_source(
    clip_segments: list[TranscriptSegment],
    source_segments: list[TranscriptSegment],
    *,
    max_anchors: int = 16,
    top_matches_per_anchor: int = 3,
    min_anchor_similarity: float = 0.28,
    offset_bucket_seconds: float = 20.0,
    offset_tolerance_seconds: float = 35.0,
) -> ClipSourceAlignment:
    if not clip_segments:
        raise ValueError("clip transcript has no segments")
    if not source_segments:
        raise ValueError("source transcript has no segments")

    clip = sorted(clip_segments, key=lambda item: (item.start, item.end))
    source = sorted(source_segments, key=lambda item: (item.start, item.end))

    candidates = [
        segment
        for segment in clip
        if len(normalize_match_text(segment.text)) >= 8
    ]
    if not candidates:
        candidates = clip

    anchors = sorted(
        candidates,
        key=lambda item: len(normalize_match_text(item.text)),
        reverse=True,
    )[:max_anchors]
    anchors = sorted(anchors, key=lambda item: item.start)

    match_rows: list[tuple[TranscriptSegment, float, str, float]] = []
    for clip_segment in anchors:
        scored: list[tuple[float, float, str]] = []
        for index in range(len(source)):
            for source_start, source_text in _source_variants(source, index):
                score = _similarity(clip_segment.text, source_text)
                if score >= min_anchor_similarity:
                    scored.append((score, source_start, source_text))

        scored.sort(key=lambda item: item[0], reverse=True)
        for score, source_start, source_text in scored[:top_matches_per_anchor]:
            match_rows.append((clip_segment, source_start, source_text, score))

    if not match_rows:
        raise ValueError("no plausible transcript anchors found")

    buckets: dict[int, float] = defaultdict(float)
    for clip_segment, source_start, _source_text, score in match_rows:
        offset = source_start - clip_segment.start
        bucket = int(round(offset / offset_bucket_seconds))
        weight = score * max(1.0, math.sqrt(len(normalize_match_text(clip_segment.text))))
        buckets[bucket] += weight

    best_bucket = max(buckets, key=buckets.get)
    bucket_center = best_bucket * offset_bucket_seconds

    best_by_clip_start: dict[float, tuple[TranscriptSegment, float, str, float]] = {}
    for row in match_rows:
        clip_segment, source_start, source_text, score = row
        offset = source_start - clip_segment.start
        if abs(offset - bucket_center) > offset_tolerance_seconds:
            continue
        previous = best_by_clip_start.get(clip_segment.start)
        if previous is None or score > previous[3]:
            best_by_clip_start[clip_segment.start] = row

    selected = sorted(best_by_clip_start.values(), key=lambda row: row[0].start)
    if not selected:
        raise ValueError("no transcript anchors agreed on a common source offset")

    offsets = [source_start - clip_segment.start for clip_segment, source_start, _, _ in selected]
    median_offset = statistics.median(offsets)

    clip_start = clip[0].start
    clip_end = clip[-1].end
    source_start = max(0.0, clip_start + median_offset)
    source_end = max(source_start + 0.001, clip_end + median_offset)

    average_similarity = statistics.fmean(row[3] for row in selected)
    coverage = min(1.0, len(selected) / max(3, min(len(anchors), 8)))
    offset_spread = (
        statistics.pstdev(offsets) if len(offsets) >= 2 else 0.0
    )
    consistency = max(0.0, 1.0 - min(1.0, offset_spread / offset_tolerance_seconds))
    confidence = max(
        0.0,
        min(
            1.0,
            (0.55 * average_similarity) + (0.25 * coverage) + (0.20 * consistency),
        ),
    )

    return ClipSourceAlignment(
        source_start=source_start,
        source_end=source_end,
        confidence=confidence,
        matched_anchors=len(selected),
        total_anchors=len(anchors),
        median_offset=median_offset,
        anchors=[
            AlignmentAnchor(
                clip_start=clip_segment.start,
                source_start=source_anchor_start,
                similarity=score,
                clip_text=clip_segment.text,
                source_text=source_text,
            )
            for clip_segment, source_anchor_start, source_text, score in selected
        ],
    )
