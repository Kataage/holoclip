"""Deterministic synthetic FFmpeg scripted cut metrics, never human reviewer gold."""
import json

import pytest
from typer.testing import CliRunner

from holoclip.cli import app
from holoclip.scripted_cut_metrics import (
    REFERENCE_VERSION, SCORE_VERSION, score_scripted_cut_intervals,
    score_scripted_cut_report, score_scripted_cut_files,
)


def _cut(lo, hi, status="CUT_INTERVAL_CANDIDATE"):
    return {"status": status, "interval": [lo, hi]}


def _reference(cuts=None):
    return {
        "version": REFERENCE_VERSION, "reference_kind": "SCRIPTED_FFMPEG_ONLY",
        "clip_id": "youtube:aaaaaaaaaaa", "duration_seconds": 48.,
        "cut_seconds": cuts if cuts is not None else [16., 32.],
    }


def _report(transitions):
    return {
        "version": "reserved-acoustic-cut-brackets-v1",
        "qualification_allowed": False, "reviewed_gold_available": False,
        "benchmark_held_out": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False,
        "records": {
            "youtube:aaaaaaaaaaa": {
                "clip_id": "youtube:aaaaaaaaaaa", "status": "MEASURED_UNREVIEWED_CUT_BRACKETS",
                "frame_accurate_cuts_verified": False,
                "refinement": {
                    "version": "bounded-acoustic-cut-brackets-v2",
                    "status": "AUDIO_SUPPORTED_INTERVALS", "transitions": transitions,
                },
            },
        },
    }


def test_two_scripted_cuts_one_extra_candidate_reports_real_fp_and_midpoint_error():
    result = score_scripted_cut_report(
        _reference(), _report([_cut(15, 17), _cut(31.5, 33.),
                               _cut(40, 41), _cut(5, 10, "UNKNOWN_WIDE_GAP")]),
    )
    assert result["version"] == SCORE_VERSION
    assert result["status"] == "SCRIPTED_CONSTRUCTION_METRICS_ONLY"
    assert result["scripted_cut_count"] == 2
    assert result["cut_candidate_count"] == 3
    assert result["true_positive_candidates"] == 2
    assert result["false_positive_candidates"] == 1
    assert result["missed_scripted_cuts"] == 0
    assert result["scripted_interval_precision"] == pytest.approx(2 / 3, abs=1e-6)
    assert result["scripted_interval_recall"] == 1.
    assert result["scripted_interval_f1"] == .8
    assert result["matched_midpoint_median_absolute_error_seconds"] == .125
    assert result["matched_bracket_median_width_seconds"] == 1.75
    assert result["unknown_audio_transitions"] == 1
    assert result["unmatched_candidate_indices"] == [2]
    assert result["unmatched_scripted_cut_seconds"] == []
    assert result["ready"] is False
    assert result["reviewed_gold_available"] is False
    assert result["editor_cut_precision"] is None
    assert result["editor_cut_recall"] is None
    assert result["native_timestamp_error_seconds"] is None


def test_one_candidate_cannot_cover_two_scripted_boundaries_twice():
    result = score_scripted_cut_intervals(
        scripted_cuts=[10., 12.], transitions=[_cut(9, 13)],
        duration_seconds=30.,
    )
    assert result["true_positive_candidates"] == 1
    assert result["false_positive_candidates"] == 0
    assert result["missed_scripted_cuts"] == 1
    assert result["scripted_interval_recall"] == .5


def test_overbroad_interval_is_false_positive_not_free_cut_recall():
    result = score_scripted_cut_intervals(
        scripted_cuts=[10.], transitions=[_cut(0., 30.)],
        duration_seconds=30.,
    )
    assert result["overbroad_candidates_counted_as_false_positives"] == 1
    assert result["true_positive_candidates"] == 0
    assert result["false_positive_candidates"] == 1
    assert result["missed_scripted_cuts"] == 1
    assert result["scripted_interval_precision"] == 0.
    assert result["scripted_interval_recall"] == 0.


def test_empty_scripted_uncut_control_has_no_invented_perfect_precision():
    result = score_scripted_cut_intervals(
        scripted_cuts=[], transitions=[], duration_seconds=45.,
    )
    assert result["true_positive_candidates"] == 0
    assert result["false_positive_candidates"] == 0
    assert result["missed_scripted_cuts"] == 0
    assert result["scripted_interval_precision"] is None
    assert result["scripted_interval_recall"] is None
    assert result["scripted_interval_f1"] is None
    assert result["matched_midpoint_median_absolute_error_seconds"] is None


def test_all_cut_transitions_unknown_is_missing_truth_not_false_precision():
    result = score_scripted_cut_intervals(
        scripted_cuts=[16.], transitions=[_cut(11., 21., "UNKNOWN_WIDE_GAP")],
        duration_seconds=44.,
    )
    assert result["scripted_cut_count"] == 1
    assert result["cut_candidate_count"] == 0
    assert result["unknown_audio_transitions"] == 1
    assert result["missed_scripted_cuts"] == 1
    assert result["scripted_interval_recall"] == 0.
    assert result["scripted_interval_precision"] is None


