from __future__ import annotations

import pytest

from holoclip.alignment import AlignmentSegment, ClipSourceAlignment
from holoclip.domain import (
    ClipTimeline,
    EditorialArchetype,
    EditorialIdea,
    TimelineSegment,
    make_editorial_idea_id,
    make_source_asset_id,
    make_timeline_segment_id,
    make_transcript_segment_id,
)
from holoclip.domain_adapters import (
    alignment_to_clip_timeline,
    candidate_to_source_moment,
    canonicalize_transcript_segments,
    human_example_to_clip_timeline,
)
from holoclip.examples import AlignmentSegmentExample, HumanClipExample
from holoclip.models import ClipCandidate, TranscriptSegment


def test_stable_ids_are_deterministic() -> None:
    source_id = make_source_asset_id("youtube", "abc123")

    assert source_id == make_source_asset_id("youtube", "abc123")
    assert source_id != make_source_asset_id("youtube", "other")
    assert make_transcript_segment_id(
        source_id,
        10.0,
        12.5,
        " こんにちは   世界 ",
    ) == make_transcript_segment_id(
        source_id,
        10.0,
        12.5,
        "こんにちは 世界",
    )


def test_legacy_transcript_converts_without_losing_content() -> None:
    source_id = make_source_asset_id("local", "test-01")
    legacy = [
        TranscriptSegment(start=10.25, end=12.75, text="こんにちは", speaker="A"),
        TranscriptSegment(start=13.0, end=15.0, text="続きです"),
    ]

    canonical = canonicalize_transcript_segments(source_id, legacy)

    assert [item.start for item in canonical] == [10.25, 13.0]
    assert [item.end for item in canonical] == [12.75, 15.0]
    assert [item.text for item in canonical] == ["こんにちは", "続きです"]
    assert canonical[0].speaker == "A"
    assert all(item.source_asset_id == source_id for item in canonical)
    assert len({item.id for item in canonical}) == 2


def test_candidate_converts_to_source_moment() -> None:
    source_id = make_source_asset_id("local", "test-01")
    candidate = ClipCandidate(
        start=100,
        end=160,
        working_title="給料の話",
        setup="前振り",
        development="告白",
        payoff="オチ",
        why_interesting="意外な告白から明確なオチまである",
        traits=["unexpected_confession"],
        confidence=0.9,
    )

    moment = candidate_to_source_moment(
        source_id,
        candidate,
        transcript_segment_ids=["seg_a", "seg_b"],
    )

    assert moment.source_asset_id == source_id
    assert moment.start == 100
    assert moment.end == 160
    assert moment.transcript_segment_ids == ["seg_a", "seg_b"]
    assert moment.traits == ["unexpected_confession"]
    assert moment.confidence == 0.9


def test_alignment_v2_converts_to_multicut_timeline() -> None:
    source_id = make_source_asset_id("local", "test-01")
    alignment = ClipSourceAlignment(
        source_start=600,
        source_end=1230,
        confidence=0.91,
        matched_anchors=6,
        total_anchors=6,
        median_offset=900,
        anchors=[],
        segments=[
            AlignmentSegment(
                clip_start=0,
                clip_end=30,
                source_start=600,
                source_end=630,
                confidence=0.95,
                matched_anchors=3,
                median_offset=600,
                anchors=[],
            ),
            AlignmentSegment(
                clip_start=30,
                clip_end=60,
                source_start=1200,
                source_end=1230,
                confidence=0.92,
                matched_anchors=3,
                median_offset=1170,
                anchors=[],
            ),
        ],
    )

    timeline = alignment_to_clip_timeline(source_id, alignment)

    assert len(timeline.segments) == 2
    assert timeline.estimated_duration == pytest.approx(60)
    assert timeline.segments[0].source_start == 600
    assert timeline.segments[1].source_start == 1200
    assert [segment.output_order for segment in timeline.segments] == [0, 1]


