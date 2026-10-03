from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from holoclip.alignment import ALIGNMENT_METHOD, WEAK_SINGLETON_MAX_CONFIDENCE
from holoclip.examples import HumanClipExample
from holoclip.structure_validation import (
    StructureWarningCode,
    validate_editorial_structure,
)


REJECT_ALIGNMENT_CONFIDENCE = 0.55
TRUSTED_MIN_ALIGNMENT_CONFIDENCE = 0.75
TRUSTED_MIN_CLIP_COVERAGE = 0.70
MIN_LARGE_CLIP_GAP_SECONDS = 30.0
LARGE_CLIP_GAP_RATIO = 0.20
SOURCE_HINT_REVIEW_DELTA_SECONDS = 600.0
EXPLAINED_BRIDGE_MAX_CLIP_GAP_SECONDS = 90.0
EXPLAINED_BRIDGE_MAX_SOURCE_CUT_SECONDS = 45.0
EXPLAINED_BRIDGE_CLOCK_TOLERANCE_SECONDS = 3.0


class LearningQuality(StrEnum):
    TRUSTED = "trusted"
    REVIEW = "review"
    REJECTED = "rejected"


class LearningQualityReason(StrEnum):
    NO_ALIGNMENT = "no_alignment"
    MISSING_ALIGNMENT_SEGMENTS = "missing_alignment_segments"
    MISSING_ALIGNMENT_CONFIDENCE = "missing_alignment_confidence"
    LOW_ALIGNMENT_CONFIDENCE = "low_alignment_confidence"
    MARGINAL_ALIGNMENT_CONFIDENCE = "marginal_alignment_confidence"
    LEGACY_ALIGNMENT_METHOD = "legacy_alignment_method"
    LOW_CLIP_COVERAGE = "low_clip_coverage"
    LARGE_UNCOVERED_CLIP_GAP = "large_uncovered_clip_gap"
    WEAK_SINGLETON_ALIGNMENT = "weak_singleton_alignment"
    NON_MONOTONIC_ALIGNMENT = "non_monotonic_alignment"
    SOURCE_HINT_DISAGREEMENT = "source_hint_disagreement"
    STRUCTURE_INCOMPLETE = "structure_incomplete"
    STRUCTURE_TRAIT_CONTRADICTION = "structure_trait_contradiction"


class AlignmentQualityMetrics(BaseModel):
    clip_timeline_start: float = 0.0
    clip_timeline_end: float = 0.0
    clip_timeline_seconds: float = 0.0
    aligned_clip_seconds: float = 0.0
    clip_coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    uncovered_clip_seconds: float = 0.0
    largest_uncovered_clip_gap: float = 0.0
    bridged_clip_seconds: float = 0.0
    explained_clip_seconds: float = 0.0
    explained_clip_coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    explained_uncovered_clip_seconds: float = 0.0
    explained_bridge_count: int = 0

    aligned_source_seconds: float = 0.0
    source_bounding_seconds: float = 0.0
    source_gap_seconds: float = 0.0

    matched_anchors: int = 0
    total_anchors: int | None = None
    anchor_coverage_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    weak_singleton_count: int = 0
    non_monotonic_transition_count: int = 0
    largest_backward_jump_seconds: float = 0.0


class HumanExampleLearningQuality(BaseModel):
    decision: LearningQuality
    reasons: list[LearningQualityReason] = Field(default_factory=list)
    metrics: AlignmentQualityMetrics
    source_hint_delta_seconds: float | None = None


def parse_youtube_time_hint(url: str | None) -> float | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    values = parse_qs(parsed.query).get("t", [])
    if not values and parsed.fragment:
        values = parse_qs(parsed.fragment).get("t", [])
    if not values:
        return None

    raw = values[0].strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)

    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", raw):
        matched = True
        amount = float(value)
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        else:
            total += amount
    return total if matched else None


def _union_seconds(ranges: list[tuple[float, float]]) -> float:
    if not ranges:
        return 0.0
    ordered = sorted(
        (start, end)
        for start, end in ranges
        if end > start
    )
    if not ordered:
        return 0.0

    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    total += current_end - current_start
    return total


