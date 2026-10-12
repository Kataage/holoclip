"""Synthetic offline A/B/B2 batch review targets: no manual data preparation."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_existing_media_autopilot as autopilot
from holoclip import render_three_way_existing_media_review_batch as batch
from holoclip import render_cut_extension_three_way_diagnostics as diagnostics
from holoclip import render_cut_extension_three_way_acoustic as acoustic
from test_render_three_way_prerequisites import _case, _fake_scan, _patch_measure
from test_render_cut_extension_mp4_ab import _args


def _completed(tmp_path, monkeypatch):
    options, prepared, media, asr = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    done = autopilot.run_existing_media_autopilot(**options, execute=True)
    assert done["jobs"][0]["final_stage"] == "EVIDENCE_COMPLETE"
    return options, prepared, media, asr


def test_preview_has_grounded_unrated_targets_without_side_effects(
    tmp_path, monkeypatch,
):
    options, prepared, media, asr = _completed(tmp_path, monkeypatch)
    before = media.read_bytes(), asr.read_bytes()
    monkeypatch.setattr(
        diagnostics, "_run",
        lambda **kw: pytest.fail("batch review must not decode frames"),
    )
    monkeypatch.setattr(
        acoustic, "_measure",
        lambda *args: pytest.fail("batch review must not decode audio"),
    )
    result = batch.run_existing_media_review_batch(**options)
    row = result["cases"][0]
    assert row["next_stage"] == "EVIDENCE_COMPLETE"
    assert row["status"] == "LOCAL_REVIEW_BATCH_READ_ONLY_PREVIEW"
    assert row["review_target_count"] >= 3
    assert row["review_target_counts_by_variant"]["a"] >= 1
    assert row["review_target_counts_by_variant"]["b"] >= 1
    assert row["review_target_counts_by_variant"]["b2"] >= 1
    assert all("output_start_seconds" in r for r in row["priority_review_windows"])
    assert result["media_decoding_performed"] is False
    assert result["new_review_receipt_count"] == 0
    assert result["ready"] is False and result["auto_winner_selected"] is False
    assert (media.read_bytes(), asr.read_bytes()) == before
    assert str(tmp_path) not in json.dumps(result)
    assert "まだ発話は続いている" not in json.dumps(result, ensure_ascii=False)
    review = prepared["receipt"].with_name(
        f"{prepared['finished']['b2']['output'].stem}.three-way-review.json"
    )
    assert not review.exists()


def test_save_once_and_verified_resume_without_overwrite(tmp_path, monkeypatch):
    options, _, media, asr = _completed(tmp_path, monkeypatch)
    before = (media.read_bytes(), asr.read_bytes())
    created = batch.run_existing_media_review_batch(**options, save=True)
    row = created["cases"][0]
    assert row["status"] == "LOCAL_REVIEW_BATCH_NEW_RECEIPT_SAVED_UNRATED"
    assert row["saved_receipt_verified"] is True
    assert created["new_review_receipt_count"] == 1
    receipt_path = next(options["preview_root"].rglob("*.three-way-review.json"))
    original = receipt_path.read_bytes()
    again = batch.run_existing_media_review_batch(**options, save=True)
    assert again["cases"][0]["status"] == (
        "LOCAL_REVIEW_BATCH_EXISTING_RECEIPT_VERIFIED"
    )
    assert again["new_review_receipt_count"] == 0
    assert receipt_path.read_bytes() == original
    assert (media.read_bytes(), asr.read_bytes()) == before
    assert json.loads(original)["best_variant"] is None


def test_tampered_saved_plan_blocks_without_repair(tmp_path, monkeypatch):
    options, _, _, _ = _completed(tmp_path, monkeypatch)
    batch.run_existing_media_review_batch(**options, save=True)
    path = next(options["preview_root"].rglob("*.three-way-review.json"))
    path.write_text('{"status":"LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED"}')
    prior = path.read_bytes()
    result = batch.run_existing_media_review_batch(**options, save=True)
    assert result["cases"][0]["status"] == (
        "LOCAL_REVIEW_BATCH_EXISTING_RECEIPT_UNTRUSTED"
    )
    assert result["new_review_receipt_count"] == 0
    assert path.read_bytes() == prior


def test_incomplete_scan_never_creates_review_plan(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        diagnostics, "_run",
        lambda **kw: pytest.fail("must not decode"),
    )
    response = batch.run_existing_media_review_batch(**options, save=True)
    row = response["cases"][0]
    assert row["status"] == "LOCAL_REVIEW_BATCH_PREREQUISITE_INCOMPLETE"
    assert row["next_stage"] == "MEASURE_TECHNICAL"
    assert row["review_target_count"] == 0
    assert not list(options["preview_root"].rglob("*.three-way-review.json"))


def test_existing_unsafe_lock_is_never_stolen(tmp_path, monkeypatch):
    options, _, _, _ = _completed(tmp_path, monkeypatch)
    lock = options["preview_root"] / (
        f".holoclip-three-way-autopilot-{options['job_id']}.lock"
    )
    lock.write_text("another process owns this", encoding="utf-8")
    result = batch.run_existing_media_review_batch(**options, save=True)
    assert result["cases"][0]["status"] == "LOCAL_REVIEW_BATCH_INPUT_OR_LOCK_BLOCKED"
    assert result["new_review_receipt_count"] == 0
    assert lock.read_text() == "another process owns this"


def test_empty_missing_queue_and_invalid_bounds_are_fail_closed(tmp_path):
    absent = tmp_path / "not-there.sqlite"
    result = batch.run_existing_media_review_batch(queue=absent, save=True)
    assert result["selected_job_count"] == 0
    assert result["total_unrated_review_target_count"] == 0
    assert not absent.exists()
    with pytest.raises(ValueError):
        batch.run_existing_media_review_batch(queue=absent, max_jobs=5)
    with pytest.raises(ValueError):
        batch.run_existing_media_review_batch(queue=absent, proposal_index=2)
    with pytest.raises(ValueError):
        batch.run_existing_media_review_batch(queue=absent, save="yes")


def test_cli_preview_and_opt_in_save(tmp_path, monkeypatch):
    options, _, _, _ = _completed(tmp_path, monkeypatch)
    argv = [
        "render-three-way-existing-media-review-batch",
        *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, argv)
    assert preview.exit_code == 0, preview.stdout
    report = json.loads(preview.stdout)
    assert report["new_review_receipt_count"] == 0
    saved = CliRunner().invoke(cli.app, [*argv, "--save"])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(saved.stdout)["new_review_receipt_count"] == 1
