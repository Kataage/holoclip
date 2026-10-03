from __future__ import annotations

import pytest

from holoclip.alignment import (
    AlignmentAnchor,
    AlignmentSegment,
    _best_alignment_path,
    _filter_spurious_segments,
    _merge_consistent_segments,
    _resolve_anchor_budget,
    _select_anchor_segments,
    align_clip_to_source,
    normalize_match_text,
)
from holoclip.models import TranscriptSegment


def segment(start: float, text: str, duration: float = 6.0) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=start + duration, text=text)


def test_normalize_match_text_removes_spacing_and_punctuation() -> None:
    assert normalize_match_text(" 今日は、いい天気！ ") == "今日はいい天気"


def test_adaptive_anchor_budget_scales_with_clip_duration() -> None:
    short = [
        segment(0, "short start"),
        segment(120, "short end"),
    ]
    long = [
        segment(0, "long start"),
        segment(900, "long end"),
    ]
    very_long = [
        segment(0, "very long start"),
        segment(3600, "very long end"),
    ]

    assert _resolve_anchor_budget(short, max_anchors=None) == 36
    assert _resolve_anchor_budget(long, max_anchors=None) == 61
    assert _resolve_anchor_budget(very_long, max_anchors=None) == 72


def test_explicit_anchor_budget_overrides_adaptive_rule() -> None:
    clip = [
        segment(0, "start"),
        segment(900, "end"),
    ]

    assert _resolve_anchor_budget(clip, max_anchors=24) == 24


def test_anchor_selection_is_timeline_balanced() -> None:
    clip = [
        segment(0, "序盤の非常に長い説明テキストその一その一その一", 5),
        segment(5, "序盤の非常に長い説明テキストその二その二その二", 5),
        segment(10, "序盤の非常に長い説明テキストその三その三その三", 5),
        segment(30, "中盤の短いけれど十分情報量のある発言です", 5),
        segment(60, "後半の短いけれど十分情報量のある発言です", 5),
        segment(90, "終盤の短いけれど十分情報量のある発言です", 5),
    ]

    selected = _select_anchor_segments(clip, max_anchors=4)

    assert len(selected) == 4
    assert selected[0].start <= 10
    assert any(item.start == 30 for item in selected)
    assert any(item.start == 60 for item in selected)
    assert any(item.start == 90 for item in selected)


def test_anchor_selection_ignores_generic_outro() -> None:
    clip = [
        segment(0, "レオンとクリスのどちらが好きかという話です"),
        segment(20, "私はクリスを選ぶと思います"),
        segment(40, "ご視聴ありがとうございました"),
    ]

    selected = _select_anchor_segments(clip, max_anchors=8)

    assert [item.start for item in selected] == [0, 20]


def test_large_unanchored_clip_tail_is_not_auto_covered() -> None:
    source = [
        segment(100, "ここが一致する重要な本文です"),
        segment(112, "この続きも一致する本文です"),
    ]
    clip = [
        segment(0, "ここが一致する重要な本文です"),
        segment(12, "この続きも一致する本文です"),
        segment(60, "ご視聴ありがとうございました"),
    ]

    result = align_clip_to_source(clip, source)

    assert len(result.segments) == 1
    assert result.segments[0].clip_end < 30
    assert result.segments[0].source_start == pytest.approx(100, abs=1)


def test_align_clip_to_source_finds_known_offset() -> None:
    source = [
        segment(0, "今日はゲームの設定から始めます"),
        segment(120, "全然関係ない雑談をしています"),
        segment(300, "ここも別の話題なので違います"),
        segment(600, "実は昨日すごくびっくりすることがあってさ"),
        segment(612, "二ヶ月くらい給料が入らなかった時期があったんだよね"),
        segment(624, "でもその時は貯金があったからなんとか生活できて"),
        segment(636, "もやし生活だったから別に困らなかったんだけどね"),
        segment(900, "ここから先はまた別の話です"),
    ]
    clip = [
        segment(0, "実は昨日すごくびっくりすることがあってさ"),
        segment(12, "2ヶ月くらい給料が入らなかった時期があったんだよね"),
        segment(24, "でもその時は貯金があったから何とか生活できて"),
        segment(36, "もやし生活だったから別に困らなかったんだけどね"),
    ]

    result = align_clip_to_source(clip, source)

    assert result.source_start == pytest.approx(600.0, abs=1.0)
    assert result.source_end == pytest.approx(642.0, abs=2.0)
    assert result.matched_anchors >= 3
    assert result.confidence >= 0.55
    assert len(result.segments) == 1
    assert result.segments[0].source_start == pytest.approx(600.0, abs=1.0)


def test_align_clip_to_source_rejects_unrelated_text() -> None:
    source = [
        segment(0, "りんごとみかんを買いに行きました"),
        segment(10, "今日は料理を作って食べます"),
    ]
    clip = [
        segment(0, "宇宙船のエンジンを修理しています"),
        segment(10, "惑星の軌道について話しています"),
    ]

    with pytest.raises(ValueError, match="no plausible transcript anchors"):
        align_clip_to_source(clip, source)


