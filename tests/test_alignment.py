from __future__ import annotations

import pytest

from holoclip.alignment import align_clip_to_source, normalize_match_text
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
