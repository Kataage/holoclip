"""Unreviewed cut statistics must never masquerade as editor precision/recall."""
import json

import pytest

from holoclip.holdout_e2e_trial import summarize_observed_evidence
from holoclip.real_cut_observations import (
    VERSION, aggregate_real_cut_observations, describe_real_cut_observations,
)


def cut_row(*, transitions=None, visual=None):
    changes = transitions if transitions is not None else [
        {"status": "CUT_INTERVAL_CANDIDATE", "interval": [15., 17.],
         "offset_change_seconds": 9.},
        {"status": "UNKNOWN_WIDE_GAP", "interval": [20., 30.]},
        {"status": "CUT_INTERVAL_CANDIDATE", "interval": [30., 40.],
         "offset_change_seconds": 18.},
    ]
    if visual is None:
        visual = [
            {
                "acoustic_interval": [15., 17.],
                "acoustic_offset_change_seconds": 9.,
                "relation": "CORROBORATING_OUTPUT_CHANGE",
                "verified_edit_cut": False, "native_frame_pts_verified": False,
                "source_visual_transition_ruled_out": False,
                "visual_result": {
                    "version": "bounded-output-visual-jumps-v1",
                    "status": "VISUAL_JUMP_CANDIDATES",
                },
            },
            {
                "acoustic_interval": [30., 40.],
                "acoustic_offset_change_seconds": 18.,
                "relation": "UNRESOLVED_VISUAL_EVIDENCE",
                "verified_edit_cut": False, "native_frame_pts_verified": False,
                "source_visual_transition_ruled_out": False,
                "visual_result": {
                    "version": "bounded-output-visual-jumps-v1",
                    "status": "UNKNOWN_BRACKET_TOO_WIDE",
                },
            },
        ]
    return {
        "status": "MEASURED_UNREVIEWED_CUT_BRACKETS",
        "clip_id": "youtube:aaaaaaaaaaa", "source_stream_id": "source_A",
        "audio_source_sha256": "a" * 64,
        "finished_media_sha256": "b" * 64,
        "qualification_allowed": False, "reviewed_gold_available": False,
        "event_split_verified": False, "frame_accurate_cuts_verified": False,
        "refinement": {
            "version": "bounded-acoustic-cut-brackets-v2",
            "status": "AUDIO_SUPPORTED_INTERVALS", "transitions": changes,
        },
        "output_visual_cut_evidence": visual,
    }


def side_row():
    return {
        "status": "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE",
        "clip_id": "youtube:aaaaaaaaaaa", "source_stream_id": "source_A",
        "audio_source_sha256": "a" * 64,
        "finished_media_sha256": "b" * 64,
        "qualification_allowed": False, "reviewed_gold_available": False,
        "event_split_verified": False, "frame_accurate_cuts_verified": False,
        "transitions": [
            {
                "acoustic_interval": [15., 17.], "editorial_gold": False,
                "candidate": {"status": "MULTIMODAL_EDIT_JUMP_CANDIDATE",
                              "verified_edit_cut": False},
            },
            {
                "acoustic_interval": [30., 40.], "editorial_gold": False,
                "candidate": {"status": "UNKNOWN_SOURCE_CONTINUITY",
                              "verified_edit_cut": False},
            },
        ],
    }


def test_real_cut_descriptive_measurements_are_not_human_gold():
    result = describe_real_cut_observations(cut_row(), side_row())
    assert result["version"] == VERSION
    assert result["status"] == "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS"
    assert result["cut_candidate_count"] == 2
    assert result["unknown_audio_transition_count"] == 1
    assert result["unknown_audio_transition_statuses"] == {"UNKNOWN_WIDE_GAP": 1}
    assert result["overbroad_candidate_count"] == 1
    assert result["candidate_median_width_seconds"] == 6.
    assert result["candidate_p90_width_seconds"] == 10.
    assert result["output_visual_corroborating_candidates"] == 1
    assert result["two_sided_multimodal_candidates"] == 1
    assert result["two_sided_unresolved_candidates"] == 1
    assert result["two_sided_status"] == "MEASURED_UNREVIEWED"
    for key in ("ready", "qualification_allowed", "reviewed_gold_available",
                "frame_accurate_cuts_verified", "event_split_verified"):
        assert result[key] is False
    assert result["editor_cut_precision"] is None
    assert result["editor_cut_recall"] is None


def test_missing_side_is_unknown_not_zero_and_aggregation_retains_null():
    r = describe_real_cut_observations(cut_row())
    assert r["two_sided_multimodal_candidates"] is None
    summary = aggregate_real_cut_observations({"a": r})
    assert summary["cut_candidates"] == 2
    assert summary["overbroad_candidates"] == 1
    assert summary["two_sided_multimodal_candidates"] is None
    assert summary["editor_cut_precision"] is None


