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


def test_batch_continues_past_untrusted_completed_job(tmp_path, monkeypatch):
    options, _, _, _ = _completed(tmp_path, monkeypatch)
    actual = options["job_id"]
    missing = "renderqueue_" + "0" * 32
    monkeypatch.setattr(batch, "_completed_jobs",
                        lambda queue, *, job_id, max_jobs: ([actual, missing], 2))
    result = batch.run_existing_media_review_batch(**options, max_jobs=2)
    assert result["selected_job_count"] == 2
    assert result["cases"][0]["review_target_count"] >= 3
    assert result["cases"][1]["status"] in {
        "LOCAL_REVIEW_BATCH_PREREQUISITE_INCOMPLETE",
        "LOCAL_REVIEW_BATCH_INPUT_OR_LOCK_BLOCKED",
    }
    assert result["total_unrated_review_target_count"] == (
        result["cases"][0]["review_target_count"]
    )
    assert result["ready"] is False


def test_stale_acoustic_measurement_blocks_saved_review(tmp_path, monkeypatch):
    options, _, _, _ = _completed(tmp_path, monkeypatch)
    source = acoustic.prepare_three_way_acoustic(**options)["receipt"]
    source.write_bytes(b"stale source audio evidence")
    result = batch.run_existing_media_review_batch(**options, save=True)
    assert result["new_review_receipt_count"] == 0
    assert result["cases"][0]["review_target_count"] == 0
    assert not list(options["preview_root"].rglob("*.three-way-review.json"))



def test_paired_source_asr_coverage_is_attached_without_text(tmp_path, monkeypatch):
    options, _, media, asr = _completed(tmp_path, monkeypatch)
    baseline = media.read_bytes(), asr.read_bytes()
    result = batch.run_existing_media_review_batch(**options)
    coverage = result["cases"][0]["source_asr_coverage"]
    assert coverage["status"] == "LOCAL_SOURCE_ASR_EDIT_CLOCK_COVERAGE_NOT_HEARD_SPEECH"
    assert coverage["source_asr_sha256"] and len(coverage["source_asr_sha256"]) == 64
    assert set(coverage["boundary_source_seconds_by_variant"]) == {"a", "b", "b2"}
    assert isinstance(coverage["paired_coverage_deltas"]["b2_gains_full_vs_b"], int)
    assert coverage["asr_text_included"] is False
    assert coverage["source_asr_audio_verified"] is False
    assert coverage["best_variant"] is None and coverage["ready"] is False
    assert (media.read_bytes(), asr.read_bytes()) == baseline
    serialized = json.dumps(coverage, ensure_ascii=False)
    assert "まだ発話は続いている" not in serialized
    assert str(tmp_path) not in serialized


def test_source_clock_category_is_conservative_for_partial_and_repeated():
    from types import SimpleNamespace
    from holoclip.render_three_way_speech_coverage import classify_source_clock_interval

    s = SimpleNamespace(start=3.0, end=5.0)
    def cut(a, b):
        return SimpleNamespace(source_start=a, source_end=b)
    assert classify_source_clock_interval(s, [cut(2, 6)]) == (
        "ASR_INTERVAL_INSIDE_SINGLE_EDIT_SEGMENT"
    )
    assert classify_source_clock_interval(s, [cut(2, 4)]) == (
        "ASR_INTERVAL_PARTLY_IN_EDIT"
    )
    assert classify_source_clock_interval(s, [cut(5.2, 9)]) == (
        "ASR_INTERVAL_OUTSIDE_EDIT"
    )
    assert classify_source_clock_interval(s, [cut(2, 4), cut(4, 6)]) == (
        "MULTIPLE_SOURCE_EDIT_OVERLAPS_UNVERIFIED"
    )
    assert classify_source_clock_interval(s, [cut(2, 6), cut(3, 4)]) == (
        "MULTIPLE_SOURCE_EDIT_OVERLAPS_UNVERIFIED"
    )


def test_invalid_asr_identity_or_changed_evidence_blocks_saved_review(
    tmp_path, monkeypatch,
):
    options, _, _, asr = _completed(tmp_path, monkeypatch)
    asr.write_text('{"segments":[]}', encoding="utf-8")
    result = batch.run_existing_media_review_batch(**options, save=True)
    assert result["cases"][0]["review_target_count"] == 0
    assert result["new_review_receipt_count"] == 0
    assert not list(options["preview_root"].rglob("*.three-way-review.json"))