def test_tolerance_does_not_turn_wide_brackets_into_hits():
    narrow = score_scripted_cut_intervals(
        scripted_cuts=[16.], transitions=[_cut(16.2, 17.)],
        duration_seconds=44., tolerance_seconds=.25,
    )
    assert narrow["true_positive_candidates"] == 1
    wide = score_scripted_cut_intervals(
        scripted_cuts=[16.], transitions=[_cut(15., 25.)],
        duration_seconds=44., tolerance_seconds=1.,
    )
    assert wide["missed_scripted_cuts"] == 1
    assert wide["false_positive_candidates"] == 1


@pytest.mark.parametrize("truth,transitions,seconds", [
    ([16., 16.], [], 44.),
    ([32., 16.], [], 44.),
    ([float("nan")], [], 44.),
    ([0.], [], 44.),
    ([44.], [], 44.),
    ([10.], [_cut(-.1, 15.)], 44.),
    ([10.], [_cut(20., 10.)], 44.),
    ([10.], [_cut(10., float("inf"))], 44.),
    ([10.], [{"status": "MEASURED_FRAME_ACCURATE", "interval": [9., 11.]}], 44.),
])
def test_malformed_scripted_cut_and_transition_inputs_are_rejected(
    truth, transitions, seconds,
):
    with pytest.raises(ValueError):
        score_scripted_cut_intervals(
            scripted_cuts=truth, transitions=transitions,
            duration_seconds=seconds,
        )


def test_tampered_or_unreviewed_foreign_report_cannot_score():
    for mutate in (
        lambda ref, doc: ref.update({"reference_kind": "HUMAN_REVIEWED_GOLD"}),
        lambda ref, doc: doc.update({"qualification_allowed": True}),
        lambda ref, doc: doc["records"]["youtube:aaaaaaaaaaa"].update({
            "frame_accurate_cuts_verified": True,
        }),
        lambda ref, doc: doc["records"]["youtube:aaaaaaaaaaa"].update({
            "status": "RETRYABLE_FAILURE",
        }),
        lambda ref, doc: doc["records"]["youtube:aaaaaaaaaaa"]["refinement"].update({
            "version": "unsupported-heuristic",
        }),
        lambda ref, doc: ref.update({"clip_id": "wrong-id"}),
    ):
        reference = _reference()
        report = _report([_cut(15, 17)])
        mutate(reference, report)
        with pytest.raises(ValueError):
            score_scripted_cut_report(reference, report)


def test_file_scoring_writes_isolated_metrics_and_never_overwrites_evidence(tmp_path):
    ref = tmp_path / "scripted.json"
    report = tmp_path / "cut-evidence.json"
    out = tmp_path / "scripted-score.json"
    ref.write_text(json.dumps(_reference()), encoding="utf-8")
    report.write_text(json.dumps(_report([_cut(15, 17), _cut(30, 33)])),
                      encoding="utf-8")
    before_ref, before_report = ref.read_bytes(), report.read_bytes()
    scored = score_scripted_cut_files(
        reference_path=ref, report_path=report, output_path=out,
    )
    assert scored["true_positive_candidates"] == 2
    assert scored["scripted_interval_recall"] == 1.
    assert json.loads(out.read_text(encoding="utf-8"))["ready"] is False
    assert ref.read_bytes() == before_ref
    assert report.read_bytes() == before_report
    for unsafe_path in (ref, report):
        with pytest.raises(ValueError):
            score_scripted_cut_files(
                reference_path=ref, report_path=report, output_path=unsafe_path,
            )


def test_cli_scores_scripted_only_without_media_download(tmp_path):
    reference = tmp_path / "scripted-reference.json"
    report = tmp_path / "cut-brackets.json"
    result = tmp_path / "score.json"
    reference.write_text(json.dumps(_reference()), encoding="utf-8")
    report.write_text(json.dumps(_report([_cut(15., 17.), _cut(31., 33.)])),
                      encoding="utf-8")
    runner = CliRunner()
    outcome = runner.invoke(app, [
        "examples-score-scripted-cuts",
        "--reference", str(reference),
        "--cut-report", str(report),
        "--output", str(result),
    ])
    assert outcome.exit_code == 0, outcome.output
    assert '"scripted_interval_recall": 1.0' in outcome.output
    assert '"editor_cut_precision": null' in outcome.output
    assert json.loads(result.read_text(encoding="utf-8"))["ready"] is False
    payload = json.loads(reference.read_text(encoding="utf-8"))
    payload["reference_kind"] = "REVIEWED_HUMAN_GOLD"
    reference.write_text(json.dumps(payload), encoding="utf-8")
    invalid = runner.invoke(app, [
        "examples-score-scripted-cuts",
        "--reference", str(reference),
        "--cut-report", str(report),
        "--output", str(result),
    ])
    assert invalid.exit_code == 3
    assert "BLOCKED_INVALID_SCRIPTED_CUT_EVIDENCE" in invalid.output