def _largest_uncovered_gap(
    ranges: list[tuple[float, float]],
    *,
    timeline_start: float,
    timeline_end: float,
) -> float:
    if timeline_end <= timeline_start:
        return 0.0

    clipped = sorted(
        (
            max(timeline_start, start),
            min(timeline_end, end),
        )
        for start, end in ranges
        if end > timeline_start and start < timeline_end and end > start
    )
    if not clipped:
        return timeline_end - timeline_start

    merged: list[tuple[float, float]] = []
    for start, end in clipped:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    largest = max(0.0, merged[0][0] - timeline_start)
    for left, right in zip(merged, merged[1:], strict=False):
        largest = max(largest, right[0] - left[1])
    largest = max(largest, timeline_end - merged[-1][1])
    return largest


def compute_alignment_quality_metrics(
    example: HumanClipExample,
) -> AlignmentQualityMetrics:
    segments = example.alignment_segments
    if not segments:
        source_bounding = (
            example.source_end - example.source_start
            if example.source_start is not None and example.source_end is not None
            else 0.0
        )
        return AlignmentQualityMetrics(
            source_bounding_seconds=max(0.0, source_bounding),
            matched_anchors=example.alignment_matched_anchors or 0,
            total_anchors=example.alignment_total_anchors,
        )

    clip_ranges = [(item.clip_start, item.clip_end) for item in segments]
    source_ranges = [(item.source_start, item.source_end) for item in segments]

    clip_timeline_start = min(start for start, _ in clip_ranges)
    clip_timeline_end = max(end for _, end in clip_ranges)
    clip_timeline_seconds = max(0.0, clip_timeline_end - clip_timeline_start)
    aligned_clip_seconds = _union_seconds(clip_ranges)
    clip_coverage_ratio = (
        min(1.0, aligned_clip_seconds / clip_timeline_seconds)
        if clip_timeline_seconds > 0
        else 0.0
    )
    uncovered_clip_seconds = max(0.0, clip_timeline_seconds - aligned_clip_seconds)
    largest_uncovered_clip_gap = _largest_uncovered_gap(
        clip_ranges,
        timeline_start=clip_timeline_start,
        timeline_end=clip_timeline_end,
    )

    aligned_source_seconds = _union_seconds(source_ranges)
    source_start = (
        example.source_start
        if example.source_start is not None
        else min(start for start, _ in source_ranges)
    )
    source_end = (
        example.source_end
        if example.source_end is not None
        else max(end for _, end in source_ranges)
    )
    source_bounding_seconds = max(0.0, source_end - source_start)
    source_gap_seconds = max(0.0, source_bounding_seconds - aligned_source_seconds)

    weak_singleton_count = sum(
        1
        for item in segments
        if item.matched_anchors == 1
        and item.confidence < WEAK_SINGLETON_MAX_CONFIDENCE
    )
    non_monotonic_transition_count = 0
    largest_backward_jump_seconds = 0.0
    previous_source_end: float | None = None
    for item in segments:
        if previous_source_end is not None:
            delta = item.source_start - previous_source_end
            if delta < -3.0:
                non_monotonic_transition_count += 1
                largest_backward_jump_seconds = max(
                    largest_backward_jump_seconds,
                    abs(delta),
                )
        previous_source_end = item.source_end

    matched_anchors = (
        example.alignment_matched_anchors
        if example.alignment_matched_anchors is not None
        else sum(item.matched_anchors for item in segments)
    )
    total_anchors = example.alignment_total_anchors
    anchor_coverage_ratio = (
        min(1.0, matched_anchors / total_anchors)
        if total_anchors is not None and total_anchors > 0
        else None
    )

    return AlignmentQualityMetrics(
        clip_timeline_start=clip_timeline_start,
        clip_timeline_end=clip_timeline_end,
        clip_timeline_seconds=clip_timeline_seconds,
        aligned_clip_seconds=aligned_clip_seconds,
        clip_coverage_ratio=clip_coverage_ratio,
        uncovered_clip_seconds=uncovered_clip_seconds,
        largest_uncovered_clip_gap=largest_uncovered_clip_gap,
        aligned_source_seconds=aligned_source_seconds,
        source_bounding_seconds=source_bounding_seconds,
        source_gap_seconds=source_gap_seconds,
        matched_anchors=matched_anchors,
        total_anchors=total_anchors,
        anchor_coverage_ratio=anchor_coverage_ratio,
        weak_singleton_count=weak_singleton_count,
        non_monotonic_transition_count=non_monotonic_transition_count,
        largest_backward_jump_seconds=largest_backward_jump_seconds,
    )


