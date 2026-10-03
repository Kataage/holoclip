from __future__ import annotations

import math
import statistics
import unicodedata

from pydantic import BaseModel, Field

from holoclip.models import TranscriptSegment


ALIGNMENT_METHOD = "transcript-anchor-v8"
WEAK_SINGLETON_MAX_CONFIDENCE = 0.70
DEFAULT_MAX_ANCHORS = 36
MAX_ADAPTIVE_ANCHORS = 72
TARGET_ANCHOR_INTERVAL_SECONDS = 15.0
ENDPOINT_EXTENSION_MAX_SECONDS = 5.0
GENERIC_BOILERPLATE_PHRASES = (
    "ご視聴ありがとうございました",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録と高評価よろしくお願いします",
    "高評価よろしくお願いします",
    "thanks for watching",
    "please subscribe",
)
BACKWARD_JUMP_BASE_PENALTY = 0.75
BACKWARD_JUMP_MAX_EXTRA_PENALTY = 0.75


class AlignmentAnchor(BaseModel):
    clip_start: float
    source_start: float
    similarity: float = Field(ge=0, le=1)
    clip_text: str
    source_text: str


class AlignmentSegment(BaseModel):
    clip_start: float = Field(ge=0)
    clip_end: float = Field(gt=0)
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    matched_anchors: int = Field(ge=1)
    median_offset: float
    anchors: list[AlignmentAnchor]


class ClipSourceAlignment(BaseModel):
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    matched_anchors: int = Field(ge=0)
    total_anchors: int = Field(ge=0)
    median_offset: float
    anchors: list[AlignmentAnchor]
    segments: list[AlignmentSegment] = Field(default_factory=list)


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
    containment = intersection / min(len(left_grams), len(right_grams))
    return max(0.0, min(1.0, (0.65 * dice) + (0.35 * containment)))


def _is_generic_boilerplate(text: str) -> bool:
    normalized = normalize_match_text(text)
    if not normalized:
        return True
    for phrase in GENERIC_BOILERPLATE_PHRASES:
        phrase_norm = normalize_match_text(phrase)
        if normalized == phrase_norm:
            return True
        if phrase_norm in normalized and len(normalized) <= len(phrase_norm) + 6:
            return True
    return False


def _resolve_anchor_budget(
    clip: list[TranscriptSegment],
    *,
    max_anchors: int | None,
) -> int:
    if max_anchors is not None:
        if max_anchors <= 0:
            raise ValueError("max_anchors must be positive")
        return max_anchors

    if not clip:
        return DEFAULT_MAX_ANCHORS

    timeline_start = min(item.start for item in clip)
    timeline_end = max(item.end for item in clip)
    duration = max(0.0, timeline_end - timeline_start)
    density_budget = math.ceil(
        duration / TARGET_ANCHOR_INTERVAL_SECONDS
    )
    return min(
        MAX_ADAPTIVE_ANCHORS,
        max(DEFAULT_MAX_ANCHORS, density_budget),
    )


def _select_anchor_segments(
    clip: list[TranscriptSegment],
    *,
    max_anchors: int,
) -> list[TranscriptSegment]:
    candidates = [
        segment
        for segment in clip
        if len(normalize_match_text(segment.text)) >= 8
        and not _is_generic_boilerplate(segment.text)
    ]
    if not candidates:
        candidates = [
            segment
            for segment in clip
            if not _is_generic_boilerplate(segment.text)
        ]
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda item: (item.start, item.end))
    if len(ordered) <= max_anchors:
        return ordered

    timeline_start = ordered[0].start
    timeline_end = max(item.end for item in ordered)
    duration = max(0.001, timeline_end - timeline_start)
    bucket_count = min(max_anchors, len(ordered))
    buckets: list[list[TranscriptSegment]] = [
        [] for _ in range(bucket_count)
    ]

    for segment in ordered:
        midpoint = (segment.start + segment.end) / 2.0
        relative = max(0.0, min(1.0, (midpoint - timeline_start) / duration))
        bucket_index = min(
            bucket_count - 1,
            int(relative * bucket_count),
        )
        buckets[bucket_index].append(segment)

    selected: list[TranscriptSegment] = []
    selected_ids: set[int] = set()

    def ranking(segment: TranscriptSegment) -> tuple[int, float, float]:
        return (
            len(normalize_match_text(segment.text)),
            segment.end - segment.start,
            -segment.start,
        )

    for bucket in buckets:
        if not bucket:
            continue
        best = max(bucket, key=ranking)
        selected.append(best)
        selected_ids.add(id(best))

    if len(selected) < max_anchors:
        remaining = [
            segment for segment in ordered if id(segment) not in selected_ids
        ]
        remaining.sort(key=ranking, reverse=True)
        selected.extend(remaining[: max_anchors - len(selected)])

    return sorted(selected, key=lambda item: (item.start, item.end))


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
            variants.append((current.start, f"{current.text}{second.text}{third.text}"))
    return variants