@pytest.mark.parametrize("field,value", [
    ("clip_id", "youtube:different"),
    ("source_stream_id", "different"),
    ("audio_source_sha256", "c" * 64),
    ("finished_media_sha256", "d" * 64),
    ("reviewed_gold_available", True),
])
def test_foreign_or_promoted_side_never_corrobates(field, value):
    side = side_row()
    side[field] = value
    result = describe_real_cut_observations(cut_row(), side)
    assert result["two_sided_status"] == "UNVERIFIED_CROSS_STAGE_PROVENANCE"
    assert result["two_sided_multimodal_candidates"] is None


def test_reordered_or_promoted_side_is_not_verified():
    side = side_row()
    side["transitions"].reverse()
    result = describe_real_cut_observations(cut_row(), side)
    assert result["two_sided_status"] == "UNVERIFIED_SIDE_INTERVAL_LINKS"
    side = side_row()
    side["transitions"][0]["candidate"]["verified_edit_cut"] = True
    assert describe_real_cut_observations(
        cut_row(), side,
    )["two_sided_multimodal_candidates"] is None


def test_forged_output_visual_label_without_corresponding_measured_status_is_unknown():
    row = cut_row()
    row["output_visual_cut_evidence"][0]["visual_result"]["status"] = (
        "UNKNOWN_NO_ISOLATED_JUMP"
    )
    result = describe_real_cut_observations(row, side_row())
    assert result["output_visual_corroborating_candidates"] is None
    # Source-side evidence is scored as its own observation, not a gold label.
    assert result["two_sided_multimodal_candidates"] == 1


def test_budget_and_missing_output_visual_are_not_zero():
    side = side_row()
    side["transitions"] = [{"status": "UNKNOWN_TRANSITION_BUDGET",
                            "unresolved": 2}]
    result = describe_real_cut_observations(
        cut_row(visual=[{"status": "UNKNOWN_VISUAL_BRACKET_BUDGET"}]), side,
    )
    assert result["two_sided_status"] == "UNKNOWN_TRANSITION_BUDGET"
    assert result["two_sided_multimodal_candidates"] is None
    assert result["output_visual_corroborating_candidates"] is None


def test_zero_cut_candidates_are_not_perfect_scores():
    empty = cut_row(transitions=[], visual=[])
    side = side_row()
    side["transitions"] = []
    row = describe_real_cut_observations(empty, side)
    assert row["cut_candidate_count"] == 0
    assert row["output_visual_corroborating_candidates"] == 0
    assert row["two_sided_multimodal_candidates"] == 0
    assert row["candidate_median_width_seconds"] is None
    assert row["editor_cut_precision"] is None


@pytest.mark.parametrize("transition", [
    {"status": "CUT_INTERVAL_CANDIDATE", "interval": [-1., 3.]},
    {"status": "CUT_INTERVAL_CANDIDATE", "interval": [20., 10.]},
    {"status": "CUT_INTERVAL_CANDIDATE", "interval": [1., float("inf")]},
    {"status": "CUT_INTERVAL_CANDIDATE", "interval": [0., True]},
    {"status": "HUMAN_GOLD_CUT", "interval": [1., 3.]},
])
def test_malformed_or_promoted_transition_fails_closed(transition):
    with pytest.raises(ValueError):
        describe_real_cut_observations(cut_row(
            transitions=[transition], visual=[],
        ))


@pytest.mark.parametrize("status", [
    "UNKNOWN_NO_UNIQUE_POINTS", "UNKNOWN_SAMPLING_BUDGET",
    "UNKNOWN_NO_SUPPORTED_RANGES",
])
def test_unsupported_audio_does_not_turn_unknown_into_zero(status):
    cut = cut_row(transitions=[], visual=[])
    cut["refinement"]["status"] = status
    result = describe_real_cut_observations(cut)
    assert result["status"] == "UNKNOWN_ACOUSTIC_SUPPORT"
    assert result["measurement_status"] == status
    assert result["cut_candidate_count"] is None
    assert result["unknown_audio_transition_count"] is None
    assert result["output_visual_corroborating_candidates"] is None
    assert aggregate_real_cut_observations({"one": result})[
        "unverified_clips"
    ] == 1


