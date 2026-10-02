from __future__ import annotations

import pytest

from holoclip.alignment import (
    AlignmentAnchor,
    AlignmentSegment,
    _best_alignment_path,
    _filter_spurious_segments,
    align_clip_to_source,
    normalize_match_text,
)
from holoclip.models import TranscriptSegment


def segment(start: float, text: str, duration: float = 6.0) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=start + duration, text=text)


def test_normalize_match_text_removes_spacing_and_punctuation() -> None:
    assert normalize_match_text(" 今日は、いい天気！ ") == "今日はいい天気"


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
