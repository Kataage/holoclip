"""Operator-local existing-video pilot: source-checked, no downloads, no auto-publish."""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_existing_media_pilot as pilot
from holoclip import render_cut_extension_three_way_diagnostics as diagnostic
from holoclip import render_cut_extension_three_way_acoustic as acoustic
from test_render_cut_extension_three_way_diagnostics import _ready, _fake_scan
from test_render_cut_extension_three_way_acoustic import _patch_measure
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    params, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    jid = prepared["finished"]["b2"]["prepared"]["trial"]["job_id"]
    proposal = prepared["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    return params, prepared, media, transcript, jid, proposal


def _run(params, job_id, proposal_index, *, execute=False):
    return pilot.run_existing_media_pilot(
        **params, job_id=job_id, proposal_index=proposal_index,
        execute=execute,
    )


def test_completed_queue_auto_discovery_and_default_preview_never_decodes(
    tmp_path, monkeypatch,
):
    params, plan, media, asr, jid, proposal = _case(tmp_path, monkeypatch)
    original = (media.read_bytes(), asr.read_bytes())
    monkeypatch.setattr(
        diagnostic, "_run", lambda **k: pytest.fail("must not scan"),
    )
    monkeypatch.setattr(
        acoustic, "_measure", lambda *a: pytest.fail("must not measure"),
    )
    report = pilot.run_existing_media_pilot(
        **params, proposal_index=proposal,
    )
    assert report["local_queue_present"] is True
    assert report["completed_queue_jobs_matching_filter"] >= 1
    assert report["selected_job_count"] == 1
    assert report["cases"][0]["job_id"] == jid
    assert report["cases"][0]["finished_triplet_evidence_valid"]
    assert report["cases"][0]["status"] == "LOCAL_PILOT_READY_FOR_MEASURED_SCAN"
    assert report["cases"][0]["technical_scan"] == "NEEDS_LOCAL_DECODE"
    assert report["completed_technical_and_audio_case_count"] == 0
    assert report["source_video_downloads_performed"] is False
    assert report["external_asr_or_llm_invoked"] is False
    assert report["auto_adoption_allowed"] is False
    assert report["ready"] is False
    assert not plan["receipt"].exists()
    assert (media.read_bytes(), asr.read_bytes()) == original
    assert str(tmp_path) not in json.dumps(report)


def test_execute_actual_local_video_evidence_once_and_resumes_by_verification(
    tmp_path, monkeypatch,
):
    params, prepared, media, asr, jid, proposal = _case(tmp_path, monkeypatch)
    before = media.read_bytes(), asr.read_bytes()
    calls = _fake_scan(monkeypatch)
    audio = _patch_measure(monkeypatch)
    first = _run(params, jid, proposal, execute=True)
    assert first["completed_technical_and_audio_case_count"] == 1
    assert first["new_local_decoded_media_case_count"] == 1
    row = first["cases"][0]
    assert row["status"] == "LOCAL_PILOT_TECHNICAL_AND_AUDIO_EVIDENCE_AVAILABLE"
    assert row["technical_scan"] == "NEW_LOCAL_DECODED_EVIDENCE"
    assert row["local_acoustic_scan"] == "NEW_LOCAL_DECODED_EVIDENCE"
    assert row["technical_warning_counts"] == {"a": 0, "b": 1, "b2": 0}
    assert row["acoustic_sample_count"] == len(audio)
    assert len(calls) == 3
    assert len(audio) >= 6
    acoustic_sidecar = acoustic.prepare_three_way_acoustic(
        **params, proposal_index=proposal,
    )["receipt"]
    assert acoustic_sidecar.is_file() and prepared["receipt"].is_file()
    monkeypatch.setattr(
        diagnostic, "_run", lambda **kwargs: pytest.fail("never rescan"),
    )
    monkeypatch.setattr(
        acoustic, "_measure", lambda *args: pytest.fail("never remeasure"),
    )
    again = _run(params, jid, proposal, execute=True)
    second = again["cases"][0]
    assert again["completed_technical_and_audio_case_count"] == 1
    assert again["new_local_decoded_media_case_count"] == 0
    assert second["technical_scan"] == "EXISTING_RECEIPT_VERIFIED"
    assert second["local_acoustic_scan"] == "EXISTING_RECEIPT_VERIFIED"
    assert (media.read_bytes(), asr.read_bytes()) == before
    assert again["real_world_editorial_accuracy_verified"] is False
    assert again["ready"] is False
    assert "まだ発話は続いている" not in json.dumps(again, ensure_ascii=False)


def test_partially_completed_run_recovers_from_diagnostics_without_overwrite(
    tmp_path, monkeypatch,
):
    params, plan, _, _, jid, proposal = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    diagnostic.run_three_way_diagnostics(plan, execute=True)
    before = plan["receipt"].read_bytes()
    result = _run(params, jid, proposal, execute=True)
    row = result["cases"][0]
    assert row["technical_scan"] == "EXISTING_RECEIPT_VERIFIED"
    assert row["local_acoustic_scan"] == "NEW_LOCAL_DECODED_EVIDENCE"
    assert plan["receipt"].read_bytes() == before


@pytest.mark.parametrize("mutate", ["diagnostics", "acoustic", "asr"])
def test_corrupted_evidence_fails_closed_and_never_repairs_or_overwrites(
    tmp_path, monkeypatch, mutate,
):
    params, plan, _, asr, jid, proposal = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    completed = _run(params, jid, proposal, execute=True)
    assert completed["completed_technical_and_audio_case_count"] == 1
    targets = {
        "diagnostics": plan["receipt"],
        "acoustic": acoustic.prepare_three_way_acoustic(
            **params, proposal_index=proposal,
        )["receipt"],
        "asr": asr,
    }
    file = targets[mutate]
    file.write_bytes(b"corrupted on local disk")
    previous = file.read_bytes()
    result = _run(params, jid, proposal, execute=True)
    assert result["cases"][0]["status"] == "LOCAL_PILOT_EVIDENCE_REJECTED_OR_SCAN_FAILED"
    assert result["completed_technical_and_audio_case_count"] == 0
    assert file.read_bytes() == previous
    assert result["ready"] is False


def test_render_queue_not_created_does_not_create_anything(tmp_path):
    missing = tmp_path / "absent.sqlite3"
    result = pilot.run_existing_media_pilot(queue=missing, max_jobs=2)
    assert result["local_queue_present"] is False
    assert result["selected_job_count"] == 0
    assert result["cases"] == []
    assert result["source_video_downloads_performed"] is False
    assert not missing.exists()


def test_queue_with_no_complete_jobs_remains_no_write_and_no_media(
    tmp_path, monkeypatch,
):
    params, _, _, _, jid, proposal = _case(tmp_path, monkeypatch)
    with sqlite3.connect(params["queue"]) as conn:
        conn.execute("UPDATE queue_jobs SET state='PAUSED' WHERE job_id=?", (jid,))
    result = _run(params, jid, proposal, execute=True)
    assert result["completed_queue_jobs_matching_filter"] == 0
    assert result["cases"] == []
    assert result["new_local_decoded_media_case_count"] == 0


@pytest.mark.parametrize("limit,job_id,proposal,refinement", [
    (0, None, 0, 0), (5, None, 0, 0),
    (2, "private-ident", 0, 0),
    (2, None, -1, 0), (2, None, 0, 3),
])
def test_invalid_job_bounds_and_ids_cannot_trigger_scanning(
    tmp_path, limit, job_id, proposal, refinement,
):
    with pytest.raises(ValueError):
        pilot.run_existing_media_pilot(
            queue=tmp_path / "empty.sqlite3",
            job_id=job_id, max_jobs=limit,
            proposal_index=proposal, refinement_index=refinement,
            execute=True,
        )


def test_cli_preview_and_explicit_existing_media_probes(
    tmp_path, monkeypatch,
):
    params, prepared, _, _, jid, proposal = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    monkeypatch.setattr(
        cli, "MeshTranscriber",
        lambda *a, **k: pytest.fail("remote ASR must never run"),
    )
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *a, **k: pytest.fail("remote LLM must never run"),
    )
    base = [
        "render-three-way-existing-media-pilot", *_args(params)[1:],
        "--proposal-index", str(proposal), "--job-id", jid,
    ]
    check = CliRunner().invoke(cli.app, base)
    assert check.exit_code == 0, check.stdout
    assert json.loads(check.stdout)["new_local_decoded_media_case_count"] == 0
    assert not prepared["receipt"].exists()
    executed = CliRunner().invoke(cli.app, [*base, "--execute"])
    assert executed.exit_code == 0, executed.stdout
    data = json.loads(executed.stdout)
    assert data["new_local_decoded_media_case_count"] == 1
    assert data["completed_technical_and_audio_case_count"] == 1
    assert data["ready"] is False and data["publication_performed"] is False