def _candidate_matches(
    anchors: list[TranscriptSegment],
    source: list[TranscriptSegment],
    *,
    top_matches_per_anchor: int,
    min_anchor_similarity: float,
) -> list[tuple[TranscriptSegment, list[tuple[float, str, float]]]]:
    matched: list[tuple[TranscriptSegment, list[tuple[float, str, float]]]] = []
    for clip_segment in anchors:
        scored: list[tuple[float, str, float]] = []
        for index in range(len(source)):
            for source_start, source_text in _source_variants(source, index):
                score = _similarity(clip_segment.text, source_text)
                if score >= min_anchor_similarity:
                    scored.append((source_start, source_text, score))
        scored.sort(key=lambda item: item[2], reverse=True)
        if scored:
            matched.append((clip_segment, scored[:top_matches_per_anchor]))
    return matched


def _best_alignment_path(
    matched: list[tuple[TranscriptSegment, list[tuple[float, str, float]]]],
    *,
    same_offset_tolerance_seconds: float,
) -> list[tuple[TranscriptSegment, float, str, float]]:
    if not matched:
        return []

    scores: list[list[float]] = []
    previous_index: list[list[int | None]] = []

    for anchor_index, (clip_segment, candidates) in enumerate(matched):
        row_scores: list[float] = []
        row_prev: list[int | None] = []

        for candidate_index, (source_start, _source_text, similarity) in enumerate(candidates):
            if anchor_index == 0:
                row_scores.append(similarity)
                row_prev.append(None)
                continue

            best_score = float("-inf")
            best_previous: int | None = None
            previous_clip, previous_candidates = matched[anchor_index - 1]

            for prev_index, (prev_source_start, _prev_text, _prev_similarity) in enumerate(
                previous_candidates
            ):
                prev_score = scores[anchor_index - 1][prev_index]
                current_offset = source_start - clip_segment.start
                previous_offset = prev_source_start - previous_clip.start
                offset_delta = abs(current_offset - previous_offset)

                expected_source_delta = clip_segment.start - previous_clip.start
                actual_source_delta = source_start - prev_source_start

                if offset_delta <= same_offset_tolerance_seconds:
                    transition = 0.35
                    drift = abs(actual_source_delta - expected_source_delta)
                    transition -= min(0.15, drift / 120.0)
                elif actual_source_delta < 0:
                    # A single-source human clip normally preserves chronology.
                    # Penalize backward jumps strongly enough that a slightly
                    # higher lexical match cannot create an isolated false cut.
                    # A genuine reordered block can still recover because the
                    # first jump pays this cost once, while its following anchors
                    # regain the same-offset continuity bonus.
                    backward_seconds = abs(actual_source_delta)
                    transition = -BACKWARD_JUMP_BASE_PENALTY
                    transition -= min(
                        BACKWARD_JUMP_MAX_EXTRA_PENALTY,
                        backward_seconds / 1200.0,
                    )
                else:
                    transition = -0.12

                total = prev_score + similarity + transition
                if total > best_score:
                    best_score = total
                    best_previous = prev_index

            row_scores.append(best_score)
            row_prev.append(best_previous)

        scores.append(row_scores)
        previous_index.append(row_prev)

    last_index = max(range(len(scores[-1])), key=lambda index: scores[-1][index])
    path: list[tuple[TranscriptSegment, float, str, float]] = []

    for anchor_index in range(len(matched) - 1, -1, -1):
        clip_segment, candidates = matched[anchor_index]
        source_start, source_text, similarity = candidates[last_index]
        path.append((clip_segment, source_start, source_text, similarity))
        prev = previous_index[anchor_index][last_index]
        if prev is None:
            break
        last_index = prev

    path.reverse()
    return path


