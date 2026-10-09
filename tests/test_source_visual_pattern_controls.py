"""Measured public-source pattern control regression and cross-source reporting."""
import copy

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.source_visual_control_report import report_saved_source_controls
from holoclip.source_visual_pattern_controls import (
    VERSION as PATTERN_VERSION, PATTERNS, measure_video_pattern_controls,
)
from holoclip.source_visual_pattern_report import (
    VERSION, add_pattern_result, checked_pattern_history,
    report_saved_patterns, summarize_pattern_results,
)
from test_source_candidate_visual import make_state, Provider, verify
from test_source_visual_real_controls import _window

CLIP = "youtube:AAAAAAAAAAA"
STREAM = "BBBBBBBBBBB"


def real_patterns(tmp_path):
    measured = _window(tmp_path)
    report = measure_video_pattern_controls(
        measured, root=tmp_path/"constructed",
    )
    assert report["status"] == "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE"
    return report


def recorded(tmp_path):
    result = real_patterns(tmp_path)
    history, added = add_pattern_result(
        [], clip_id=CLIP, candidate_ids=[STREAM],
        source_id=STREAM, result=result,
    )
    assert added is True
    return history


def test_five_real_mp4_decoded_control_patterns_are_measured_without_gold(tmp_path):
    result = real_patterns(tmp_path)
    assert result["version"] == PATTERN_VERSION
    assert set(result["patterns"]) == set(PATTERNS)
    assert result["measured_frame_count"] == 4
    assert len(result["measured_frame_sha256s"]) == 4
    assert result["patterns"]["jpeg_overlay"]["behaved_as_constructed"] is True
    assert result["patterns"]["reversed_time"]["behaved_as_constructed"] is True
    assert result["patterns"]["cyclic_time_shift"]["behaved_as_constructed"] is True
    assert result["source_identity_verified"] is False
    assert result["real_human_editor_labels"] is False
    assert result["official_hololive_source_verified"] is False
    assert result["editor_cut_precision"] is None
    assert result["editor_cut_recall"] is None
    assert result["real_original_match_false_positive_rate"] is None


def test_unrecognized_matcher_outcomes_are_unevaluable_not_decoy_success(tmp_path):
    measured = _window(tmp_path)
    def unavailable(*_args):
        return {
            "status": "UNKNOWN_IMAGE_DECODE",
            "source_identity_verified": False,
            "reviewed_gold_available": False,
            "qualification_allowed": False,
            "used_in_learning_or_holdout": False,
            "original_video_absolute_pts_verified": False,
        }
    result = measure_video_pattern_controls(
        measured, root=tmp_path/"controls", matcher=unavailable,
    )
    assert all(row["behaved_as_constructed"] is None
               for row in result["patterns"].values())
    history, added = add_pattern_result(
        [], clip_id=CLIP, candidate_ids=[STREAM],
        source_id=STREAM, result=result,
    )
    assert added is True
    summary = summarize_pattern_results(history)
    assert all(row["evaluable_cases"] == 0
               and row["unevaluable_cases"] == 1
               and row["constructed_case_success_fraction"] is None
               for row in summary["pattern_results"].values())
    assert summary["editor_cut_precision"] is None


def test_pattern_video_same_sha_replay_is_not_an_independent_source(tmp_path):
    history = recorded(tmp_path)
    again, added = add_pattern_result(
        history, clip_id=CLIP, candidate_ids=[STREAM],
        source_id=STREAM, result=history[0]["result"],
    )
    assert added is False
    assert again == history
    summary = summarize_pattern_results(again)
    assert summary["real_measured_source_video_samples"] == 1
    assert summary["distinct_candidate_source_streams_measured"] == 1
    assert summary["source_diversity_sufficient_for_generalization"] is False
    assert summary["precision_recall_estimable"] is False
    assert summary["ready"] is False


def test_no_real_samples_means_no_measured_pattern_pass_rate(tmp_path):
    empty = report_saved_patterns(tmp_path/"absent.json")
    assert empty["status"] == "UNKNOWN_NO_CORPUS_STATE"
    assert empty["real_measured_source_video_samples"] == 0
    assert all(p["constructed_case_success_fraction"] is None
               for p in empty["pattern_results"].values())
    make_state(tmp_path/"state.json")
    existing = report_saved_source_controls(tmp_path/"state.json")
    audit = existing["constructed_pattern_audit"]
    assert audit["status"] == "UNKNOWN_NO_REAL_VIDEO_PATTERN_SAMPLES"
    assert audit["real_measured_source_video_samples"] == 0
    assert audit["editor_cut_recall"] is None


def test_cross_clip_reuse_of_same_original_media_is_deduplicated(tmp_path):
    history = recorded(tmp_path)
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    original_scout = state.items["candidate"].source_scout
    original_scout["real_video_pattern_history"] = history
    other_clip_url = "https://www.youtube.com/watch?v=DDDDDDDDDDD"
    copy_history = copy.deepcopy(history)
    copy_history[0]["clip_id"] = "youtube:DDDDDDDDDDD"
    duplicate_scout = copy.deepcopy(original_scout)
    duplicate_scout["real_video_pattern_history"] = copy_history
    state.items["another"] = CorpusGrowthItem(
        clip_url=other_clip_url, query="fixture",
        status="source_visual_pending",
        source_scout=duplicate_scout,
    )
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    result = report_saved_patterns(tmp_path/"state.json")
    assert result["source_visual_pending_clips"] == 2
    assert result["real_measured_source_video_samples"] == 1
    assert result["deduplicated_cross_clip_repeat_samples"] == 1
    assert result["distinct_candidate_source_streams_measured"] == 1


@pytest.mark.parametrize("mode", [
    "promoted", "fake_human_gold", "case_inflation", "unsupported_pattern",
    "source_changed", "tampered_source_sha", "wrong_expected",
])
def test_forged_pattern_history_is_blocked_before_source_media_acquisition(tmp_path, mode):
    history = recorded(tmp_path)
    control = history[0]["result"]
    if mode == "promoted":
        control["qualification_allowed"] = True
    elif mode == "fake_human_gold":
        control["official_hololive_source_verified"] = True
    elif mode == "case_inflation":
        control["patterns"]["reversed_time"]["behaved_as_constructed"] = False
    elif mode == "unsupported_pattern":
        control["patterns"]["invented_editor"] = {}
    elif mode == "source_changed":
        history[0]["source_stream_id"] = "unexpected"
    elif mode == "tampered_source_sha":
        history[0]["bounded_source_media_sha256"] = "0"*64
    else:
        control["patterns"]["jpeg_overlay"]["expected"] = "constructed_temporal_decoy"
    with pytest.raises(ValueError):
        checked_pattern_history(
            history, clip_id=CLIP, candidate_ids=[STREAM],
        )
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    state.items["candidate"].source_scout["real_video_pattern_history"] = history
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    provider = Provider()
    status = verify(provider, tmp_path)
    assert status["counts"] == {
        "BLOCKED_INVALID_REAL_VIDEO_PATTERN_HISTORY": 1,
    }
    assert provider.calls == []


def test_unknown_pattern_video_hash_never_adds_history(tmp_path):
    measured = _window(tmp_path)
    measured.media_sha256 = "f"*64
    report = measure_video_pattern_controls(
        measured, root=tmp_path/"controls",
    )
    assert report["status"] == "UNKNOWN_MEASURED_VIDEO_HASH_OR_BUDGET"
    history, added = add_pattern_result(
        [], clip_id=CLIP, candidate_ids=[STREAM],
        source_id=STREAM, result=report,
    )
    assert history == []
    assert added is False
