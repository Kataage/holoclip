from holoclip.alignment import ALIGNMENT_METHOD
from holoclip.example_quality import (
    LearningQuality,
    LearningQualityReason,
    compute_alignment_quality_metrics,
    evaluate_human_example_learning_quality,
)
from holoclip.examples import AlignmentSegmentExample, HumanClipExample


def aligned_segment(
    clip_start: float,
    clip_end: float,
    source_start: float,
    source_end: float,
    *,
    confidence: float = 0.9,
    anchors: int = 3,
) -> AlignmentSegmentExample:
    return AlignmentSegmentExample(
        clip_start=clip_start,
        clip_end=clip_end,
        source_start=source_start,
        source_end=source_end,
        confidence=confidence,
        matched_anchors=anchors,
    )


def example_with_segments(
    segments: list[AlignmentSegmentExample],
    *,
    confidence: float = 0.9,
    method: str = ALIGNMENT_METHOD,
    source_url: str = "https://youtu.be/source",
) -> HumanClipExample:
    return HumanClipExample(
        id="quality-example",
        clip_url="https://youtu.be/clip",
        clip_title="quality",
        source_url=source_url,
        source_start=min(item.source_start for item in segments),
        source_end=max(item.source_end for item in segments),
        alignment_confidence=confidence,
        alignment_method=method,
        alignment_matched_anchors=sum(item.matched_anchors for item in segments),
        alignment_total_anchors=sum(item.matched_anchors for item in segments),
        alignment_segments=segments,
        acquisition_provenance={"resolved_source_url": source_url},
        setup="setup",
        development="development",
        payoff="payoff",
        why_selected="why selected",
    )


def test_temporal_coverage_uses_interval_union() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 40, 100, 140),
            aligned_segment(30, 70, 200, 240),
            aligned_segment(90, 100, 300, 310),
        ]
    )

    metrics = compute_alignment_quality_metrics(item)

    assert metrics.clip_timeline_seconds == 100
    assert metrics.aligned_clip_seconds == 80
    assert metrics.clip_coverage_ratio == 0.8
    assert metrics.uncovered_clip_seconds == 20
    assert metrics.largest_uncovered_clip_gap == 20


def test_continuous_current_alignment_is_trusted() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 100, 160, anchors=6)]
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.TRUSTED
    assert quality.reasons == []
    assert quality.metrics.clip_coverage_ratio == 1.0


def test_marginal_confidence_routes_to_review() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 100, 160, anchors=6)],
        confidence=0.70,
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.MARGINAL_ALIGNMENT_CONFIDENCE in quality.reasons


def test_coherent_anchor_gaps_count_toward_explained_coverage() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 40, 100, 140, confidence=0.8, anchors=2),
            aligned_segment(80, 100, 198, 218, confidence=0.9, anchors=1),
            aligned_segment(140, 180, 278, 318, confidence=0.85, anchors=3),
        ]
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.metrics.clip_coverage_ratio == (100 / 180)
    assert quality.metrics.bridged_clip_seconds == 80
    assert quality.metrics.explained_clip_coverage_ratio == 1.0
    assert quality.metrics.explained_bridge_count == 2
    assert quality.metrics.largest_unexplained_clip_gap == 0
    assert quality.decision is LearningQuality.TRUSTED
    assert LearningQualityReason.LOW_CLIP_COVERAGE not in quality.reasons


def test_weak_boundary_prevents_explained_gap_bridge() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 20, 100, 120, confidence=0.9, anchors=3),
            aligned_segment(60, 80, 170, 190, confidence=0.65, anchors=1),
        ]
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.metrics.bridged_clip_seconds == 0
    assert quality.metrics.explained_clip_coverage_ratio == 0.5
    assert quality.decision is LearningQuality.REVIEW


