"""Offline source-pinned A/B/B2 diagnostics; no inference of comedy quality."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_three_way_diagnostics as quality
from holoclip import render_cut_extension_speech_b2_finish as finish
from holoclip import render_cut_extension_finish_ab as ab
from test_render_cut_extension_speech_b2_finish import _case, _stages
from test_render_cut_extension_mp4_ab import _args


def _ready(tmp_path, monkeypatch):
    params, prepared, media, transcript = _case(tmp_path, monkeypatch)
    _stages(monkeypatch)
    completed = finish.finish_speech_b2(
        prepared, execute=True, approve_local_preview=True,
    )
    assert completed["status"] == "LOCAL_B2_FINISH_STAGES_TECHNICALLY_COMPARABLE"
    audited = quality.prepare_three_way_diagnostics(
        **params, proposal_index=prepared["b2"]["prepared"]["trial"]["proposal_index"],
    )
    return params, audited, media, transcript


def _fake_scan(monkeypatch, *, fail=None, mutate=None):
    calls = []

    def scan(*, base, base_sha256, final, final_sha256, timeline,
             caption_sha256):
        name = (
            "b2" if "-speechb2-" in base.name else
            "b" if "-extension-" in base.name else "a"
        )
        calls.append((name, base, final, caption_sha256))
        if name == fail:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_VIDEO_UNAVAILABLE")
        if name == mutate:
            base.write_bytes(b"changed during local diagnostic")
        return {
            "version": "holoclip-edit-diagnostic-v1",
            "status": "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS",
            "base_sha256": base_sha256,
            "final_sha256": final_sha256,
            "black_interval_count": 0,
            "freeze_interval_count": 0,
            "subtitle_checked": caption_sha256 is not None,
            "subtitle_fast_cue_count": 0 if name != "b" else 1,
            "subtitle_overflow_count": 0,
            "subtitle_max_cps": 10.0 if name != "b" else 21.0,
            "audio_sample_mean_dbfs": -19.0 if name != "b" else -24.0,
            "audio_sample_peak_dbfs": -4.0,
            "flag_count": 0 if name != "b" else 1,
            "review_observations": [],
        }

    monkeypatch.setattr(quality, "_run", scan)
    return calls


def test_default_view_is_read_only_and_does_not_decode(tmp_path, monkeypatch):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    base = prepared["finished"]["b2"]["output"]
    before = (media.read_bytes(), base.read_bytes(), transcript.read_bytes())
    monkeypatch.setattr(
        quality, "_run", lambda **kwargs: pytest.fail("preview must not decode"),
    )
    report = quality.run_three_way_diagnostics(prepared)
    assert report["status"] == "LOCAL_THREE_WAY_DIAGNOSTICS_PREVIEW_ONLY"
    assert set(report["variants"]) == {"a", "b", "b2"}
    assert all(v["technical_diagnostic"] is None for v in report["variants"].values())
    assert report["comparison"] is None
    assert report["actual_media_scanned"] is False
    assert report["source_clock_boundary"]["asr_only_not_verified_audio"] is True
    assert report["ready"] is False and report["publication_performed"] is False
    assert not prepared["receipt"].exists()
    assert (media.read_bytes(), base.read_bytes(), transcript.read_bytes()) == before
    assert str(tmp_path) not in json.dumps(report)


def test_execute_scans_all_finished_variants_without_editing_any(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    a = media.read_bytes()
    b2 = prepared["finished"]["b2"]["output"].read_bytes()
    asr = transcript.read_bytes()
    scans = _fake_scan(monkeypatch)
    report = quality.run_three_way_diagnostics(prepared, execute=True)
    assert [x[0] for x in scans] == ["a", "b", "b2"]
    assert all(x[3] is not None for x in scans)
    assert report["status"] == "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY"
    assert report["actual_media_scanned"] is True
    assert report["comparison"]["b2_minus_b_review_flag_count"] == -1
    assert report["comparison"]["b2_minus_a_review_flag_count"] == 0
    assert report["comparison"]["no_automatic_winner_selected"] is True
    assert report["source_asr_linguistically_verified"] is False
    assert report["human_editorial_qa_completed"] is False
    assert report["editorial_quality_improvement"] is None
    assert report["ready"] is False
    assert report["auto_adoption_allowed"] is False
    assert report["production_ready"] is False
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    assert media.read_bytes() == a
    assert prepared["finished"]["b2"]["output"].read_bytes() == b2
    assert transcript.read_bytes() == asr
    assert str(tmp_path) not in json.dumps(report)
    with pytest.raises(ValueError, match="REPORT_EXISTS"):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert len(scans) == 3


def test_decoder_failure_stops_without_creating_report(tmp_path, monkeypatch):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    scans = _fake_scan(monkeypatch, fail="b")
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert [x[0] for x in scans] == ["a", "b"]
    assert not prepared["receipt"].exists()


def test_media_tamper_during_decoder_refuses_saving(tmp_path, monkeypatch):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    _fake_scan(monkeypatch, mutate="b2")
    with pytest.raises(ValueError):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert not prepared["receipt"].exists()


@pytest.mark.parametrize("which", ["a", "b", "b2", "matched", "b2tech", "b2finish", "asr"])
def test_changed_input_provenance_blocks_without_decoder(
    tmp_path, monkeypatch, which,
):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    b2 = prepared["finished"]["b2"]
    matched = b2["prepared"]
    paths = {
        "a": media,
        "b": matched["trial"]["output"],
        "b2": b2["output"],
        "matched": matched["matched_receipt"],
        "b2tech": b2["receipt"],
        "b2finish": prepared["finished"]["finished_receipt"],
        "asr": transcript,
    }
    file = paths[which]
    if which == "asr":
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["segments"][-1]["end"] = 25.0
        file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    else:
        file.write_bytes(b"some altered evidence")
    monkeypatch.setattr(
        quality, "_run", lambda **kwargs: pytest.fail("do not decode"),
    )
    with pytest.raises(ValueError):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert not prepared["receipt"].exists()


@pytest.mark.parametrize("variant", ["a", "b", "b2"])
def test_corrupted_subtitles_block_without_scanning(tmp_path, monkeypatch, variant):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    b2 = prepared["finished"]["b2"]
    bases = {
        "a": b2["prepared"]["trial"]["original"]["baseline_media"],
        "b": b2["prepared"]["trial"]["output"],
        "b2": b2["output"],
    }
    caption = ab._stage_paths(bases[variant])["caption"]
    caption.write_bytes(b"changed SRT")
    monkeypatch.setattr(
        quality, "_run", lambda **kwargs: pytest.fail("do not decode"),
    )
    with pytest.raises(ValueError):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert not prepared["receipt"].exists()


def test_incomplete_B2_finish_receipt_never_claims_comparable(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    file = prepared["finished"]["finished_receipt"]
    payload = json.loads(file.read_text(encoding="utf-8"))
    payload["stage_comparable_to_a_and_b"] = False
    file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="FINISH_LINEAGE_CHANGED"):
        quality.run_three_way_diagnostics(prepared, execute=True)
    assert not prepared["receipt"].exists()


def test_cli_readonly_and_no_model_requirements(tmp_path, monkeypatch):
    params, prepared, _, _ = _ready(tmp_path, monkeypatch)
    args = [
        "render-cut-extension-three-way-diagnostics",
        *_args(params)[1:],
        "--proposal-index",
        str(prepared["finished"]["b2"]["prepared"]["trial"]["proposal_index"]),
    ]
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kwargs: pytest.fail("external AI must not be used"),
    )
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == "LOCAL_THREE_WAY_DIAGNOSTICS_PREVIEW_ONLY"
    assert not prepared["receipt"].exists()
