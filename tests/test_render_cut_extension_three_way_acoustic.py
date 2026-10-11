"""Offline acoustic signal checks at ASR-mapped A/B/B2 positions.

These are measurements of decoded volume only, never proof of spoken words.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_three_way_acoustic as acoustic
from test_render_cut_extension_three_way_triage import _prepared
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    params, review, media, asr, _ = _prepared(tmp_path, monkeypatch)
    proposal_index = review["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    prepared = acoustic.prepare_three_way_acoustic(
        **params, proposal_index=proposal_index,
    )
    return params, prepared, media, asr


def _patch_measure(monkeypatch, *, mutate=None, fail_at=None):
    calls = []

    def measure(ffmpeg, path, start, end):
        calls.append((ffmpeg, path, start, end))
        if fail_at == len(calls):
            raise ValueError("LOCAL_AUDIO_MEASUREMENT_UNAVAILABLE")
        if mutate is not None and len(calls) == 1:
            mutate()
        return (-55.0, -49.0) if len(calls) % 2 == 0 else (-24.0, -7.0)

    monkeypatch.setattr(acoustic.shutil, "which", lambda executable: "fake-ffmpeg")
    monkeypatch.setattr(acoustic, "_measure", measure)
    return calls


def test_preview_is_no_ffmpeg_no_asr_text_no_edit(tmp_path, monkeypatch):
    _, prepared, media, transcript = _case(tmp_path, monkeypatch)
    raw_video, raw_asr = media.read_bytes(), transcript.read_bytes()
    monkeypatch.setattr(
        acoustic, "_measure",
        lambda *a: pytest.fail("preview must not open FFmpeg"),
    )
    monkeypatch.setattr(
        acoustic.shutil, "which",
        lambda name: pytest.fail("preview must not check decoder"),
    )
    report = acoustic.run_three_way_acoustic(prepared)
    assert report["status"] == "LOCAL_ACOUSTIC_PROBE_PREVIEW_NO_DECODING"
    assert report["measured_audio_sample_count"] == 0
    assert set(report["variants"]) == {"a", "b", "b2"}
    for variant in ("a", "b", "b2"):
        row = report["variants"][variant]
        assert row["audio_samples"]
        assert row["spoken_words_verified"] is False
        assert row["new_video_frame_analysis_performed"] is False
        assert all(x["mean_dbfs"] is None and x["peak_dbfs"] is None
                   for x in row["audio_samples"])
        assert all(x["output_start_seconds"] < x["output_end_seconds"]
                   for x in row["audio_samples"])
    assert report["asr_audio_content_independently_verified"] is False
    assert report["human_narrative_quality_verified"] is False
    assert report["best_variant"] is None
    assert report["ready"] is False and report["publication_performed"] is False
    assert not prepared["receipt"].exists()
    assert media.read_bytes() == raw_video
    assert transcript.read_bytes() == raw_asr
    serialized = json.dumps(report, ensure_ascii=False)
    assert "まだ発話は続いている" not in serialized
    assert str(tmp_path) not in serialized


def test_execute_measures_real_local_derived_windows_without_promoting(
    tmp_path, monkeypatch,
):
    _, prepared, media, asr = _case(tmp_path, monkeypatch)
    before = media.read_bytes(), asr.read_bytes()
    b2 = prepared["dialogue"]["review"]["diagnostics"]["finished"]["b2"]["output"]
    b2_before = b2.read_bytes()
    calls = _patch_measure(monkeypatch)
    report = acoustic.run_three_way_acoustic(prepared, execute=True)
    assert report["status"] == "LOCAL_ACOUSTIC_MEASURED_SIGNALS_NOT_SPEECH_PROOF"
    assert report["measured_audio_sample_count"] == len(calls)
    assert len(calls) >= 6
    assert all(path.suffix == ".mp4" for _, path, _, _ in calls)
    assert all(0.06 <= end - start <= 0.602 for _, _, start, end in calls)
    assert all(x[0] == "fake-ffmpeg" for x in calls)
    flat = [item for v in report["variants"].values() for item in v["audio_samples"]]
    assert all(isinstance(item["mean_dbfs"], float) and
               isinstance(item["peak_dbfs"], float) for item in flat)
    assert any(item["near_silent_energy_hint"] is True for item in flat)
    assert any(item["near_silent_energy_hint"] is False for item in flat)
    assert report["source_asr_sha256"]
    assert report["subtitle_to_speech_verified"] is False
    assert report["non_silence_does_not_prove_spoken_words"] is True
    assert report["silence_does_not_imply_incomplete_speech"] is True
    assert report["auto_adoption_allowed"] is False
    assert report["editorial_quality_improvement"] is None
    assert report["ready"] is False
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    assert (media.read_bytes(), asr.read_bytes()) == before
    assert b2.read_bytes() == b2_before
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert len(calls) == report["measured_audio_sample_count"]


def test_no_ffmpeg_blocks_no_receipt(tmp_path, monkeypatch):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(acoustic.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        acoustic, "_measure", lambda *a: pytest.fail("must not probe"),
    )
    with pytest.raises(ValueError, match="FFMPEG_UNAVAILABLE"):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert not prepared["receipt"].exists()


@pytest.mark.parametrize("invalid", [
    (8.0, -6.0),
    (-80.0, -90.0),
    (float("nan"), -6.0),
    (-30.0, float("inf")),
])
def test_invalid_audio_decoding_fails_closed(tmp_path, monkeypatch, invalid):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(acoustic.shutil, "which", lambda _: "fake-ffmpeg")
    monkeypatch.setattr(acoustic, "_measure", lambda *a: invalid)
    with pytest.raises(ValueError, match="MEASUREMENT_UNTRUSTED"):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert not prepared["receipt"].exists()


def test_failed_audio_probe_never_commits_partial_evidence(tmp_path, monkeypatch):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    calls = _patch_measure(monkeypatch, fail_at=3)
    with pytest.raises(ValueError, match="MEASUREMENT_UNAVAILABLE"):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert len(calls) == 3
    assert not prepared["receipt"].exists()


@pytest.mark.parametrize("changed", ["media", "asr", "diagnostic"])
def test_changed_input_during_decode_rejects_receipt(
    tmp_path, monkeypatch, changed,
):
    _, prepared, media, transcript = _case(tmp_path, monkeypatch)
    path = {
        "media": media,
        "asr": transcript,
        "diagnostic": prepared["dialogue"]["review"]["diagnostics"]["receipt"],
    }[changed]

    def mutate():
        if changed == "asr":
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["segments"][-1]["end"] = 26.0
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_bytes(b"changed while decoding")

    _patch_measure(monkeypatch, mutate=mutate)
    with pytest.raises(ValueError):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert not prepared["receipt"].exists()


def test_missing_saved_three_way_diagnostics_blocks_without_decoder(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    prepared["dialogue"]["review"]["diagnostics"]["receipt"].unlink()
    monkeypatch.setattr(
        acoustic, "_measure", lambda *a: pytest.fail("must not decode"),
    )
    with pytest.raises(ValueError):
        acoustic.run_three_way_acoustic(prepared, execute=True)
    assert not prepared["receipt"].exists()


def test_cli_preview_and_explicit_local_probe(tmp_path, monkeypatch):
    params, prepared, _, _ = _case(tmp_path, monkeypatch)
    _patch_measure(monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *a, **k: pytest.fail("no external language provider needed"),
    )
    proposal_index = prepared["dialogue"]["review"]["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    args = [
        "render-cut-extension-three-way-acoustic", *_args(params)[1:],
        "--proposal-index", str(proposal_index),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["measured_audio_sample_count"] == 0
    assert not prepared["receipt"].exists()
    observed = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert observed.exit_code == 0, observed.stdout
    assert json.loads(observed.stdout)["measured_audio_sample_count"] >= 6
    assert prepared["receipt"].is_file()
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["status"] == "LOCAL_THREE_WAY_ACOUSTIC_BLOCKED"
