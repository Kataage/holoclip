"""Offline local A/B/B2 technical render, provenance and no-overwrite tests."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_speech_b2 as b2
from holoclip import render_cut_extension_finish_ab as finish
from test_render_cut_extension_mp4_ab import _args
from test_render_cut_extension_speech_refine import _ready


def _setup(tmp_path, monkeypatch):
    params, finished, media, transcript = _ready(tmp_path, monkeypatch)
    selected_index = finished["trial"]["proposal_index"]
    candidate = b2.prepare_speech_b2_preview(
        **params, proposal_index=selected_index,
    )
    monkeypatch.setattr(b2, "_probe", lambda path: (
        7.5 if "-speechb2-" in path.name
        else 7.0 if "-extension-" in path.name
        else 6.0,
        True, True,
    ))
    return params, candidate, media, transcript


def _fake_renderer(monkeypatch):
    renders = []
    policies = []

    def fake_render(plan):
        renders.append(plan)
        payload = b"isolated new B2 technical MP4" * 120
        plan.output.write_bytes(payload)
        return {
            "status": b2._EXPECTED_QA,
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "measured_duration": plan.expected_duration,
        }

    monkeypatch.setattr(b2, "render_local_preview", fake_render)
    monkeypatch.setattr(
        b2, "_offline_render_policy",
        lambda *args: policies.append(args),
    )
    return renders, policies


def test_default_preview_is_no_write_and_does_not_fake_finished_parity(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _setup(tmp_path, monkeypatch)
    before_a = media.read_bytes()
    before_b = prepared["prepared"]["trial"]["output"].read_bytes()
    before_asr = transcript.read_bytes()
    report = b2.run_speech_b2_preview(prepared)
    assert report["status"] == "LOCAL_B2_PREVIEW_NOT_RENDERED"
    assert report["b2_extra_seconds_over_b"] == 0.5
    assert report["a_b_b2_media_compared"] is False
    assert report["b2_caption_audio_finishing_matched_to_a_b"] is False
    assert report["ready"] is False
    assert report["auto_adoption_allowed"] is False
    assert report["editorial_quality_improvement"] is None
    assert not prepared["output"].exists()
    assert not prepared["receipt"].exists()
    assert media.read_bytes() == before_a
    assert prepared["prepared"]["trial"]["output"].read_bytes() == before_b
    assert transcript.read_bytes() == before_asr
    assert str(tmp_path) not in json.dumps(report)


def test_explicit_local_render_creates_only_b2_and_immutable_receipt(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _setup(tmp_path, monkeypatch)
    original_a = media.read_bytes()
    original_b = prepared["prepared"]["trial"]["output"].read_bytes()
    original_asr = transcript.read_bytes()
    renders, policies = _fake_renderer(monkeypatch)
    result = b2.run_speech_b2_preview(
        prepared, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_B2_RENDERED_THREE_WAY_TECHNICAL_ONLY"
    assert len(renders) == len(policies) == 1
    assert renders[0].expected_duration == pytest.approx(7.5)
    assert result["b2_mp4_sha256"] == hashlib.sha256(
        prepared["output"].read_bytes()
    ).hexdigest()
    assert result["baseline_a_measured_seconds"] == 6.0
    assert result["trial_b_measured_seconds"] == 7.0
    assert result["b2_measured_seconds"] == 7.5
    assert result["b2_vs_b_duration_difference_seconds"] == 0.5
    assert result["b2_caption_audio_finishing_matched_to_a_b"] is False
    assert result["a_b_b2_media_compared"] is True
    assert result["b2_technical_qa"] == b2._EXPECTED_QA
    assert result["ready"] is False and result["auto_adoption_allowed"] is False
    assert result["publication_performed"] is False
    assert json.loads(prepared["receipt"].read_text(
        encoding="utf-8"
    )) == result
    assert media.read_bytes() == original_a
    assert prepared["prepared"]["trial"]["output"].read_bytes() == original_b
    assert transcript.read_bytes() == original_asr
    output_text = json.dumps(result, ensure_ascii=False)
    assert "まだ発話は続いている" not in output_text
    assert str(tmp_path) not in output_text


def test_no_render_without_explicit_local_permission(tmp_path, monkeypatch):
    _, prepared, _, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        b2, "render_local_preview",
        lambda *args: pytest.fail("must not start render"),
    )
    with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
        b2.run_speech_b2_preview(prepared, execute=True)
    assert not prepared["output"].exists()


@pytest.mark.parametrize("occupy", ["output", "receipt"])
def test_no_existing_b2_artifact_is_overwritten(
    tmp_path, monkeypatch, occupy,
):
    _, prepared, _, _ = _setup(tmp_path, monkeypatch)
    prepared[occupy].write_bytes(b"existing local user file")
    renders, _ = _fake_renderer(monkeypatch)
    with pytest.raises(ValueError, match="EXISTS|UNSAFE"):
        b2.run_speech_b2_preview(
            prepared, execute=True, approve_local_preview=True,
        )
    assert renders == []
    assert prepared[occupy].read_bytes() == b"existing local user file"


def test_changed_b_final_blocks_before_new_render(tmp_path, monkeypatch):
    _, prepared, _, _ = _setup(tmp_path, monkeypatch)
    final = finish._stage_paths(prepared["prepared"]["trial"]["output"])["burnin"]
    final.write_bytes(b"tampered finished B")
    renders, _ = _fake_renderer(monkeypatch)
    with pytest.raises(ValueError, match="TRIAL_CHANGED"):
        b2.run_speech_b2_preview(
            prepared, execute=True, approve_local_preview=True,
        )
    assert renders == []
    assert not prepared["receipt"].exists()


def test_changed_asr_refuses_stale_trial_candidate(tmp_path, monkeypatch):
    _, prepared, _, transcript = _setup(tmp_path, monkeypatch)
    raw = json.loads(transcript.read_text(encoding="utf-8"))
    raw["segments"][-1]["end"] = 25.0
    transcript.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    renders, _ = _fake_renderer(monkeypatch)
    with pytest.raises(ValueError, match="ASR_LINEAGE_CHANGED"):
        b2.run_speech_b2_preview(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not renders


def test_baseline_mutated_during_encoding_blocks_three_way_receipt(
    tmp_path, monkeypatch,
):
    _, prepared, media, _ = _setup(tmp_path, monkeypatch)
    _fake_renderer(monkeypatch)
    render = b2.render_local_preview

    def mutate_a(plan):
        result = render(plan)
        media.write_bytes(b"changed base A after encode")
        return result

    monkeypatch.setattr(b2, "render_local_preview", mutate_a)
    with pytest.raises(ValueError, match="ORIGINAL_CHANGED"):
        b2.run_speech_b2_preview(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not prepared["receipt"].exists()
    assert prepared["output"].exists()


def test_invalid_refinement_index_abstains_without_files(tmp_path, monkeypatch):
    params, prepared, _, _ = _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="NO_VERIFIED_REFINEMENT"):
        b2.prepare_speech_b2_preview(
            **params,
            proposal_index=prepared["prepared"]["trial"]["proposal_index"],
            refinement_index=1,
        )


def test_cli_default_and_permission_gate_have_no_hidden_external_work(
    tmp_path, monkeypatch,
):
    params, prepared, _, _ = _setup(tmp_path, monkeypatch)
    args = [
        "render-cut-extension-speech-b2",
        *_args(params)[1:],
        "--proposal-index",
        str(prepared["prepared"]["trial"]["proposal_index"]),
    ]
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kwargs: pytest.fail("no model allowed"),
    )
    default = CliRunner().invoke(cli.app, args)
    assert default.exit_code == 0, default.stdout
    assert json.loads(default.stdout)["status"] == "LOCAL_B2_PREVIEW_NOT_RENDERED"
    assert not prepared["output"].exists()
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["status"] == "LOCAL_B2_BLOCKED"
    assert not prepared["output"].exists()
