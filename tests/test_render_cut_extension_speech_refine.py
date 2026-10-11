"""Offline, no-adoption regression for ASR-clock-only alternative cut edges."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_mp4_ab as basic
from holoclip import render_cut_extension_finish_ab as finish
from holoclip import render_cut_extension_speech_audit as speech
from holoclip import render_cut_extension_speech_refine as refine
from holoclip.models import TranscriptSegment
from test_render_cut_extension_finish_ab import _monkeypatch_video, _stages
from test_render_cut_extension_mp4_ab import _args, _inputs


def _ready(tmp_path, monkeypatch, *, extra_end=24.5):
    params, _, _, media, transcript = _inputs(tmp_path)
    if extra_end is not None:
        payload = json.loads(transcript.read_text(encoding="utf-8"))
        payload["segments"].append({
            "start": 23.85, "end": extra_end,
            "text": "まだ発話は続いている",
        })
        transcript.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
    _stages(params, media)
    # Select the actual verified extension of the final cut, rather than
    # assuming the first available trial is always that candidate.
    proposals = basic.preview_cut_extensions(**params)["proposals"]
    matching = [item for item in proposals if item["job_id"] == proposals[0]["job_id"]]
    index = next(
        i for i, item in enumerate(matching)
        if item["source_cut_index"] == 1
        and item["trial_action"] == "EXTEND_END"
    )
    prepared = finish.prepare_matched_finishing_trial(
        **params, proposal_index=index,
    )
    _monkeypatch_video(monkeypatch)
    saved = finish.run_matched_finishing_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert saved["stage_comparable"] is True
    monkeypatch.setattr(
        speech, "_probe", lambda path: (
            7.0 if "-extension-" in path.name else 6.0, True, True,
        ),
    )
    return params, prepared, media, transcript


def test_actual_finished_ab_recommends_completed_utterance_without_edits(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    media_before, asr_before = media.read_bytes(), transcript.read_bytes()
    original_trial = prepared["trial"]["output"].read_bytes()
    report = refine.suggest_speech_boundary_refinements(prepared)
    assert report["status"] == "LOCAL_SPEECH_BOUNDARY_ALTERNATIVES_UNRENDERED"
    assert report["proposal_count"] == 1
    candidate = report["proposals"][0]
    assert candidate["source_cut_index"] == 1
    assert candidate["trial_action"] == "EXTEND_END"
    assert candidate["proposed_source_end_seconds"] == 24.5
    assert candidate["increment_beyond_current_trial_seconds"] == 0.5
    assert candidate["total_extension_from_baseline_seconds"] == 1.5
    assert candidate["old_trial_boundary_asr_crossings"] >= 1
    assert candidate["proposed_boundary_asr_crossings"] == 0
    assert candidate["requires_fresh_render_policy_and_source_media_qa"] is True
    assert candidate["unrendered_preview_only"] is True
    assert report["editorial_quality_improvement"] is None
    assert report["audio_quality_improvement"] is None
    assert report["ready"] is False
    assert report["auto_adoption_allowed"] is False
    assert media.read_bytes() == media_before
    assert transcript.read_bytes() == asr_before
    assert prepared["trial"]["output"].read_bytes() == original_trial
    redacted = json.dumps(report, ensure_ascii=False)
    assert "まだ発話は続いている" not in redacted
    assert str(tmp_path) not in redacted


def test_existing_clean_boundary_abstains_instead_of_fake_improvement(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch, extra_end=None)
    report = refine.suggest_speech_boundary_refinements(prepared)
    assert report["status"] == "LOCAL_SPEECH_BOUNDARY_NO_SAFER_CLOCK_ALTERNATIVE"
    assert report["proposal_count"] == 0
    assert report["proposals"] == []
    assert report["ready"] is False


def test_candidate_beyond_two_second_trial_margin_is_rejected(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch, extra_end=26.5)
    report = refine.suggest_speech_boundary_refinements(prepared)
    assert report["proposals"] == []


def test_start_extension_matches_asr_start_not_word_guess(tmp_path, monkeypatch):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    plan = dict(prepared["trial"])
    original = plan["original"]["baseline_timeline"]
    ordered = sorted(original.segments, key=lambda item: item.output_order)
    before = [
        {"source_start_seconds": x.source_start,
         "source_end_seconds": x.source_end}
        for x in ordered
    ]
    after = [dict(x) for x in before]
    after[1]["source_start_seconds"] = 19.0
    plan["trial"] = basic._trial_timeline(original, {
        "baseline_source_intervals": before,
        "trial_source_intervals": after,
        "source_cut_index": 1,
        "trial_action": "EXTEND_START",
    })
    plan["trial_action"] = "EXTEND_START"
    plan["source_cut_index"] = 1
    alternatives = refine._boundary_refinements(plan, [
        TranscriptSegment(start=18.5, end=19.5, text="切り始めが途中"),
    ])
    assert len(alternatives) == 1
    assert alternatives[0]["proposed_source_start_seconds"] == 18.5
    assert alternatives[0]["proposed_boundary_asr_crossings"] == 0


def test_alternatives_are_immutable_optional_json_only(tmp_path, monkeypatch):
    _, prepared, media, _ = _ready(tmp_path, monkeypatch)
    target = tmp_path / "report.json"
    raw = media.read_bytes()
    first = refine.suggest_speech_boundary_refinements(
        prepared, output=target,
    )
    assert json.loads(target.read_text(encoding="utf-8")) == first
    with pytest.raises(ValueError):
        refine.suggest_speech_boundary_refinements(prepared, output=target)
    assert media.read_bytes() == raw
    assert first["publication_performed"] is False


def test_modified_trial_final_bytes_prevent_any_proposal(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    altered = finish._stage_paths(prepared["trial"]["output"])["burnin"]
    altered.write_bytes(b"some different MP4")
    with pytest.raises(ValueError, match="TRIAL_CHANGED"):
        refine.suggest_speech_boundary_refinements(prepared)


def test_changed_source_asr_prevents_stale_candidate(tmp_path, monkeypatch):
    _, prepared, _, transcript = _ready(tmp_path, monkeypatch)
    raw = json.loads(transcript.read_text(encoding="utf-8"))
    raw["segments"][-1]["end"] = 25.0
    transcript.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="ASR_LINEAGE_CHANGED"):
        refine.suggest_speech_boundary_refinements(prepared)


def test_cli_preview_never_contacts_external_model_or_writes(
    tmp_path, monkeypatch,
):
    params, prepared, _, _ = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kwargs: pytest.fail("No external AI required"),
    )
    target = tmp_path / "not-created.json"
    args = ["render-cut-extension-speech-refine", *_args(params)[1:],
            "--proposal-index", str(prepared["trial"]["proposal_index"])]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["proposal_count"] == 1
    assert not target.exists()
    run = CliRunner().invoke(cli.app, [*args, "--output", str(target)])
    assert run.exit_code == 0, run.stdout
    assert json.loads(target.read_text(encoding="utf-8")) == (
        json.loads(run.stdout)
    )
    assert CliRunner().invoke(cli.app, [
        *args, "--output", str(target),
    ]).exit_code == 6