def test_align_clip_to_source_detects_multiple_cut_segments() -> None:
    source = [
        segment(0, "配信開始のあいさつをしています"),
        segment(600, "まず最初に大事な話をしておきたいんだけど"),
        segment(612, "昨日かなりびっくりする出来事がありました"),
        segment(624, "ここまでが最初の話のオチになります"),
        segment(800, "途中では全く別の雑談をしています"),
        segment(1000, "さらに別のゲームの話題が続いています"),
        segment(1200, "さっきの話に関係する後日談があるんだけど"),
        segment(1212, "実はその後さらに予想外のことが起きました"),
        segment(1224, "最後はみんなで笑って終わりました"),
        segment(1500, "ここからエンディングです"),
    ]
    clip = [
        segment(0, "まず最初に大事な話をしておきたいんだけど"),
        segment(12, "昨日かなりびっくりする出来事がありました"),
        segment(24, "ここまでが最初の話のオチになります"),
        segment(30, "さっきの話に関係する後日談があるんだけど"),
        segment(42, "実はその後さらに予想外のことが起きました"),
        segment(54, "最後はみんなで笑って終わりました"),
    ]

    result = align_clip_to_source(clip, source)

    assert result.confidence >= 0.75
    assert len(result.segments) == 2
    assert result.segments[0].clip_start == pytest.approx(0)
    assert result.segments[0].source_start == pytest.approx(600, abs=1)
    assert result.segments[0].source_end == pytest.approx(630, abs=2)
    assert result.segments[1].clip_start == pytest.approx(30, abs=1)
    assert result.segments[1].source_start == pytest.approx(1200, abs=1)
    assert result.segments[1].source_end == pytest.approx(1230, abs=2)



def test_filter_spurious_singleton_source_regression() -> None:
    def aligned_segment(
        *,
        clip_start: float,
        clip_end: float,
        source_start: float,
        source_end: float,
        confidence: float,
        matched_anchors: int,
    ) -> AlignmentSegment:
        anchors = [
            AlignmentAnchor(
                clip_start=clip_start + index,
                source_start=source_start + index,
                similarity=confidence,
                clip_text=f"clip-{index}",
                source_text=f"source-{index}",
            )
            for index in range(matched_anchors)
        ]
        return AlignmentSegment(
            clip_start=clip_start,
            clip_end=clip_end,
            source_start=source_start,
            source_end=source_end,
            confidence=confidence,
            matched_anchors=matched_anchors,
            median_offset=source_start - clip_start,
            anchors=anchors,
        )

    segments = [
        aligned_segment(
            clip_start=0,
            clip_end=145,
            source_start=1997,
            source_end=2142,
            confidence=0.797,
            matched_anchors=11,
        ),
        aligned_segment(
            clip_start=158,
            clip_end=164,
            source_start=574,
            source_end=580,
            confidence=0.554,
            matched_anchors=1,
        ),
        aligned_segment(
            clip_start=164,
            clip_end=413,
            source_start=2175,
            source_end=2423,
            confidence=0.798,
            matched_anchors=12,
        ),
    ]

    filtered = _filter_spurious_segments(segments)

    assert len(filtered) == 2
    assert [item.source_start for item in filtered] == [1997, 2175]


def test_filter_removes_weak_singleton_contained_in_adjacent_strong_span() -> None:
    def item(
        clip_start: float,
        clip_end: float,
        source_start: float,
        source_end: float,
        confidence: float,
        anchors: int,
    ) -> AlignmentSegment:
        return AlignmentSegment(
            clip_start=clip_start,
            clip_end=clip_end,
            source_start=source_start,
            source_end=source_end,
            confidence=confidence,
            matched_anchors=anchors,
            median_offset=source_start - clip_start,
            anchors=[
                AlignmentAnchor(
                    clip_start=clip_start + index,
                    source_start=source_start + index,
                    similarity=confidence,
                    clip_text="clip",
                    source_text="source",
                )
                for index in range(anchors)
            ],
        )

    segments = [
        item(8, 122, 1242, 1356, 0.849, 6),
        item(143, 147, 1944, 1948, 0.545, 1),
        item(155, 751, 1390, 1986, 0.858, 29),
    ]

    filtered = _filter_spurious_segments(segments)

    assert len(filtered) == 2
    assert filtered == [segments[0], segments[2]]


def test_filter_keeps_strong_or_multi_anchor_reordered_segment() -> None:
    strong = AlignmentSegment(
        clip_start=20,
        clip_end=40,
        source_start=100,
        source_end=120,
        confidence=0.90,
        matched_anchors=3,
        median_offset=80,
        anchors=[
            AlignmentAnchor(
                clip_start=20 + index,
                source_start=100 + index,
                similarity=0.9,
                clip_text="clip",
                source_text="source",
            )
            for index in range(3)
        ],
    )
    before = AlignmentSegment(
        clip_start=0,
        clip_end=20,
        source_start=500,
        source_end=520,
        confidence=0.9,
        matched_anchors=3,
        median_offset=500,
        anchors=[
            AlignmentAnchor(
                clip_start=index,
                source_start=500 + index,
                similarity=0.9,
                clip_text="clip",
                source_text="source",
            )
            for index in range(3)
        ],
    )
    after = AlignmentSegment(
        clip_start=40,
        clip_end=60,
        source_start=600,
        source_end=620,
        confidence=0.9,
        matched_anchors=3,
        median_offset=560,
        anchors=[
            AlignmentAnchor(
                clip_start=40 + index,
                source_start=600 + index,
                similarity=0.9,
                clip_text="clip",
                source_text="source",
            )
            for index in range(3)
        ],
    )

    assert _filter_spurious_segments([before, strong, after]) == [
        before,
        strong,
        after,
    ]



