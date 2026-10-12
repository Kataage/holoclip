"""Offline staged prerequisites on canonical local A/B/B2 assets.

These are strict inventory checks, not automatic media production or gold QA.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_prerequisites as doctor
from holoclip import render_cut_extension_three_way_diagnostics as diagnostics
from holoclip import render_cut_extension_three_way_acoustic as acoustic
from holoclip.render_cut_extension_finish_ab import _stage_paths
from test_render_three_way_existing_media_pilot import _case as _completed
from test_render_cut_extension_three_way_diagnostics import _fake_scan
from test_render_cut_extension_three_way_acoustic import _patch_measure
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    params, completed, media, asr, jid, proposal = _completed(tmp_path, monkeypatch)
    options = {**params, "job_id": jid, "proposal_index": proposal}
    return options, completed, media, asr


def _inspect(options):
    return doctor.inspect_existing_media_prerequisites(**options)


def _first(options):
    response = _inspect(options)
    assert response["jobs_selected"] == 1
    return response["cases"][0]


def test_canonical_default_diagnoses_real_technical_missing_without_decoding(
    tmp_path, monkeypatch,
):
    options, prepared, media, asr = _case(tmp_path, monkeypatch)
    before = (media.read_bytes(), asr.read_bytes())
    monkeypatch.setattr(
        diagnostics, "_run",
        lambda **kwargs: pytest.fail("doctor must not decode frames"),
    )
    monkeypatch.setattr(
        acoustic, "_measure",
        lambda *a: pytest.fail("doctor must not decode audio"),
    )
    row = _first(options)
    assert row["next_stage"] == "MEASURE_TECHNICAL"
    assert row["next_command"] == "render-cut-extension-three-way-diagnostics"
    assert row["completed_verified_stages"] == [
        "CANONICAL_SOURCE_AND_AB_PROPOSAL",
        "B_RENDER_QA", "AB_MATCHED_FINISHING",
        "SOURCE_ASR_B2_REFINEMENT", "B2_RENDER_QA",
        "FINISHED_A_B_B2_SOURCE_LINKED",
    ]
    assert row["action_executed"] is False and row["ready"] is False
    assert row["operator_local_approval_required"] is False
    report = _inspect(options)
    assert report["stage_counts"]["MEASURE_TECHNICAL"] == 1
    assert report["source_video_downloads_performed"] is False
    assert report["media_decoding_performed"] is False
    assert report["all_commands_are_suggestions_not_executed"] is True
    assert not prepared["receipt"].exists()
    assert (media.read_bytes(), asr.read_bytes()) == before
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "まだ発話は続いている" not in serialized


def test_identifies_audio_measurement_then_fully_pinned_evidence(
    tmp_path, monkeypatch,
):
    options, prepared, _, _ = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    technical = diagnostics.run_three_way_diagnostics(prepared, execute=True)
    assert technical["actual_media_scanned"] is True
    row = _first(options)
    assert row["next_stage"] == "MEASURE_ACOUSTIC"
    assert row["next_command"] == "render-cut-extension-three-way-acoustic"
    measured = acoustic.run_three_way_acoustic(
        acoustic.prepare_three_way_acoustic(**options),
        execute=True,
    )
    assert measured["measured_audio_sample_count"] >= 6
    full = _first(options)
    assert full["next_stage"] == "EVIDENCE_COMPLETE"
    assert full["next_command"] is None
    assert "REAL_LOCAL_ACOUSTIC_SAMPLES" in full["completed_verified_stages"]
    assert full["quality_verified"] is False
    assert full["ready"] is False


def test_B2_matched_finishing_needed_when_render_is_only_remaining_artifact(
    tmp_path, monkeypatch,
):
    options, completed, _, _ = _case(tmp_path, monkeypatch)
    b2 = completed["finished"]["b2"]
    b2paths = _stage_paths(b2["output"])
    for stage in ("audio", "caption", "burnin"):
        b2paths[stage].unlink()
    completed["finished"]["finished_receipt"].unlink()
    row = _first(options)
    assert row["next_stage"] == "FINISH_B2"
    assert row["next_command"] == "render-cut-extension-speech-b2-finish"
    assert row["operator_local_approval_required"] is True
    assert "B2_RENDER_QA" in row["completed_verified_stages"]


def test_B2_render_needed_only_if_original_and_descendants_absent(
    tmp_path, monkeypatch,
):
    options, completed, _, _ = _case(tmp_path, monkeypatch)
    b2 = completed["finished"]["b2"]
    descendants = _stage_paths(b2["output"])
    for stage in ("audio", "caption", "burnin"):
        descendants[stage].unlink()
    completed["finished"]["finished_receipt"].unlink()
    b2["output"].unlink()
    b2["receipt"].unlink()
    row = _first(options)
    assert row["next_stage"] == "RENDER_B2"
    assert row["next_command"] == "render-cut-extension-speech-b2"
    assert row["operator_local_approval_required"] is True


def test_partial_b2_descendants_without_original_block_fresh_render(
    tmp_path, monkeypatch,
):
    options, completed, _, _ = _case(tmp_path, monkeypatch)
    b2 = completed["finished"]["b2"]
    b2["output"].unlink()
    b2["receipt"].unlink()
    original = completed["finished"]["finished_receipt"].read_bytes()
    row = _first(options)
    assert row["next_stage"] == "BLOCKED_UNVERIFIED"
    assert row["blocking_reason"] == "B2_DESCENDANTS_EXIST_WITHOUT_RENDER_PROOF"
    assert row["next_command"] is None
    assert completed["finished"]["finished_receipt"].read_bytes() == original


def test_matching_AB_needs_processing_when_B_proof_remains(
    tmp_path, monkeypatch,
):
    options, completed, _, _ = _case(tmp_path, monkeypatch)
    matched = completed["finished"]["b2"]["prepared"]
    trial = matched["trial"]
    matched["matched_receipt"].unlink()
    for stage in ("audio", "caption", "burnin"):
        _stage_paths(trial["output"])[stage].unlink()
    row = _first(options)
    assert row["next_stage"] == "FINISH_AB"
    assert row["next_command"] == "render-cut-extension-finished-ab"
    assert "B_RENDER_QA" in row["completed_verified_stages"]


def test_partially_corrupt_AB_receipt_never_recommended_as_missing(
    tmp_path, monkeypatch,
):
    options, completed, _, _ = _case(tmp_path, monkeypatch)
    receipt = completed["finished"]["b2"]["prepared"]["matched_receipt"]
    receipt.write_bytes(b"not valid JSON")
    row = _first(options)
    assert row["next_stage"] == "BLOCKED_UNVERIFIED"
    assert row["blocking_reason"] == "MATCHED_AB_PROOF_UNTRUSTED"
    assert row["next_command"] is None


def test_corrupted_source_ASR_fails_closed_no_quality_or_new_commands(
    tmp_path, monkeypatch,
):
    options, _, _, asr = _case(tmp_path, monkeypatch)
    asr.write_bytes(b"corrupted source ASR")
    row = _first(options)
    assert row["next_stage"] in ("NO_VERIFIED_AB_PROPOSAL", "BLOCKED_UNVERIFIED")
    assert row["next_command"] is None and row["ready"] is False
    assert row["action_executed"] is False


def test_missing_queue_creates_nothing(tmp_path):
    target = tmp_path / "queue.sqlite3"
    report = doctor.inspect_existing_media_prerequisites(queue=target)
    assert report["jobs_selected"] == 0
    assert sum(report["stage_counts"].values()) == 0
    assert report["source_video_downloads_performed"] is False
    assert not target.exists()


def test_cli_doctor_and_bounded_arguments(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    base = [
        "render-three-way-existing-media-doctor",
        *_args(options)[1:],
        "--proposal-index", str(options["proposal_index"]),
    ]
    result = CliRunner().invoke(cli.app, base)
    assert result.exit_code == 0, result.stdout
    parsed = json.loads(result.stdout)
    assert parsed["stage_counts"]["MEASURE_TECHNICAL"] == 1
    assert parsed["ready"] is False
    assert parsed["publication_performed"] is False
    assert str(tmp_path) not in result.stdout
    invalid = CliRunner().invoke(cli.app, [*base, "--max-jobs", "10"])
    assert invalid.exit_code != 0
    with pytest.raises(ValueError):
        doctor.inspect_existing_media_prerequisites(
            queue=options["queue"], max_jobs=5,
        )