def test_human_example_multicut_converts_losslessly() -> None:
    source_id = make_source_asset_id("youtube", "source-1")
    example = HumanClipExample(
        id="clip",
        clip_url="https://example.invalid/clip",
        clip_title="multi",
        source_start=100,
        source_end=220,
        alignment_confidence=0.9,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=30,
                source_start=100,
                source_end=130,
                confidence=0.9,
                matched_anchors=4,
            ),
            AlignmentSegmentExample(
                clip_start=30,
                clip_end=60,
                source_start=190,
                source_end=220,
                confidence=0.88,
                matched_anchors=3,
            ),
        ],
    )

    timeline = human_example_to_clip_timeline(example, source_asset_id=source_id)

    assert timeline is not None
    assert [(item.source_start, item.source_end) for item in timeline.segments] == [
        (100, 130),
        (190, 220),
    ]
    assert timeline.estimated_duration == 60


def test_timeline_supports_multiple_source_assets() -> None:
    first = make_source_asset_id("youtube", "stream-a")
    second = make_source_asset_id("youtube", "stream-b")
    segments = [
        TimelineSegment(
            id=make_timeline_segment_id(first, 100, 130, 0),
            source_asset_id=first,
            source_start=100,
            source_end=130,
            output_order=0,
            purpose="setup",
        ),
        TimelineSegment(
            id=make_timeline_segment_id(second, 500, 540, 1),
            source_asset_id=second,
            source_start=500,
            source_end=540,
            output_order=1,
            purpose="payoff",
        ),
    ]

    timeline = ClipTimeline(
        id="timeline",
        segments=segments,
        estimated_duration=70,
    )

    assert {item.source_asset_id for item in timeline.segments} == {first, second}


def test_timeline_rejects_non_contiguous_output_order() -> None:
    source_id = make_source_asset_id("local", "test")
    with pytest.raises(ValueError, match="contiguous"):
        ClipTimeline(
            id="bad",
            segments=[
                TimelineSegment(
                    id="a",
                    source_asset_id=source_id,
                    source_start=0,
                    source_end=10,
                    output_order=0,
                ),
                TimelineSegment(
                    id="b",
                    source_asset_id=source_id,
                    source_start=20,
                    source_end=30,
                    output_order=2,
                ),
            ],
            estimated_duration=20,
        )


def test_timeline_rejects_wrong_duration() -> None:
    source_id = make_source_asset_id("local", "test")
    with pytest.raises(ValueError, match="estimated_duration"):
        ClipTimeline(
            id="bad-duration",
            segments=[
                TimelineSegment(
                    id="a",
                    source_asset_id=source_id,
                    source_start=0,
                    source_end=10,
                    output_order=0,
                )
            ],
            estimated_duration=9,
        )


def test_editorial_idea_supports_multi_source_and_stable_id() -> None:
    first = make_source_asset_id("youtube", "stream-a")
    second = make_source_asset_id("youtube", "stream-b")
    moment_ids = ["mom_a", "mom_b"]
    idea_id = make_editorial_idea_id(
        EditorialArchetype.HISTORICAL_CALLBACK,
        moment_ids,
        "過去の発言が現在のオチにつながる",
    )

    idea = EditorialIdea(
        id=idea_id,
        archetype=EditorialArchetype.HISTORICAL_CALLBACK,
        thesis="過去の発言が現在のオチにつながる",
        viewer_promise="昔を知らない人にも流れが分かる",
        hook="昔は絶対やらないと言っていた",
        target_duration_range={
            "minimum_seconds": 120,
            "maximum_seconds": 480,
        },
        required_source_asset_ids=[first, second],
        source_moment_ids=moment_ids,
        generation_horizon="historical",
    )

    assert idea.id == make_editorial_idea_id(
        EditorialArchetype.HISTORICAL_CALLBACK,
        list(reversed(moment_ids)),
        "過去の発言が現在のオチにつながる",
    )
    assert len(idea.required_source_asset_ids) == 2


def test_editorial_idea_rejects_source_in_required_and_optional() -> None:
    source_id = make_source_asset_id("youtube", "stream-a")
    with pytest.raises(ValueError, match="must not overlap"):
        EditorialIdea(
            id="idea",
            archetype="single_topic_story",
            thesis="話",
            viewer_promise="分かる",
            hook="導入",
            target_duration_range={
                "minimum_seconds": 60,
                "maximum_seconds": 180,
            },
            required_source_asset_ids=[source_id],
            optional_source_asset_ids=[source_id],
        )