def test_best_path_prefers_forward_candidate_over_better_backward_singleton() -> None:
    clip_a = segment(0, "first")
    clip_b = segment(10, "middle")
    clip_c = segment(20, "last")
    matched = [
        (clip_a, [(1000.0, "first", 0.80)]),
        (
            clip_b,
            [
                (100.0, "middle duplicate", 0.99),
                (1100.0, "middle correct", 0.40),
            ],
        ),
        (clip_c, [(1110.0, "last", 0.90)]),
    ]

    path = _best_alignment_path(
        matched,
        same_offset_tolerance_seconds=22.0,
    )

    assert [row[1] for row in path] == [1000.0, 1100.0, 1110.0]


def test_filter_spurious_singleton_accepts_real_world_0628_regression() -> None:
    def item(
        clip_start: float,
        source_start: float,
        confidence: float,
        anchors: int,
        duration: float,
    ) -> AlignmentSegment:
        return AlignmentSegment(
            clip_start=clip_start,
            clip_end=clip_start + duration,
            source_start=source_start,
            source_end=source_start + duration,
            confidence=confidence,
            matched_anchors=anchors,
            median_offset=source_start - clip_start,
            anchors=[
                AlignmentAnchor(
                    clip_start=clip_start + index,
                    source_start=source_start + index,
                    similarity=confidence,
                    clip_text="clip",
                    source_text="source",
                )
                for index in range(anchors)
            ],
        )

    segments = [
        item(0, 1231, 0.726, 2, 123),
        item(143, 215, 0.628, 1, 4),
        item(186, 1421, 0.813, 9, 244),
    ]

    filtered = _filter_spurious_segments(segments)

    assert len(filtered) == 2
    assert [segment.source_start for segment in filtered] == [1231, 1421]



def test_merge_consistent_segments_rejoins_gap_after_outlier_removal() -> None:
    def item(
        clip_start: float,
        clip_end: float,
        source_start: float,
        source_end: float,
        confidence: float,
        anchors: int,
    ) -> AlignmentSegment:
        offset = source_start - clip_start
        return AlignmentSegment(
            clip_start=clip_start,
            clip_end=clip_end,
            source_start=source_start,
            source_end=source_end,
            confidence=confidence,
            matched_anchors=anchors,
            median_offset=offset,
            anchors=[
                AlignmentAnchor(
                    clip_start=clip_start + index,
                    source_start=source_start + index,
                    similarity=confidence,
                    clip_text=f"clip-{index}",
                    source_text=f"source-{index}",
                )
                for index in range(anchors)
            ],
        )

    segments = [
        item(0.0, 122.0, 1231.0, 1354.0, 0.726, 2),
        item(186.0, 430.0, 1421.0, 1666.0, 0.813, 9),
        item(436.0, 771.0, 1672.0, 2007.0, 0.823, 12),
    ]

    merged = _merge_consistent_segments(
        segments,
        source_duration=4000.0,
        cut_offset_tolerance_seconds=12.0,
    )

    assert len(merged) == 1
    assert merged[0].clip_start == pytest.approx(0.0)
    assert merged[0].clip_end == pytest.approx(771.0)
    assert merged[0].source_start == pytest.approx(1235.0, abs=6.0)
    assert merged[0].source_end == pytest.approx(2006.0, abs=6.0)
    assert merged[0].matched_anchors == 23


def test_merge_consistent_segments_preserves_real_cut_offset_jump() -> None:
    left = AlignmentSegment(
        clip_start=0,
        clip_end=60,
        source_start=1000,
        source_end=1060,
        confidence=0.9,
        matched_anchors=3,
        median_offset=1000,
        anchors=[
            AlignmentAnchor(
                clip_start=float(index),
                source_start=1000.0 + index,
                similarity=0.9,
                clip_text="clip",
                source_text="source",
            )
            for index in range(3)
        ],
    )
    right = AlignmentSegment(
        clip_start=60,
        clip_end=120,
        source_start=1200,
        source_end=1260,
        confidence=0.9,
        matched_anchors=3,
        median_offset=1140,
        anchors=[
            AlignmentAnchor(
                clip_start=60.0 + index,
                source_start=1200.0 + index,
                similarity=0.9,
                clip_text="clip",
                source_text="source",
            )
            for index in range(3)
        ],
    )

    merged = _merge_consistent_segments(
        [left, right],
        source_duration=4000.0,
        cut_offset_tolerance_seconds=12.0,
    )

    assert merged == [left, right]
