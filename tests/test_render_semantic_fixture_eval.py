"""Offline synthetic narrative omission and source-selection presence safeguards."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_same_source_geometry as geometry
from holoclip import render_same_source_semantic_review as semantic
from holoclip import render_same_source_semantic_audit as audit
from holoclip import render_semantic_fixture_eval as fixture
from holoclip.render_same_source_selection_presence import (
    parse_generated_intervals, source_span_coverage,
)
from test_render_same_source_semantic_review import _case
from test_render_same_source_semantic_audit import _saved


def test_built_in_narrative_role_span_fixtures_and_known_limits():
    report = fixture.evaluate_synthetic_semantic_fixtures()
    assert report["status"] == "LOCAL_SYNTHETIC_SOURCE_CLOCK_CONTRACT_PASSED"
    assert report["fixture_count"] == 9
    assert report["synthetic_oracle_span_checks"] == 45
    assert report["synthetic_oracle_checks_passed"] == 45
    assert all(case["source_clock_contract_passed"] for case in report["fixtures"])
    assert report["guardrails"]["fabricated_source_quote_rejected"] is True
    assert report["guardrails"][
        "wrong_role_but_valid_quote_remains_semantically_unverified"
    ] is True
    assert report["guardrails"]["omitted_payoff_source_clock_not_selected"] is True
    assert report["guardrails"]["independent_semantic_role_accuracy_measured"] is False
    assert report["semantic_human_quality_precision"] is None
    assert report["semantic_human_quality_recall"] is None
    assert report["ready"] is False


def test_synthetic_case_names_and_omission_oracle_are_explicit():
    result = fixture.evaluate_synthetic_semantic_fixtures()
    roles = {case["fixture"]: case["role_span_checks"] for case in result["fixtures"]}
    assert roles["setup_omitted"]["setup"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert roles["development_omitted"]["development"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert roles["payoff_omitted"]["payoff"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert roles["reaction_omitted"]["reaction"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert roles["context_omitted"]["context"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert roles["payoff_clipped"]["payoff"][
        "observed_source_clock_selection"] == "SOURCE_CLOCK_PARTIALLY_SELECTED"


def test_replay_does_not_multiply_source_coverage_and_out_of_order_remains_full():
    expected = source_span_coverage(30, 34, [(30, 34)])
    repeated = source_span_coverage(30, 34, [(30, 32), (30, 34), (31, 34)])
    assert expected == repeated
    assert repeated["source_clock_selected_seconds"] == 4
    out_of_order = source_span_coverage(10, 14, [(30, 38), (10, 14)])
    assert out_of_order["source_clock_selection_status"] == (
        "SOURCE_CLOCK_FULLY_SELECTED"
    )


@pytest.mark.parametrize("cuts,start,end,expected", [
    ([(10, 12), (12, 15)], 11, 14, "SOURCE_CLOCK_FULLY_SELECTED"),
    ([(10, 11), (13, 15)], 10, 15, "SOURCE_CLOCK_PARTIALLY_SELECTED"),
    ([(10, 12)], 11, 13, "SOURCE_CLOCK_PARTIALLY_SELECTED"),
    ([(10, 12)], 12, 13, "SOURCE_CLOCK_NOT_SELECTED"),
    ([(20, 21), (10, 14)], 30, 33, "SOURCE_CLOCK_NOT_SELECTED"),
])
def test_coverage_classification_from_union_of_real_source_intervals(
    cuts, start, end, expected,
):
    assert source_span_coverage(start, end, cuts)[
        "source_clock_selection_status"
    ] == expected


@pytest.mark.parametrize("intervals", [
    [], [("10", 12)], [(13, 12)], [(float("nan"), 12)],
    [(10, float("inf"))], [(True, 12)], [(0, 86401)], [(10, 12, 13)],
])
def test_invalid_generated_intervals_fail_closed(intervals):
    with pytest.raises((ValueError, TypeError)):
        source_span_coverage(10, 12, intervals)


def test_parse_generated_source_intervals_rejects_injected_metadata():
    with pytest.raises(ValueError):
        parse_generated_intervals([{
            "source_start_seconds": 10,
            "source_end_seconds": 12,
            "creative_quality_approved": True,
        }])
    with pytest.raises(ValueError):
        parse_generated_intervals([{
            "source_start_seconds": True,
            "source_end_seconds": 12,
        }])


def test_real_same_source_jobs_include_actual_ordered_generated_intervals(tmp_path):
    params, *_ = _case(tmp_path)
    report = geometry.compare_same_source_edit_geometry(**params)
    item = report["inspection_queue"]["results"][0]
    assert item["generated_source_intervals"] == [
        {"source_start_seconds": 10, "source_end_seconds": 13},
        {"source_start_seconds": 20, "source_end_seconds": 23},
    ]
    assert item["evaluation_split"] == "TRAINING_REFERENCE_IN_SAMPLE"
    assert report["ready"] is False


def test_prepared_model_evidence_distinguishes_source_window_side_from_presence(
    tmp_path,
):
    params, *_ = _case(tmp_path)
    base = geometry.compare_same_source_edit_geometry(**params)
    prepared = semantic.prepare_semantic_evidence(base)
    assert prepared["status"] == "LOCAL_SEMANTIC_EVIDENCE_PREPARED"
    refs = prepared["cases"][0]["source_timed_unverified_asr"]
    assert all(x["source_clock_coverage_not_audible_speech_verification"]
               for x in refs)
    assert all("source_clock_selected_seconds" in x for x in refs)
    assert any(x["source_clock_selection_status"] ==
               "SOURCE_CLOCK_FULLY_SELECTED" for x in refs)
    assert any(x["source_clock_selection_status"] ==
               "SOURCE_CLOCK_NOT_SELECTED" for x in refs)
    assert prepared["cases"][0][
        "source_selection_sides_are_not_presence_labels"
    ] is True


def test_model_can_cite_unselected_source_excerpt_but_never_claim_presence(
    tmp_path,
):
    params, *_ = _case(tmp_path)
    prepared = semantic.prepare_semantic_evidence(
        geometry.compare_same_source_edit_geometry(**params)
    )
    case = prepared["cases"][0]
    item = next(x for x in case["source_timed_unverified_asr"]
                if x["source_clock_selection_status"] ==
                "SOURCE_CLOCK_NOT_SELECTED")
    answer = {"roles": [
        {"role": "setup", "status": "POSSIBLE_TEXTUAL_REFERENCE",
         "evidence_id": item["id"], "quote": item["unverified_asr_text"]},
        {"role": "development", "status": "INSUFFICIENT_EVIDENCE",
         "evidence_id": None, "quote": None},
        {"role": "payoff", "status": "INSUFFICIENT_EVIDENCE",
         "evidence_id": None, "quote": None},
    ]}
    role = semantic._validate_candidate(answer, case)[0]
    assert role["source_clock_selection_status"] == "SOURCE_CLOCK_NOT_SELECTED"
    assert role["source_clock_selected_seconds"] == 0
    assert role["actual_audible_speech_or_story_role_verified"] is False


@pytest.mark.parametrize("tamper", ["selection_status", "selected_seconds", "audibility"])
def test_offline_audit_rejects_selection_presence_tampering(tmp_path, tamper):
    params, *_ = _case(tmp_path)
    saved, fresh = _saved(params)
    role = saved["hypotheses"][0]["role_hypotheses"][0]
    if tamper == "selection_status":
        role["source_clock_selection_status"] = "SOURCE_CLOCK_FULLY_SELECTED" \
            if role["source_clock_selection_status"] != \
            "SOURCE_CLOCK_FULLY_SELECTED" else "SOURCE_CLOCK_NOT_SELECTED"
    elif tamper == "selected_seconds":
        role["source_clock_selected_seconds"] += 1.0
    else:
        role["actual_audible_speech_or_story_role_verified"] = True
    result = audit.audit_semantic_receipt(saved, fresh)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert "CITATION_NOT_REPRODUCIBLE" in result["rejection_reasons"]


def test_changed_generated_timeline_invalidates_old_evidence_receipt(tmp_path):
    params, *_ = _case(tmp_path)
    old, old_prepared = _saved(params)
    modified = geometry.compare_same_source_edit_geometry(**params)
    item = modified["inspection_queue"]["results"][0]
    item["generated_source_intervals"] = [
        {"source_start_seconds": 30, "source_end_seconds": 34},
    ]
    # The edited selection does not alter the quote text, but it must change
    # the source-clock presence metadata and therefore the evidence hash.
    updated = semantic.prepare_semantic_evidence(modified)
    assert semantic.prepared_fingerprint(updated) != (
        semantic.prepared_fingerprint(old_prepared)
    )
    assert audit.audit_semantic_receipt(old, updated)["status"] == (
        "LOCAL_SEMANTIC_AUDIT_REJECTED"
    )


def test_fixture_cli_is_offline_deterministic_and_never_uses_provider(monkeypatch):
    class MustNotUse:
        def __init__(self, *args, **kwargs):
            raise AssertionError("No provider in synthetic fixture evaluation")
    monkeypatch.setattr(cli, "MeshLLMClient", MustNotUse)
    runner = CliRunner()
    left = runner.invoke(cli.app, ["render-semantic-fixture-eval"])
    right = runner.invoke(cli.app, ["render-semantic-fixture-eval"])
    assert left.exit_code == 0, left.stdout
    assert left.stdout == right.stdout
    output = json.loads(left.stdout)
    assert output["fixture_count"] == 9
    assert output["real_video_validation_complete"] is False


def test_holdout_exclusion_suppresses_source_selections_and_model_cases(tmp_path):
    params, *_ = _case(tmp_path, holdout=True)
    result = geometry.compare_same_source_edit_geometry(**params)
    assert result["inspection_queue"]["results"] == []
    prepared = semantic.prepare_semantic_evidence(result)
    assert prepared["cases"] == []
    assert prepared["status"] == "LOCAL_SEMANTIC_GATES_NOT_VERIFIED"


def test_narrative_wrong_role_remains_known_blind_spot_even_with_valid_quote():
    report = fixture.evaluate_synthetic_semantic_fixtures()
    assert report["guardrails"][
        "wrong_role_but_valid_quote_remains_semantically_unverified"
    ] is True
    assert report["semantic_human_quality_precision"] is None
    assert report["real_video_validation_complete"] is False