def test_sparse_weak_singleton_warning_does_not_block_strong_alignment() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 20, 100, 120, confidence=0.9, anchors=3),
            aligned_segment(20, 40, 120, 140, confidence=0.9, anchors=3),
            aligned_segment(40, 60, 140, 160, confidence=0.65, anchors=1),
            aligned_segment(60, 80, 160, 180, confidence=0.9, anchors=3),
            aligned_segment(80, 100, 180, 200, confidence=0.9, anchors=3),
            aligned_segment(100, 120, 200, 220, confidence=0.9, anchors=3),
        ],
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.metrics.weak_singleton_count == 1
    assert quality.metrics.weak_singleton_ratio == (1 / 6)
    assert quality.decision is LearningQuality.TRUSTED
    assert LearningQualityReason.WEAK_SINGLETON_ALIGNMENT not in quality.reasons


def test_high_weak_singleton_ratio_routes_to_review() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 20, 100, 120, confidence=0.65, anchors=1),
            aligned_segment(20, 40, 120, 140, confidence=0.9, anchors=3),
            aligned_segment(40, 60, 140, 160, confidence=0.65, anchors=1),
            aligned_segment(60, 80, 160, 180, confidence=0.9, anchors=3),
        ],
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.metrics.weak_singleton_count == 2
    assert quality.metrics.weak_singleton_ratio == 0.5
    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.WEAK_SINGLETON_ALIGNMENT in quality.reasons


def test_sparse_multicut_is_preserved_for_review_not_rejected() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 20, 100, 120),
            aligned_segment(80, 100, 500, 520),
        ]
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.LOW_CLIP_COVERAGE in quality.reasons
    assert LearningQualityReason.LARGE_UNCOVERED_CLIP_GAP in quality.reasons


def test_nonmonotonic_alignment_routes_to_review() -> None:
    item = example_with_segments(
        [
            aligned_segment(0, 10, 700, 710),
            aligned_segment(10, 20, 100, 110),
            aligned_segment(20, 30, 800, 810),
        ]
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.NON_MONOTONIC_ALIGNMENT in quality.reasons
    assert quality.metrics.non_monotonic_transition_count == 1
    assert quality.metrics.largest_backward_jump_seconds == 610


def test_large_source_hint_disagreement_routes_to_review() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 300, 360)],
        source_url="https://youtu.be/source?t=1800",
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.SOURCE_HINT_DISAGREEMENT in quality.reasons
    assert quality.source_hint_delta_seconds == -1500


def test_auto_detected_source_hint_disagreement_is_diagnostic_only() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 300, 360)],
        source_url="https://youtu.be/source",
    )
    item.acquisition_provenance = {
        "requested_source_url": None,
        "resolved_source_url": "https://youtu.be/source?t=1800",
        "source_auto_detected": True,
    }

    quality = evaluate_human_example_learning_quality(item)

    assert quality.source_hint_delta_seconds == -1500
    assert quality.decision is LearningQuality.TRUSTED
    assert LearningQualityReason.SOURCE_HINT_DISAGREEMENT not in quality.reasons


def test_explicit_requested_source_hint_disagreement_still_routes_to_review() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 300, 360)],
        source_url="https://youtu.be/source",
    )
    item.acquisition_provenance = {
        "requested_source_url": "https://youtu.be/source?t=1800",
        "resolved_source_url": "https://youtu.be/source?t=1800",
        "source_auto_detected": False,
    }

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.SOURCE_HINT_DISAGREEMENT in quality.reasons


def test_old_alignment_method_routes_to_review() -> None:
    item = example_with_segments(
        [aligned_segment(0, 60, 100, 160)],
        method="transcript-anchor-v2",
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REVIEW
    assert LearningQualityReason.LEGACY_ALIGNMENT_METHOD in quality.reasons


def test_missing_source_alignment_is_rejected() -> None:
    item = HumanClipExample(
        id="unaligned",
        clip_url="https://youtu.be/clip",
        clip_title="unaligned",
        alignment_confidence=0.4,
        alignment_method=ALIGNMENT_METHOD,
    )

    quality = evaluate_human_example_learning_quality(item)

    assert quality.decision is LearningQuality.REJECTED
    assert quality.reasons == [LearningQualityReason.NO_ALIGNMENT]
