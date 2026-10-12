"""Time-local Japanese ASR cross-checks do not certify word accuracy."""
from __future__ import annotations

import json

import pytest

from holoclip.models import TranscriptSegment
from holoclip.render_cut_extension_clip_asr_alignment import temporal_asr_review
from holoclip.render_cut_extension_three_way_clip_asr import _normalize
from test_render_cut_extension_three_way_clip_asr import (
    _FakeAsr, _case, _fake_extract,
)
from holoclip import render_cut_extension_three_way_clip_asr as cross
import asyncio


def _source(*values):
    return [{
        "id": f"b2-e{i:02d}",
        "text": _normalize(text),
        "output_start_seconds": start,
        "output_end_seconds": end,
    } for i, (text, start, end) in enumerate(values, start=1)]


def _run(expected, recognized, *, start=9.5, end=13.0):
    segments = [
        TranscriptSegment(start=first, end=last, text=text)
        for text, first, last in recognized
    ]
    return temporal_asr_review(
        expected=expected, heard=segments,
        output_focus_start_seconds=start,
        output_focus_end_seconds=end, normalize=_normalize,
    )


def test_japanese_exact_edit_audio_text_and_clock_align():
    sources = _source(("あの話を聞いて", 10, 10.8),
                      ("そこが面白いんだ", 11.2, 12.0))
    measured = _run(sources, [
        ("あの話を聞いて", .5, 1.3),
        ("そこが面白いんだ", 1.7, 2.5),
    ])
    assert measured["time_anchor_warning_count"] == 0
    assert measured["expected_utterance_count"] == 2
    assert measured["observed_segment_count"] == 2
    assert measured["observed_segment_without_matched_sampled_reference_count"] == 0
    assert [x["status"] for x in measured["source_utterance_alignment"]] == [
        "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED",
        "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED",
    ]
    assert measured["source_utterance_alignment"][1]["observed_segment_indices"] == [2]
    assert measured["source_utterance_alignment"][1]["center_clock_skew_seconds"] == 0.0
    assert measured["source_asr_not_ground_truth"] is True
    assert measured["edited_asr_not_ground_truth"] is True
    assert measured["human_audio_review_completed"] is False
    assert measured["ready"] is False and measured["auto_adoption_allowed"] is False
    assert "面白い" not in json.dumps(measured, ensure_ascii=False)


def test_partial_phrase_is_possible_truncation_not_full_presence():
    result = _run(_source(("今日はありがとうね", 10, 10.8)), [
        ("今日はありがとう", .5, 1.3),
    ])
    utterance = result["source_utterance_alignment"][0]
    assert utterance["status"] == "PARTIAL_ASR_TEXT_OVERLAP_REVIEW"
    assert utterance["missing_speech_proven"] is False
    assert result["time_anchor_warning_count"] == 1


def test_split_recognized_segments_are_joined_without_fake_gap():
    result = _run(_source(("今日はありがとうね", 10, 11.2)), [
        ("今日は", .5, .9),
        ("ありがとうね", .9, 1.7),
    ])
    utterance = result["source_utterance_alignment"][0]
    assert utterance["status"] == "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"
    assert utterance["observed_segment_indices"] == [1, 2]
    assert result["observed_segment_without_matched_sampled_reference_count"] == 0


def test_one_recognized_segment_may_cover_two_source_segments():
    source = _source(("今日はありがとう", 10, 10.6),
                     ("みんな大好き", 10.6, 11.2))
    result = _run(source, [("今日はありがとうみんな大好き", .5, 1.7)])
    assert [x["observed_segment_indices"] for x in
            result["source_utterance_alignment"]] == [[1], [1]]
    assert result["time_anchor_warning_count"] == 0
    assert result["source_utterance_alignment"][0]["repetition_proven"] is False


def test_multiple_plausible_occurrences_are_ambiguous_not_duplicate_proof():
    result = _run(_source(("ありがとうみんな", 10, 10.8)), [
        ("ありがとうみんな", .5, 1.3),
        ("ありがとうみんな", 2.0, 2.5),
    ])
    entry = result["source_utterance_alignment"][0]
    assert entry["status"] == "MULTIPLE_PLAUSIBLE_TRANSCRIPT_OCCURRENCES"
    assert entry["plausible_transcript_window_count"] >= 2
    assert entry["repetition_proven"] is False
    assert result["time_anchor_warning_count"] == 1