def test_mismatched_cut_method_and_claimed_gold_are_not_measured():
    row = cut_row()
    row["refinement"]["version"] = "old-method"
    result = describe_real_cut_observations(row)
    assert result["status"] == "UNVERIFIED_CUT_MEASUREMENT"
    assert result["cut_candidate_count"] is None
    row = cut_row()
    row["reviewed_gold_available"] = True
    assert describe_real_cut_observations(row)["cut_candidate_count"] is None


def test_stage_summary_does_not_use_unexecuted_cached_cut_or_side(tmp_path):
    clip_id = "youtube:aaaaaaaaaaa"
    for name, version, row in (
        ("cut_brackets/holdout-cut-brackets.json",
         "reserved-acoustic-cut-brackets-v1", cut_row()),
        ("cut_sides/holdout-cut-side-evidence.json",
         "reserved-multimodal-cut-side-evidence-v1", side_row()),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": version, "records": {clip_id: row},
            "qualification_allowed": False, "reviewed_gold_available": False,
            "event_split_verified": False, "benchmark_held_out": False,
        }), encoding="utf-8")
    only_cut = summarize_observed_evidence(
        tmp_path, {clip_id}, completed_stages={"acoustic_cut_brackets"},
    )
    result = only_cut["real_cut_observations"][clip_id]
    assert result["cut_candidate_count"] == 2
    assert result["two_sided_multimodal_candidates"] is None
    complete = summarize_observed_evidence(
        tmp_path, {clip_id},
        completed_stages={"acoustic_cut_brackets", "two_sided_cut_evidence"},
    )
    assert complete["real_cut_observations"][clip_id][
        "two_sided_multimodal_candidates"
    ] == 1
    assert complete["real_cut_observation_totals"][
        "two_sided_multimodal_candidates"
    ] == 1
    none = summarize_observed_evidence(
        tmp_path, {clip_id}, completed_stages=set(),
    )
    assert none["real_cut_observations"] == {}
    assert none["real_cut_observation_totals"]["described_clips"] == 0


def test_multi_batch_persists_all_distinct_real_cut_descriptions(tmp_path):
    """A later bounded batch must not replace earlier clip observations."""
    from holoclip.holdout_e2e_trial import run_all_reserved_holdout_batches
    from test_holdout_e2e_trial import add_reserved_clips, make_runners
    from test_holdout_preparation import files

    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    runners = make_runners(root)
    cut_runner = runners["acoustic_cut_brackets"]
    side_runner = runners["two_sided_cut_evidence"]

    def cut(provider, **kwargs):
        answer = cut_runner(provider, **kwargs)
        path = root / "cut_brackets" / "holdout-cut-brackets.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for clip_id, record in doc["records"].items():
            if record["status"] == "MEASURED_UNREVIEWED_CUT_BRACKETS":
                record.update({
                    "clip_id": clip_id,
                    "audio_source_sha256": "a" * 64,
                    "finished_media_sha256": "b" * 64,
                    "refinement": {
                        "version": "bounded-acoustic-cut-brackets-v2",
                        "status": "AUDIO_SUPPORTED_INTERVALS",
                        "transitions": [{
                            "status": "CUT_INTERVAL_CANDIDATE",
                            "interval": [15., 17.],
                            "offset_change_seconds": 9.,
                        }],
                    },
                    "output_visual_cut_evidence": [{
                        "acoustic_interval": [15., 17.],
                        "acoustic_offset_change_seconds": 9.,
                        "relation": "CORROBORATING_OUTPUT_CHANGE",
                        "verified_edit_cut": False,
                        "native_frame_pts_verified": False,
                        "source_visual_transition_ruled_out": False,
                        "visual_result": {
                            "version": "bounded-output-visual-jumps-v1",
                            "status": "VISUAL_JUMP_CANDIDATES",
                        },
                    }],
                    "reviewed_gold_available": False,
                    "event_split_verified": False,
                    "frame_accurate_cuts_verified": False,
                })
        path.write_text(json.dumps(doc), encoding="utf-8")
        return answer

    def sides(provider, **kwargs):
        answer = side_runner(provider, **kwargs)
        path = root / "cut_sides" / "holdout-cut-side-evidence.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for clip_id, record in doc["records"].items():
            if record["status"] == "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE":
                record.update({
                    "clip_id": clip_id,
                    "audio_source_sha256": "a" * 64,
                    "finished_media_sha256": "b" * 64,
                    "reviewed_gold_available": False,
                    "event_split_verified": False,
                    "frame_accurate_cuts_verified": False,
                    "transitions": [{
                        "acoustic_interval": [15., 17.],
                        "editorial_gold": False,
                        "candidate": {
                            "status": "MULTIMODAL_EDIT_JUMP_CANDIDATE",
                            "verified_edit_cut": False,
                        },
                    }],
                })
        path.write_text(json.dumps(doc), encoding="utf-8")
        return answer

    runners["acoustic_cut_brackets"] = cut
    runners["two_sided_cut_evidence"] = sides
    result = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=2, runners=runners,
    )
    assert result["status"] == "SIMULATED_ALL_BATCHES_COMPLETE"
    assert result["real_media_run_completed"] is False
    assert len(result["real_cut_observations"]) == 3
    assert len(result["batches"]) == 2
    assert sum(len(batch["real_cut_observations"]) for batch in
               result["batches"]) == 3
    assert result["real_cut_observation_totals"]["described_clips"] == 3
    assert result["real_cut_observation_totals"]["cut_candidates"] == 3
    assert result["real_cut_observation_totals"][
        "two_sided_multimodal_candidates"
    ] == 3
    assert result["real_cut_observation_totals"]["editor_cut_precision"] is None
    persisted = json.loads(
        (root / "holdout-e2e-all-batches.json").read_text(encoding="utf-8")
    )
    assert persisted["real_cut_observation_totals"] == result[
        "real_cut_observation_totals"
    ]


