"""Review-need triage from locally measured A/B/B2, never AI quality ranking."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_three_way_diagnostics as diagnostics
from holoclip import render_cut_extension_three_way_triage as triage
from test_render_cut_extension_three_way_diagnostics import _fake_scan, _ready
from test_render_cut_extension_mp4_ab import _args


def _prepared(tmp_path, monkeypatch, *, observations=False):
    params, checked, media, transcript = _ready(tmp_path, monkeypatch)
    calls = _fake_scan(monkeypatch)
    if observations:
        original = diagnostics._run

        def with_review_observations(**kwargs):
            result = original(**kwargs)
            if "-speechb2-" in kwargs["base"].name:
                result["review_observations"] = [
                    {"kind": "fast_subtitle", "start_seconds": .3,
                     "end_seconds": .75, "cue_number": 1},
                    {"kind": "blackout", "start_seconds": 1.0,
                     "end_seconds": 2.0},
                ]
            return result

        monkeypatch.setattr(diagnostics, "_run", with_review_observations)
    done = diagnostics.run_three_way_diagnostics(checked, execute=True)
    assert done["status"] == "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY"
    prepared = triage.prepare_three_way_review(
        **params, proposal_index=checked["finished"]["b2"]["prepared"]["trial"]["proposal_index"],
    )
    return params, prepared, media, transcript, calls


def test_default_prioritizes_all_three_versions_at_source_aligned_edit(
    tmp_path, monkeypatch,
):
    _, plan, media, asr, calls = _prepared(tmp_path, monkeypatch)
    saved_media = media.read_bytes()
    saved_asr = asr.read_bytes()
    monkeypatch.setattr(
        diagnostics, "_run", lambda **kwargs: pytest.fail("must not decode twice"),
    )
    result = triage.run_three_way_review(plan)
    assert result["status"] == "LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED"
    assert result["review_target_count"] == 3
    assert [x["variant"] for x in result["review_targets"]] == ["a", "b", "b2"]
    assert [x["review_order"] for x in result["review_targets"]] == [1, 2, 3]
    assert [x["priority"] for x in result["review_targets"]] == [0, 0, 0]
    assert all(x["editorial_result"] is None for x in result["review_targets"])
    assert result["review_order_is_not_variant_ranking"] is True
    assert result["best_variant"] is None
    assert result["editorial_quality_improvement"] is None
    assert result["human_editorial_qa_completed"] is False
    assert result["auto_adoption_allowed"] is False
    assert result["publication_performed"] is False
    assert result["ready"] is False
    assert not plan["receipt"].exists()
    assert len(calls) == 3
    assert asr.read_bytes() == saved_asr
    assert media.read_bytes() == saved_media
    assert str(tmp_path) not in json.dumps(result)
    assert "まだ発話は続いている" not in json.dumps(result, ensure_ascii=False)


def test_decoder_observations_become_located_review_questions_not_scores(
    tmp_path, monkeypatch,
):
    _, prepared, _, _, _ = _prepared(
        tmp_path, monkeypatch, observations=True,
    )
    report = triage.run_three_way_review(prepared)
    assert report["review_target_count"] == 5
    assert [x["kind"] for x in report["review_targets"][:3]] == [
        "VERIFY_EDIT_TRANSITION_AND_SPEECH_COMPLETENESS",
    ] * 3
    assert [x["kind"] for x in report["review_targets"][3:]] == [
        "blackout", "fast_subtitle",
    ]
    assert report["review_targets"][4]["cue_number"] == 1
    assert report["review_targets"][3]["question_code"] == (
        "INSPECT_DARK_SCENE_AND_SOURCE"
    )
    assert all(x["editorial_result"] is None for x in report["review_targets"])
    assert report["best_variant"] is None
    for role in ("a", "b", "b2"):
        window = report["review_focus_per_variant"][role]
        assert 0 <= window["output_start_seconds"] < window["output_end_seconds"]


def test_save_creates_once_without_changing_any_asset(tmp_path, monkeypatch):
    _, prepared, media, transcript, _ = _prepared(tmp_path, monkeypatch)
    original = media.read_bytes()
    asr = transcript.read_bytes()
    finished = prepared["diagnostics"]["finished"]["b2"]["output"]
    b2_bytes = finished.read_bytes()
    report = triage.run_three_way_review(prepared, save=True)
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        triage.run_three_way_review(prepared, save=True)
    assert finished.read_bytes() == b2_bytes
    assert media.read_bytes() == original
    assert transcript.read_bytes() == asr
    assert report["ready"] is False


@pytest.mark.parametrize("damage", ["a", "b", "b2", "diagnostic", "asr", "matched"])
def test_mutated_lineage_never_emits_triage(
    tmp_path, monkeypatch, damage,
):
    _, plan, media, transcript, _ = _prepared(tmp_path, monkeypatch)
    b2 = plan["diagnostics"]["finished"]["b2"]
    files = {
        "a": media,
        "b": b2["prepared"]["trial"]["output"],
        "b2": b2["output"],
        "diagnostic": plan["diagnostics"]["receipt"],
        "asr": transcript,
        "matched": b2["prepared"]["matched_receipt"],
    }
    target = files[damage]
    if damage == "diagnostic":
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["comparison"]["b2_minus_b_review_flag_count"] = -99
        target.write_text(json.dumps(payload), encoding="utf-8")
    elif damage == "asr":
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["segments"][-1]["end"] = 26.0
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    else:
        target.write_bytes(b"tampered!")
    with pytest.raises(ValueError):
        triage.run_three_way_review(plan, save=True)
    assert not plan["receipt"].exists()


def test_invalid_out_of_range_timeline_observation_rejected(
    tmp_path, monkeypatch,
):
    _, plan, _, _, _ = _prepared(tmp_path, monkeypatch)
    file = plan["diagnostics"]["receipt"]
    report = json.loads(file.read_text(encoding="utf-8"))
    report["variants"]["b2"]["technical_diagnostic"]["review_observations"] = [
        {"kind": "blackout", "start_seconds": 5000.0,
         "end_seconds": None},
    ]
    file.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="ITEM_UNTRUSTED"):
        triage.run_three_way_review(plan, save=True)
    assert not plan["receipt"].exists()


def test_incomplete_real_local_diagnostic_refuses_review(
    tmp_path, monkeypatch,
):
    _, plan, _, _, _ = _prepared(tmp_path, monkeypatch)
    file = plan["diagnostics"]["receipt"]
    report = json.loads(file.read_text(encoding="utf-8"))
    report["actual_media_scanned"] = False
    file.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="LINEAGE_CHANGED"):
        triage.run_three_way_review(plan, save=True)


def test_operator_read_only_cli_and_create_only_save(
    tmp_path, monkeypatch,
):
    params, prepared, _, _, _ = _prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kwargs: pytest.fail("no remote LLM"),
    )
    args = [
        "render-cut-extension-three-way-review",
        *_args(params)[1:],
        "--proposal-index",
        str(prepared["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["review_target_count"] == 3
    assert not prepared["receipt"].exists()
    saved = CliRunner().invoke(cli.app, [*args, "--save"])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(saved.stdout)["ready"] is False
    assert prepared["receipt"].is_file()
    denied = CliRunner().invoke(cli.app, [*args, "--save"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["status"] == "LOCAL_THREE_WAY_REVIEW_BLOCKED"