def test_same_text_at_distant_time_is_timing_warning():
    result = _run(_source(("ちょっと待ってね", 10, 10.5)), [
        ("ちょっと待ってね", 2.5, 3.0),
    ])
    entry = result["source_utterance_alignment"][0]
    assert entry["status"] == "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK"
    assert entry["center_clock_skew_seconds"] > 1.25
    assert entry["missing_speech_proven"] is False


def test_no_nearby_segments_reports_hypothesis_not_silence():
    result = _run(_source(("おはようございます", 10, 10.5)), [])
    assert result["source_utterance_alignment"][0]["status"] == (
        "NO_EDITED_ASR_NEAR_EXPECTED_TIME"
    )
    assert result["observed_segment_count"] == 0
    assert result["source_utterance_alignment"][0]["missing_speech_proven"] is False


def test_low_overlap_near_expected_clock_is_review_only():
    result = _run(_source(("こんなことあるのかな", 10, 10.8)), [
        ("ぜんぜん別のセリフ", .5, 1.3),
    ])
    assert result["source_utterance_alignment"][0]["status"] == (
        "LOW_ASR_TEXT_OVERLAP_AT_EXPECTED_TIME"
    )
    assert result["observed_segment_without_matched_sampled_reference_count"] == 1


def test_short_repeated_acknowledgment_always_abstains():
    result = _run(_source(("はい", 10, 10.8)), [("はい", .5, 1.3)])
    entry = result["source_utterance_alignment"][0]
    assert entry["status"] == "SHORT_TEXT_AMBIGUOUS_REVIEW"
    assert entry["repetition_proven"] is False


@pytest.mark.parametrize("changed", ["negative", "beyond", "nan", "many"])
def test_invalid_focus_and_decoded_time_rejected(changed):
    refs = _source(("ありがとうございます", 10, 10.8))
    args = {"expected": refs, "heard": [
        TranscriptSegment(start=.5, end=1.3, text="ありがとうございます")
    ], "output_focus_start_seconds": 9.5,
            "output_focus_end_seconds": 13.0, "normalize": _normalize}
    if changed == "negative":
        args["output_focus_start_seconds"] = -1
    elif changed == "beyond":
        args["heard"] = [TranscriptSegment(
            start=.5, end=25, text="ありがとうございます",
        )]
    elif changed == "nan":
        args["output_focus_start_seconds"] = float("nan")
    else:
        args["expected"] *= 10
    with pytest.raises(ValueError):
        temporal_asr_review(**args)


def test_real_three_way_report_has_time_cited_review_without_exposed_speech(
    tmp_path, monkeypatch,
):
    _, prepared, media, asr, _ = _case(tmp_path, monkeypatch)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    original = (media.read_bytes(), asr.read_bytes())
    report = asyncio.run(cross.observe_three_way_clip_asr(
        prepared, _FakeAsr(texts={
            "a": "何かがあった", "b": "まだ発話は続いている",
            "b2": "まだ発話は続いている",
        }),
    ))
    assert report["version"] == cross.VERSION
    assert report["clip_audio_transcribed"] is True
    for role in ("a", "b", "b2"):
        temporal = report["variants"][role]["temporal_asr_review"]
        assert temporal["version"]
        assert temporal["status"] == "LOCAL_ASR_TIME_ANCHORED_REVIEW_ONLY"
        assert temporal["human_audio_review_completed"] is False
        assert temporal["ready"] is False
        for item in temporal["source_utterance_alignment"]:
            assert "source_evidence_id" in item
            assert "expected_output_start_seconds" in item
            assert "expected_output_end_seconds" in item
            assert "observed_segment_indices" in item
            assert item["missing_speech_proven"] is False
            assert item["repetition_proven"] is False
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    assert (media.read_bytes(), asr.read_bytes()) == original
    assert "まだ発話は続いている" not in json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in json.dumps(report)