def test_visual_only_signal_survives_unknown_acoustic_support():
    cut = cut_row(transitions=[], visual=[])
    cut["refinement"]["status"] = "UNKNOWN_SAMPLING_BUDGET"
    cut["visual_survey_method_version"] = "bounded-output-only-visual-survey-v1"
    cut["independent_output_visual_survey"] = {
        "version": "bounded-output-only-visual-survey-v1",
        "status": "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS",
        "source": "FINISHED_OUTPUT_ONLY_UNREVIEWED",
        "duration_seconds": 12., "sampled_coverage_seconds": 12.,
        "unsampled_duration_seconds": 0.,
        "sampled_windows": [[0., 6.], [2., 8.], [6., 12.]],
        "observed_visual_peaks": 1,
        "without_acoustic_candidate": 1,
        "observed_peaks": [{
            "time_bracket_seconds": [5.95, 6.05],
            "relation": "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
            "verified_edit_cut": False,
            "source_scene_change_ruled_out": False,
        }],
        "verified_edit_cut": False,
        "reviewed_gold_available": False,
        "frame_accurate_cuts_verified": False,
        "qualification_allowed": False,
    }
    result = describe_real_cut_observations(cut)
    assert result["status"] == "UNKNOWN_ACOUSTIC_SUPPORT"
    assert result["cut_candidate_count"] is None
    assert result["output_only_visual_change_count"] == 1
    assert result["output_only_visual_without_acoustic_count"] == 1
    assert result["output_only_visual_coverage_seconds"] == 12.
    summary = aggregate_real_cut_observations({"clip": result})
    assert summary["described_clips"] == 0
    assert summary["clips_with_measured_output_only_visual_survey"] == 1
    assert summary["output_only_visual_without_acoustic_candidates"] == 1
    assert summary["editor_cut_precision"] is None

    forged = cut.copy()
    forged["independent_output_visual_survey"] = {
        **cut["independent_output_visual_survey"],
        "verified_edit_cut": True,
    }
    import pytest
    with pytest.raises(ValueError):
        describe_real_cut_observations(forged)

    forged = cut.copy()
    forged["independent_output_visual_survey"] = {
        **cut["independent_output_visual_survey"],
        "sampled_windows": [[0., 3.]],
    }
    with pytest.raises(ValueError):
        describe_real_cut_observations(forged)


def test_unknown_visual_decode_keeps_unmeasured_counts_null():
    cut = cut_row(transitions=[], visual=[])
    cut["visual_survey_method_version"] = "bounded-output-only-visual-survey-v1"
    cut["independent_output_visual_survey"] = {
        "version": "bounded-output-only-visual-survey-v1",
        "status": "UNKNOWN_BOUNDED_VISUAL_DECODE",
        "source": "FINISHED_OUTPUT_ONLY_UNREVIEWED",
        "duration_seconds": 12., "sampled_coverage_seconds": 0.,
        "unsampled_duration_seconds": 12.,
        "sampled_windows": [],
        "observed_visual_peaks": None, "without_acoustic_candidate": None,
        "verified_edit_cut": False,
        "reviewed_gold_available": False,
        "frame_accurate_cuts_verified": False,
        "qualification_allowed": False,
    }
    result = describe_real_cut_observations(cut)
    assert result["output_only_visual_status"] == "UNKNOWN_BOUNDED_VISUAL_DECODE"
    assert result["output_only_visual_change_count"] is None
    assert result["output_only_visual_without_acoustic_count"] is None
