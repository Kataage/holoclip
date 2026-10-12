"""Four-way A/B/B2/B3 finished-media diagnostics, entirely offline mock tests."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_four_way_local_diagnostics as qa
from holoclip import render_three_way_b3_matched_finishing as finish
from test_render_three_way_b3_matched_finishing import _case, _mock_stages
from test_render_cut_extension_mp4_ab import _args


def _ready(tmp_path, monkeypatch):
    options, source, transcript, edited = _case(tmp_path, monkeypatch)
    _mock_stages(monkeypatch)
    output = finish.run_existing_b3_matched_finishing(
        **options, execute=True, approve_local_preview=True,
    )
    assert output["status"] == "LOCAL_B3_FINISH_STAGES_TECHNICALLY_COMPARABLE"
    return options, source, transcript, edited


def _probes(monkeypatch, *, change=None, fail_audio=False):
    scans = []
    audio = []

    def scan(*, base, base_sha256, final, final_sha256, timeline, caption_sha256):
        scans.append(base)
        if change == "source" and len(scans) == 4:
            change_path = getattr(_probes, "source", None)
            if change_path:
                change_path.write_bytes(b"modified during video scan")
        return {
            "version": "holoclip-edit-diagnostic-v1",
            "status": "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS",
            "base_sha256": base_sha256,
            "final_sha256": final_sha256,
            "flag_count": 0 if "-speechb3-" in base.name else 2,
            "black_interval_count": 0,
            "freeze_interval_count": 0,
            "subtitle_checked": caption_sha256 is not None,
            "subtitle_fast_cue_count": 0,
            "subtitle_overflow_count": 0,
            "subtitle_max_cps": 10.,
            "audio_sample_mean_dbfs": -20.,
            "audio_sample_peak_dbfs": -7.,
            "review_observations": [],
        }

    def measure(ffmpeg, path, start, end):
        audio.append((ffmpeg, path, start, end))
        if fail_audio and len(audio) == 3:
            raise ValueError("LOCAL_AUDIO_MEASUREMENT_FAILED")
        return (-30., -10.) if len(audio) % 2 else (-52., -49.)

    monkeypatch.setattr(qa, "_run", scan)
    monkeypatch.setattr(qa, "_measure", measure)
    monkeypatch.setattr(qa.shutil, "which", lambda exe: "mock-ffmpeg")
    return scans, audio


def test_read_only_four_way_preview_never_decodes_or_writes(tmp_path, monkeypatch):
    options, source, asr, edited = _ready(tmp_path, monkeypatch)
    before = source.read_bytes(), asr.read_bytes(), edited.read_bytes()
    monkeypatch.setattr(
        qa, "_run", lambda **kw: pytest.fail("preview must not decode video"),
    )
    monkeypatch.setattr(
        qa, "_measure", lambda *a: pytest.fail("preview must not decode audio"),
    )
    row = qa.run_four_way_diagnostics(**options)
    assert row["status"] == "LOCAL_FOUR_WAY_DIAGNOSTIC_PREVIEW_NO_DECODING"
    assert set(row["variants"]) == {"a", "b", "b2", "b3"}
    assert all(v["technical_diagnostic"] is None for v in row["variants"].values())
    assert all(v["edge_audio_samples"] for v in row["variants"].values())
    assert row["actual_audio_samples_measured"] == 0
    assert row["actual_videos_scanned"] == 0
    assert row["best_variant"] is None and row["ready"] is False
    assert row["transcript_text_exported"] is False
    assert (source.read_bytes(), asr.read_bytes(), edited.read_bytes()) == before
    assert not list(options["preview_root"].rglob("*-speechb3-*.four-way-diagnostics.json"))
    assert str(tmp_path) not in json.dumps(row)
    assert "まだ発話は続いている" not in json.dumps(row, ensure_ascii=False)


def test_explicit_permission_is_required_before_decoding(tmp_path, monkeypatch):
    options, _, _, _ = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        qa, "_run", lambda **kw: pytest.fail("no approval, no video decoder"),
    )
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        qa.run_four_way_diagnostics(**options, execute=True)


def test_four_way_scan_measures_all_with_identical_technical_analyzer(
    tmp_path, monkeypatch,
):
    options, source, asr, edited = _ready(tmp_path, monkeypatch)
    before = source.read_bytes(), asr.read_bytes(), edited.read_bytes()
    scans, audio = _probes(monkeypatch)
    report = qa.run_four_way_diagnostics(
        **options, execute=True, approve_local_preview=True,
    )
    assert report["status"] == "LOCAL_FOUR_WAY_TECHNICAL_ACOUSTIC_SIGNALS_ONLY"
    assert len(scans) == report["actual_videos_scanned"] == 4
    assert report["actual_audio_samples_measured"] == len(audio)
    assert len(audio) >= 4
    assert all(0.06 <= end - start <= .602 for _, _, start, end in audio)
    assert report["comparison"]["b3_minus_b2_flag_count"] == -2
    assert report["comparison"]["all_four_use_same_technical_analyzer"] is True
    assert report["comparison"]["edge_windows_are_not_identical_source_clock_speech"] is True
    assert report["source_clock_word_completeness_proven"] is False
    assert report["editorial_quality_improvement"] is None
    assert report["best_variant"] is None and report["ready"] is False
    assert all(isinstance(row["mean_dbfs"], float)
               for v in report["variants"].values()
               for row in v["edge_audio_samples"])
    receipts = list(options["preview_root"].rglob("*-speechb3-*.four-way-diagnostics.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text("utf-8")) == report
    assert (source.read_bytes(), asr.read_bytes(), edited.read_bytes()) == before
    with pytest.raises(ValueError, match="EXISTS"):
        qa.run_four_way_diagnostics(
            **options, execute=True, approve_local_preview=True,
        )
    assert len(scans) == 4


def test_failed_audio_measurement_does_not_create_report(tmp_path, monkeypatch):
    options, _, _, _ = _ready(tmp_path, monkeypatch)
    _probes(monkeypatch, fail_audio=True)
    with pytest.raises(ValueError, match="MEASUREMENT_FAILED"):
        qa.run_four_way_diagnostics(
            **options, execute=True, approve_local_preview=True,
        )
    assert not list(options["preview_root"].rglob("*-speechb3-*.four-way-diagnostics.json"))


def test_predecessor_mutation_during_scan_blocks_receipt(tmp_path, monkeypatch):
    options, source, _, _ = _ready(tmp_path, monkeypatch)
    _probes.source = source
    _probes(monkeypatch, change="source")
    with pytest.raises(ValueError):
        qa.run_four_way_diagnostics(
            **options, execute=True, approve_local_preview=True,
        )
    assert not list(options["preview_root"].rglob("*-speechb3-*.four-way-diagnostics.json"))


def test_corrupted_B3_finished_receipt_blocks_all_scans(tmp_path, monkeypatch):
    options, _, _, _ = _ready(tmp_path, monkeypatch)
    path = next(options["preview_root"].rglob("*-speechb3-*.matched.json"))
    receipt = json.loads(path.read_text("utf-8"))
    receipt["b3_final_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(qa, "_run", lambda **k: pytest.fail("must not scan"))
    with pytest.raises(ValueError):
        qa.run_four_way_diagnostics(
            **options, execute=True, approve_local_preview=True,
        )
    assert not list(options["preview_root"].rglob("*-speechb3-*.four-way-diagnostics.json"))


def test_cli_default_and_blocked_execution(tmp_path, monkeypatch):
    options, _, _, _ = _ready(tmp_path, monkeypatch)
    args = [
        "render-four-way-existing-media-diagnostics", *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == (
        "LOCAL_FOUR_WAY_DIAGNOSTIC_PREVIEW_NO_DECODING"
    )
    rejected = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert rejected.exit_code == 6
    assert json.loads(rejected.stdout)["ready"] is False
