"""Offline B3 single-cut extension vs unverified blinded responses."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_blind_full_study as study
from holoclip import render_b2_b3_blind_edge_association as association
from test_render_b2_b3_blind_rating_ingest import _environment


def _study(tmp_path, monkeypatch):
    original_a, original_b, root, mapping, form, rating = _environment(
        tmp_path, monkeypatch,
    )
    state = study._basis({}, 0)
    ctx = state["basis"]["context"]
    ctx["source_cut_index"] = 0
    ctx["trial_action"] = "EXTEND_START"

    def cut(i: int, lo: float, hi: float):
        return SimpleNamespace(
            source_asset_id="same-source",
            output_order=i, source_start=lo, source_end=hi,
        )

    ctx["roles"]["b2"]["timeline"] = SimpleNamespace(segments=[
        cut(0, 10, 20), cut(1, 30, 40),
    ])
    ctx["roles"]["b3"]["timeline"] = SimpleNamespace(segments=[
        cut(0, 9.5, 20), cut(1, 30, 40),
    ])
    monkeypatch.setattr(
        association, "_exclusive_run_lock", lambda *args, **kwargs: nullcontext(),
    )
    return state, form, rating


def test_paired_scores_and_real_edge_are_descriptive_not_causal(
    tmp_path, monkeypatch,
):
    state, form, rating = _study(tmp_path, monkeypatch)
    result = association.compare_blind_edge_association(
        rating_paths=[rating], preview_root=tmp_path,
    )
    assert result["status"] == "BLIND_ASSOCIATION_UNVERIFIED_OBSERVATION_ONLY"
    edge = result["changed_edit"]
    assert edge["status"] == "B3_OUTWARD_SINGLE_SOURCE_EDGE_EXTENSION"
    assert edge["b3_added_source_duration_seconds"] == .5
    assert edge["source_cut_index"] == 0
    assert edge["number_of_other_changed_cuts"] == 0
    summary = result["unverified_paired_role_score_differences"]
    order = state["basis"]["context"]["roles"]
    assert set(order) == {"b2", "b3"}
    # The private mapping decides whether score +3 for slot one means B3 +3 or -3.
    expected = 3 if study._prepare({
        "queue": association.DEFAULT_QUEUE,
        "example_store": association.DEFAULT_HUMAN_STORE,
        "growth_state": association.DEFAULT_GROWTH_STATE,
        "preview_root": tmp_path,
        "transcript_root": association.DEFAULT_TRANSCRIPT_ROOT,
        "job_id": None, "proposal_index": 0,
        "refinement_index": 0, "candidate_index": 0, "target_index": 0,
    })["order"][0] == "b3" else -3
    assert summary["speech_naturalness"]["unverified_b3_minus_b2_score_sum"] == expected
    assert sum(result["unverified_preference_counts_by_edit"].values()) == 1
    assert result["human_quality_gold_verified"] is False
    assert result["independent_raters_verified"] is False
    assert result["observational_association_not_causal_improvement"] is True
    assert result["automatic_winner"] is None
    assert result["automatic_adoption_allowed"] is False
    assert result["ready"] is False and result["production_ready"] is False


@pytest.mark.parametrize("mutate", [
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[0], "source_start", 10.5),
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[0], "source_start", 7),
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[1], "source_end", 39),
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[0], "source_end", 22),
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[0], "source_asset_id", "other"),
    lambda c: setattr(c["roles"]["b3"]["timeline"].segments[0], "output_order", 5),
    lambda c: c.update(trial_action="UNKNOWN"),
])
def test_non_single_edge_or_untrusted_changes_fail_closed(
    tmp_path, monkeypatch, mutate,
):
    state, form, rating = _study(tmp_path, monkeypatch)
    mutate(state["basis"]["context"])
    with pytest.raises(ValueError, match="BLIND_ASSOCIATION"):
        association.compare_blind_edge_association(
            rating_paths=[rating], preview_root=tmp_path,
        )


def test_no_ratings_cannot_be_misrepresented_as_quality(tmp_path, monkeypatch):
    _study(tmp_path, monkeypatch)
    report = association.compare_blind_edge_association(
        rating_paths=[], preview_root=tmp_path,
    )
    assert report["status"] == "BLIND_ASSOCIATION_NO_VERIFIED_STUDY_AND_SUBMISSIONS"
    assert report["human_quality_gold_verified"] is False
    assert report["automatic_winner"] is None
    assert report["ready"] is False
    assert "unverified_paired_role_score_differences" not in report


def test_alternate_end_extension_is_supported(tmp_path, monkeypatch):
    state, form, rating = _study(tmp_path, monkeypatch)
    ctx = state["basis"]["context"]
    ctx["trial_action"] = "EXTEND_END"
    ctx["roles"]["b3"]["timeline"].segments[0].source_start = 10
    ctx["roles"]["b3"]["timeline"].segments[0].source_end = 20.75
    result = association.compare_blind_edge_association(
        rating_paths=[rating], preview_root=tmp_path,
    )
    assert result["changed_edit"]["edge"] == "EXTEND_END"
    assert result["changed_edit"]["b3_added_source_duration_seconds"] == .75


def test_repeated_files_and_unsafe_form_fields_are_rejected(
    tmp_path, monkeypatch,
):
    state, form, rating = _study(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="DUPLICATED_FORM_BYTES"):
        association.compare_blind_edge_association(
            rating_paths=[rating, rating], preview_root=tmp_path,
        )
    spoof = copy.deepcopy(form)
    spoof["independent_human_gold_verified"] = True
    rating.write_text(json.dumps(spoof), encoding="utf-8")
    with pytest.raises(ValueError, match="BLIND_RATING"):
        association.compare_blind_edge_association(
            rating_paths=[rating], preview_root=tmp_path,
        )


def test_cli_and_create_only_summary(tmp_path, monkeypatch):
    _study(tmp_path, monkeypatch)
    rating = tmp_path / "rating.json"
    argv = [
        "render-b2-b3-blind-edge-association",
        "--preview-root", str(tmp_path), "--rating", str(rating),
    ]
    out = CliRunner().invoke(cli.app, argv)
    assert out.exit_code == 0, out.stdout
    assert json.loads(out.stdout)["status"] == "BLIND_ASSOCIATION_UNVERIFIED_OBSERVATION_ONLY"
    target = tmp_path / "edge-association.json"
    created = CliRunner().invoke(cli.app, [*argv, "--output", str(target)])
    assert created.exit_code == 0, created.stdout
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(created.stdout)
    blocked = CliRunner().invoke(cli.app, [*argv, "--output", str(target)])
    assert blocked.exit_code == 6
    assert json.loads(blocked.stdout)["ready"] is False
