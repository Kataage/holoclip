"""Only actual bounded acquired measured-video constructed controls count."""
import json

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.source_visual_control_report import (
    VERSION, add_control_result, checked_control_history,
    report_saved_source_controls, summarize_control_history,
)
from holoclip.source_visual_real_controls import measure_source_video_controls
from test_source_candidate_visual import Provider, make_state, verify
from test_source_visual_real_controls import _window


CID = "youtube:AAAAAAAAAAA"
SOURCE = "BBBBBBBBBBB"


def _record(tmp_path):
    window = _window(tmp_path)
    result = measure_source_video_controls(
        window, root=tmp_path/"local-reencoding",
    )
    assert result["status"] == "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED"
    saved, appended = add_control_result(
        [], clip_id=CID, candidate_ids=[SOURCE],
        source_id=SOURCE, control=result,
    )
    assert appended is True
    return saved


def test_real_video_control_history_counts_one_sample_not_editor_accuracy(tmp_path):
    saved = _record(tmp_path)
    again, inserted = add_control_result(
        saved, clip_id=CID, candidate_ids=[SOURCE],
        source_id=SOURCE, control=saved[0]["control"],
    )
    assert inserted is False
    assert again == saved
    summary = summarize_control_history(saved)
    assert summary["version"] == VERSION
    assert summary["real_measured_video_control_samples"] == 1
    assert summary["constructed_jpeg_positive_detected"] == 1
    assert summary["constructed_reversed_sequence_rejected"] == 1
    assert summary["constructed_positive_detection_fraction"] == 1
    assert summary["constructed_reversed_sequence_rejection_fraction"] == 1
    assert summary["precision_recall_estimable"] is False
    assert summary["real_source_match_false_positive_rate"] is None
    assert summary["editor_cut_precision"] is None
    assert summary["reviewed_gold_available"] is False


def test_missing_real_source_video_reports_zero_and_null_rates(tmp_path):
    summary = report_saved_source_controls(tmp_path/"missing.json")
    assert summary["status"] == "UNKNOWN_NO_CORPUS_STATE"
    assert summary["saved_source_state_checked"] is False
    assert summary["real_measured_video_control_samples"] == 0
    assert summary["constructed_positive_detection_fraction"] is None
    assert summary["editor_cut_recall"] is None

    make_state(tmp_path/"state.json")
    report = report_saved_source_controls(tmp_path/"state.json")
    assert report["status"] == "UNKNOWN_NO_REAL_VIDEO_CONTROL_SAMPLES"
    assert report["pending_clip_count"] == 1
    assert report["constructed_positive_detection_fraction"] is None


def test_saved_auto_source_video_measurement_can_be_reported_without_user_data(tmp_path):
    history = _record(tmp_path)
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    state.items["candidate"].source_scout["real_video_control_history"] = history
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    report = report_saved_source_controls(tmp_path/"state.json")
    assert report["status"] == "MEASURED_CONSTRUCTED_CONTROLS_NOT_EDITOR_ACCURACY"
    assert report["saved_source_state_checked"] is True
    assert report["real_measured_video_control_samples"] == 1
    assert report["source_identity_verified"] is False
    assert report["source_provenance_kind"] == (
        "YT_DLP_BOUNDED_PUBLIC_VIDEO_UNREVIEWED"
    )


@pytest.mark.parametrize("alteration", [
    "forged_gold", "forged_precision", "new_source",
    "bad_sample_checksum", "control_count", "duplicate",
    "pretend_hololive_verified",
])
def test_tampered_or_duplicate_control_history_blocks_source_preflight(tmp_path, alteration):
    history = _record(tmp_path)
    if alteration == "forged_gold":
        history[0]["control"]["reviewed_gold_available"] = True
    elif alteration == "forged_precision":
        history[0]["control"]["editor_cut_precision"] = .9
    elif alteration == "new_source":
        history[0]["source_stream_id"] = "imposter"
    elif alteration == "bad_sample_checksum":
        history[0]["measured_source_video_sha256"] = "0"*64
    elif alteration == "control_count":
        history[0]["control"]["observed_control_pairs"] = 999
    elif alteration == "duplicate":
        history.append(dict(history[0]))
    else:
        history[0]["control"]["source_video_is_official_hololive_verified"] = True

    with pytest.raises(ValueError):
        checked_control_history(history, clip_id=CID, candidate_ids=[SOURCE])

    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    state.items["candidate"].source_scout["real_video_control_history"] = history
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    provider = Provider()
    observation = verify(provider, tmp_path)
    assert observation["counts"] == {
        "BLOCKED_INVALID_REAL_VIDEO_CONTROL_HISTORY": 1
    }
    assert provider.calls == []


def test_unknown_video_control_does_not_increment_real_measured_count():
    history, saved = add_control_result(
        [], clip_id=CID, candidate_ids=[SOURCE], source_id=SOURCE,
        control={
            "status": "UNKNOWN_SOURCE_VIDEO_HASH_OR_BUDGET",
            "version": "measured-public-video-self-controls-v1",
        },
    )
    assert saved is False
    assert history == []
    assert summarize_control_history(history)[
        "real_measured_video_control_samples"
    ] == 0