def _segment_path(
    path: list[tuple[TranscriptSegment, float, str, float]],
    clip: list[TranscriptSegment],
    source: list[TranscriptSegment],
    *,
    cut_offset_tolerance_seconds: float,
    endpoint_extension_max_seconds: float,
) -> list[AlignmentSegment]:
    if not path:
        return []

    groups: list[list[tuple[TranscriptSegment, float, str, float]]] = [[path[0]]]
    for row in path[1:]:
        previous = groups[-1][-1]
        previous_offset = previous[1] - previous[0].start
        current_offset = row[1] - row[0].start
        if abs(current_offset - previous_offset) <= cut_offset_tolerance_seconds:
            groups[-1].append(row)
        else:
            groups.append([row])

    source_duration = source[-1].end
    segments: list[AlignmentSegment] = []
    for index, group in enumerate(groups):
        offsets = [source_start - clip_segment.start for clip_segment, source_start, _, _ in group]
        median_offset = statistics.median(offsets)

        clip_start = group[0][0].start
        clip_end = group[-1][0].end
        if (
            index == 0
            and group[0][0].start - clip[0].start
            <= endpoint_extension_max_seconds
        ):
            clip_start = clip[0].start
        if (
            index == len(groups) - 1
            and clip[-1].end - group[-1][0].end
            <= endpoint_extension_max_seconds
        ):
            clip_end = clip[-1].end

        source_start = max(0.0, min(source_duration - 0.001, clip_start + median_offset))
        source_end = min(source_duration, clip_end + median_offset)
        source_end = max(source_start + 0.001, source_end)

        average_similarity = statistics.fmean(row[3] for row in group)
        spread = statistics.pstdev(offsets) if len(offsets) >= 2 else 0.0
        consistency = max(
            0.0,
            1.0 - min(1.0, spread / max(1.0, cut_offset_tolerance_seconds)),
        )
        anchor_strength = min(1.0, len(group) / 3.0)
        confidence = max(
            0.0,
            min(
                1.0,
                (0.65 * average_similarity) + (0.20 * consistency) + (0.15 * anchor_strength),
            ),
        )

        anchors = [
            AlignmentAnchor(
                clip_start=clip_segment.start,
                source_start=source_anchor_start,
                similarity=score,
                clip_text=clip_segment.text,
                source_text=source_text,
            )
            for clip_segment, source_anchor_start, source_text, score in group
        ]
        segments.append(
            AlignmentSegment(
                clip_start=clip_start,
                clip_end=clip_end,
                source_start=source_start,
                source_end=source_end,
                confidence=confidence,
                matched_anchors=len(group),
                median_offset=median_offset,
                anchors=anchors,
            )
        )
    return segments



def _filter_spurious_segments(
    segments: list[AlignmentSegment],
    *,
    isolated_max_confidence: float = WEAK_SINGLETON_MAX_CONFIDENCE,
    source_order_tolerance_seconds: float = 3.0,
) -> list[AlignmentSegment]:
    """Drop weak one-anchor segments that are clear source-order outliers.

    Human-made clips can intentionally reorder material, so this is deliberately
    conservative: only an interior singleton is removed, only when both neighboring
    segments form a coherent forward sequence and the singleton sits outside that
    sequence. Strong or multi-anchor segments are never removed by this heuristic.
    """
    if len(segments) < 3:
        return segments

    kept: list[AlignmentSegment] = []
    for index, segment in enumerate(segments):
        if index == 0 or index == len(segments) - 1:
            kept.append(segment)
            continue

        previous = segments[index - 1]
        following = segments[index + 1]
        weak_singleton = (
            segment.matched_anchors == 1
            and segment.confidence < isolated_max_confidence
        )
        neighbors_forward = (
            following.source_start + source_order_tolerance_seconds
            >= previous.source_end
        )
        before_previous = (
            segment.source_end + source_order_tolerance_seconds
            < previous.source_start
        )
        after_following = (
            segment.source_start
            > following.source_end + source_order_tolerance_seconds
        )

        def trustworthy_neighbor(item: AlignmentSegment) -> bool:
            return item.matched_anchors >= 2 or item.confidence >= 0.75

        def source_contains(
            outer: AlignmentSegment,
            inner: AlignmentSegment,
        ) -> bool:
            return (
                inner.source_start + source_order_tolerance_seconds
                >= outer.source_start
                and inner.source_end
                <= outer.source_end + source_order_tolerance_seconds
            )

        redundant_overlap = (
            trustworthy_neighbor(previous)
            and source_contains(previous, segment)
        ) or (
            trustworthy_neighbor(following)
            and source_contains(following, segment)
        )

        if weak_singleton and (
            redundant_overlap
            or (neighbors_forward and (before_previous or after_following))
        ):
            continue
        kept.append(segment)

    return kept