def _source_hint_delta(example: HumanClipExample) -> float | None:
    provenance = example.acquisition_provenance
    raw_url = (
        provenance.get("resolved_source_url")
        or provenance.get("requested_source_url")
        or example.source_url
    )
    hint = parse_youtube_time_hint(str(raw_url) if raw_url else None)
    if hint is None or not example.alignment_segments:
        return None
    return example.alignment_segments[0].source_start - hint


def evaluate_human_example_learning_quality(
    example: HumanClipExample,
) -> HumanExampleLearningQuality:
    metrics = compute_alignment_quality_metrics(example)
    reasons: list[LearningQualityReason] = []

    if example.source_start is None or example.source_end is None:
        return HumanExampleLearningQuality(
            decision=LearningQuality.REJECTED,
            reasons=[LearningQualityReason.NO_ALIGNMENT],
            metrics=metrics,
        )

    if not example.alignment_segments:
        reasons.append(LearningQualityReason.MISSING_ALIGNMENT_SEGMENTS)

    confidence = example.alignment_confidence
    if confidence is None:
        reasons.append(LearningQualityReason.MISSING_ALIGNMENT_CONFIDENCE)
    elif confidence < REJECT_ALIGNMENT_CONFIDENCE:
        reasons.append(LearningQualityReason.LOW_ALIGNMENT_CONFIDENCE)
    elif confidence < TRUSTED_MIN_ALIGNMENT_CONFIDENCE:
        reasons.append(LearningQualityReason.MARGINAL_ALIGNMENT_CONFIDENCE)

    if example.alignment_method != ALIGNMENT_METHOD:
        reasons.append(LearningQualityReason.LEGACY_ALIGNMENT_METHOD)

    if metrics.clip_timeline_seconds > 0:
        if metrics.clip_coverage_ratio < TRUSTED_MIN_CLIP_COVERAGE:
            reasons.append(LearningQualityReason.LOW_CLIP_COVERAGE)
        gap_threshold = max(
            MIN_LARGE_CLIP_GAP_SECONDS,
            metrics.clip_timeline_seconds * LARGE_CLIP_GAP_RATIO,
        )
        if metrics.largest_uncovered_clip_gap > gap_threshold:
            reasons.append(LearningQualityReason.LARGE_UNCOVERED_CLIP_GAP)

    if metrics.weak_singleton_count:
        reasons.append(LearningQualityReason.WEAK_SINGLETON_ALIGNMENT)
    if metrics.non_monotonic_transition_count:
        reasons.append(LearningQualityReason.NON_MONOTONIC_ALIGNMENT)

    source_hint_delta = _source_hint_delta(example)
    if (
        source_hint_delta is not None
        and abs(source_hint_delta) > SOURCE_HINT_REVIEW_DELTA_SECONDS
    ):
        reasons.append(LearningQualityReason.SOURCE_HINT_DISAGREEMENT)

    structure = validate_editorial_structure(example)
    if any(
        warning.code is StructureWarningCode.INCOMPLETE
        for warning in structure.warnings
    ):
        reasons.append(LearningQualityReason.STRUCTURE_INCOMPLETE)
    if any(
        warning.code is StructureWarningCode.UNRESOLVED_SETUP_PAYOFF
        for warning in structure.warnings
    ):
        reasons.append(LearningQualityReason.STRUCTURE_TRAIT_CONTRADICTION)

    if LearningQualityReason.LOW_ALIGNMENT_CONFIDENCE in reasons:
        decision = LearningQuality.REJECTED
    elif reasons:
        decision = LearningQuality.REVIEW
    else:
        decision = LearningQuality.TRUSTED

    return HumanExampleLearningQuality(
        decision=decision,
        reasons=reasons,
        metrics=metrics,
        source_hint_delta_seconds=source_hint_delta,
    )
