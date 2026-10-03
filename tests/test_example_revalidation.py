from holoclip.alignment import ALIGNMENT_METHOD, align_clip_to_source
from holoclip.example_revalidation import realign_human_clip_example
from holoclip.examples import (
    WEAK_NEGATIVE_METHOD,
    AlignmentSegmentExample,
    HumanClipExample,
)
from holoclip.models import TranscriptSegment


def seg(start: float, text: str, duration: float = 6.0) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=start + duration, text=text)


def test_realign_upgrades_alignment_and_invalidates_old_weak_negative_method() -> None:
    example = HumanClipExample(
        id="legacy",
        clip_url="https://example.invalid/clip",
        clip_title="legacy",
        source_start=10,
        source_end=20,
        alignment_confidence=0.7,
        alignment_method="transcript-anchor-v2",
        alignment_matched_anchors=1,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=10,
                source_start=10,
                source_end=20,
                confidence=0.7,
                matched_anchors=1,
            )
        ],
        weak_negative_method=WEAK_NEGATIVE_METHOD,
    )
    clip = [
        seg(0, "実は昨日すごくびっくりすることがあってさ"),
        seg(12, "二ヶ月くらい給料が入らなかった時期があったんだよね"),
        seg(24, "でもその時は貯金があったからなんとか生活できて"),
    ]
    source = [
        seg(100, "前の雑談です"),
        seg(600, "実は昨日すごくびっくりすることがあってさ"),
        seg(612, "二ヶ月くらい給料が入らなかった時期があったんだよね"),
        seg(624, "でもその時は貯金があったからなんとか生活できて"),
    ]

    result = realign_human_clip_example(example, clip, source)

    assert result.accepted is True
    assert result.alignment_changed is True
    assert result.alignment_geometry_changed is True
    assert result.example.alignment_method == ALIGNMENT_METHOD
    assert result.example.alignment_total_anchors is not None
    assert result.example.alignment_total_anchors >= result.example.alignment_matched_anchors
    assert result.example.source_start == 600
    assert result.example.weak_negative_method is None


def test_alignment_method_upgrade_does_not_count_as_geometry_change() -> None:
    clip = [
        seg(0, "実は昨日すごくびっくりすることがあってさ"),
        seg(12, "二ヶ月くらい給料が入らなかった時期があったんだよね"),
        seg(24, "でもその時は貯金があったからなんとか生活できて"),
    ]
    source = [
        seg(600, "実は昨日すごくびっくりすることがあってさ"),
        seg(612, "二ヶ月くらい給料が入らなかった時期があったんだよね"),
        seg(624, "でもその時は貯金があったからなんとか生活できて"),
    ]
    aligned = align_clip_to_source(clip, source)
    example = HumanClipExample(
        id="version-only",
        clip_url="https://example.invalid/version-only",
        clip_title="version-only",
        source_start=aligned.source_start,
        source_end=aligned.source_end,
        alignment_confidence=aligned.confidence,
        alignment_method="transcript-anchor-v5",
        alignment_matched_anchors=aligned.matched_anchors,
        alignment_total_anchors=aligned.total_anchors,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=item.clip_start,
                clip_end=item.clip_end,
                source_start=item.source_start,
                source_end=item.source_end,
                confidence=item.confidence,
                matched_anchors=item.matched_anchors,
            )
            for item in aligned.segments
        ],
        weak_negative_method=WEAK_NEGATIVE_METHOD,
    )

    result = realign_human_clip_example(example, clip, source)

    assert result.accepted is True
    assert result.alignment_changed is True
    assert result.alignment_geometry_changed is False
    assert result.example.alignment_method == ALIGNMENT_METHOD
    assert result.example.weak_negative_method is None


def test_unalignable_example_becomes_explicit_unaligned_result() -> None:
    example = HumanClipExample(
        id="bad",
        clip_url="https://example.invalid/bad",
        clip_title="bad",
        source_start=10,
        source_end=20,
        alignment_confidence=0.8,
        alignment_method="transcript-anchor-v2",
    )
    clip = [seg(0, "宇宙船のエンジンを修理しています")]
    source = [seg(100, "りんごとみかんを買いに行きました")]

    result = realign_human_clip_example(example, clip, source)

    assert result.accepted is False
    assert result.example.source_start is None
    assert result.example.source_end is None
    assert result.example.alignment_confidence == 0.0
    assert result.example.alignment_segments == []
    assert result.example.alignment_method == ALIGNMENT_METHOD