def _merge_consistent_segments(
    segments: list[AlignmentSegment],
    *,
    source_duration: float,
    cut_offset_tolerance_seconds: float,
) -> list[AlignmentSegment]:
    """Rejoin neighboring groups after an outlier segment has been removed.

    If two surviving segments have effectively the same clip->source offset,
    the apparent gap was created by segmentation noise rather than an editorial
    cut. Reconstruct one continuous segment so unanchored clip time is not lost.
    """
    if len(segments) < 2:
        return segments

    def merge(left: AlignmentSegment, right: AlignmentSegment) -> AlignmentSegment:
        anchors = sorted(
            [*left.anchors, *right.anchors],
            key=lambda item: (item.clip_start, item.source_start),
        )
        offsets = [anchor.source_start - anchor.clip_start for anchor in anchors]
        median_offset = statistics.median(offsets)
        clip_start = left.clip_start
        clip_end = right.clip_end
        source_start = max(
            0.0,
            min(source_duration - 0.001, clip_start + median_offset),
        )
        source_end = min(source_duration, clip_end + median_offset)
        source_end = max(source_start + 0.001, source_end)

        average_similarity = statistics.fmean(anchor.similarity for anchor in anchors)
        spread = statistics.pstdev(offsets) if len(offsets) >= 2 else 0.0
        consistency = max(
            0.0,
            1.0
            - min(
                1.0,
                spread / max(1.0, cut_offset_tolerance_seconds),
            ),
        )
        anchor_strength = min(1.0, len(anchors) / 3.0)
        confidence = max(
            0.0,
            min(
                1.0,
                (0.65 * average_similarity)
                + (0.20 * consistency)
                + (0.15 * anchor_strength),
            ),
        )
        return AlignmentSegment(
            clip_start=clip_start,
            clip_end=clip_end,
            source_start=source_start,
            source_end=source_end,
            confidence=confidence,
            matched_anchors=len(anchors),
            median_offset=median_offset,
            anchors=anchors,
        )

    merged: list[AlignmentSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        offset_delta = abs(segment.median_offset - previous.median_offset)
        if offset_delta <= cut_offset_tolerance_seconds:
            merged[-1] = merge(previous, segment)
        else:
            merged.append(segment)
    return merged

def align_clip_to_source(
    clip_segments: list[TranscriptSegment],
    source_segments: list[TranscriptSegment],
    *,
    max_anchors: int | None = None,
    top_matches_per_anchor: int = 8,
    min_anchor_similarity: float = 0.28,
    same_offset_tolerance_seconds: float = 22.0,
    cut_offset_tolerance_seconds: float = 12.0,
) -> ClipSourceAlignment:
    if not clip_segments:
        raise ValueError("clip transcript has no segments")
    if not source_segments:
        raise ValueError("source transcript has no segments")

    clip = sorted(clip_segments, key=lambda item: (item.start, item.end))
    source = sorted(source_segments, key=lambda item: (item.start, item.end))

    anchor_budget = _resolve_anchor_budget(
        clip,
        max_anchors=max_anchors,
    )
    anchors = _select_anchor_segments(
        clip,
        max_anchors=anchor_budget,
    )
    if not anchors:
        raise ValueError("clip transcript has no informative anchor candidates")

    matched = _candidate_matches(
        anchors,
        source,
        top_matches_per_anchor=top_matches_per_anchor,
        min_anchor_similarity=min_anchor_similarity,
    )
    if not matched:
        raise ValueError("no plausible transcript anchors found")

    path = _best_alignment_path(
        matched,
        same_offset_tolerance_seconds=same_offset_tolerance_seconds,
    )
    if not path:
        raise ValueError("no coherent transcript alignment path found")

    segments = _segment_path(
        path,
        clip,
        source,
        cut_offset_tolerance_seconds=cut_offset_tolerance_seconds,
        endpoint_extension_max_seconds=ENDPOINT_EXTENSION_MAX_SECONDS,
    )
    if not segments:
        raise ValueError("no coherent transcript alignment segments found")

    segments = _filter_spurious_segments(segments)
    if not segments:
        raise ValueError("no coherent transcript alignment segments found")

    segments = _merge_consistent_segments(
        segments,
        source_duration=source[-1].end,
        cut_offset_tolerance_seconds=cut_offset_tolerance_seconds,
    )

    all_anchors = [anchor for segment in segments for anchor in segment.anchors]
    all_offsets = [anchor.source_start - anchor.clip_start for anchor in all_anchors]
    average_similarity = statistics.fmean(anchor.similarity for anchor in all_anchors)
    coverage = min(1.0, len(all_anchors) / max(3, min(len(anchors), 10)))
    segment_confidence = statistics.fmean(segment.confidence for segment in segments)
    confidence = max(
        0.0,
        min(
            1.0,
            (0.50 * average_similarity)
            + (0.25 * coverage)
            + (0.25 * segment_confidence),
        ),
    )

    return ClipSourceAlignment(
        source_start=min(segment.source_start for segment in segments),
        source_end=max(segment.source_end for segment in segments),
        confidence=confidence,
        matched_anchors=len(all_anchors),
        total_anchors=len(anchors),
        median_offset=statistics.median(all_offsets),
        anchors=all_anchors,
        segments=segments,
    )
