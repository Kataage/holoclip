"""Offline unlabeled calibration sampling; provenance and fail-closed tests."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_review_calibration as cal


def _create_review(
    root, digit: str, *, kind="blackout", fast=False,
    media_changed=False, final_changed=False,
):
    root.mkdir(parents=True, exist_ok=True)
    job = "renderqueue_" + digit * 32
    original = root / f"{job}.mp4"
    original.write_bytes(b"original technical MP4 " + digit.encode())
    final = root / f"{job}.leveled.mp4"
    final.write_bytes(b"final MP4 " + digit.encode())
    sha = hashlib.sha256(original.read_bytes()).hexdigest()
    finalsha = hashlib.sha256(final.read_bytes()).hexdigest()
    candidate = {
        "kind": kind, "start_seconds": 1.2,
        "end_seconds": 2.0,
        "position": "inside_cut",
        "segment_ids": ["segment-0"],
        "auto_repair_allowed": False,
        "human_editorial_qa_completed": False,
    }
    if fast:
        candidate["kind"] = "fast_subtitle"
        candidate["cue_number"] = 1
    candidates = [] if kind is None else [candidate]
    ledger = {
        "version": cal.REVIEW_VERSION,
        "diagnostic_status": (
            "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS" if candidates
            else "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS"
        ),
        "base_sha256": sha,
        "final_sha256": finalsha,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "publication_performed": False,
        "human_editorial_qa_completed": False,
        "ready": False,
        "production_ready": False,
    }
    review = root / f"{job}.review.json"
    review.write_text(json.dumps(ledger), encoding="utf-8")
    if media_changed:
        original.write_bytes(b"changed original bytes")
    if final_changed:
        final.write_bytes(b"changed final bytes")
    return original, final, review


def test_bounded_balanced_sampling_and_zero_flags_are_not_human_negatives(tmp_path):
    root = tmp_path / "previews"
    _create_review(root, "a", kind="blackout")
    _create_review(root, "b", kind="freeze")
    _create_review(root, "c", kind="blackout")
    _create_review(root, "d", kind=None)
    report = cal.compile_review_calibration_pack(
        preview_root=root, example_store=tmp_path / "missing.jsonl",
    )
    assert report["status"] == "LOCAL_EDIT_PACK_UNLABELLED_REVIEW_READY"
    assert report["candidate_count"] == 3
    assert report["reviewed_job_count"] == 4
    assert report["calibration_metrics_available"] is False
    assert report["precision"] is None and report["false_positive_rate"] is None
    assert report["human_reference"]["existing_human_edit_quality_labels"] == 0
    assert [s["kind"] for s in report["candidate_samples"]] == [
        "blackout", "freeze", "blackout",
    ]
    assert report["unlabelled_controls"][0]["classification"] == (
        "NO_DETECTOR_FLAGS_NOT_HUMAN_GOOD"
    )
    assert not report["ready"] and not report["auto_repair_allowed"]
    assert report == cal.compile_review_calibration_pack(
        preview_root=root, example_store=tmp_path / "missing.jsonl",
    )


def test_changed_media_receipts_are_ignored_without_fake_controls(tmp_path):
    root = tmp_path / "previews"
    _create_review(root, "a", media_changed=True)
    _create_review(root, "b", final_changed=True)
    report = cal.compile_review_calibration_pack(
        preview_root=root, example_store=tmp_path / "missing.jsonl",
    )
    assert report["stale_media_job_count"] == 2
    assert report["candidate_count"] == 0
    assert report["status"] == "LOCAL_EDIT_PACK_NO_VERIFIED_REVIEWS"


@pytest.mark.parametrize("damage", ["wrong_status", "gold_claim", "wrong_count", "unsafe_candidate"])
def test_tampered_ledger_blocks_pack_instead_of_blessing_data(tmp_path, damage):
    root = tmp_path / "previews"
    _, _, review = _create_review(root, "a")
    doc = json.loads(review.read_text())
    if damage == "wrong_status":
        doc["diagnostic_status"] = "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"
    elif damage == "gold_claim":
        doc["human_editorial_qa_completed"] = True
    elif damage == "wrong_count":
        doc["candidate_count"] = 5
    else:
        doc["candidates"][0]["auto_repair_allowed"] = True
    review.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="LOCAL_EDIT_PACK"):
        cal.compile_review_calibration_pack(
            preview_root=root, example_store=tmp_path / "missing.jsonl",
        )


def test_symlinked_review_data_is_rejected(tmp_path):
    source = tmp_path / "outside"
    _, _, review = _create_review(source, "a")
    root = tmp_path / "previews"
    root.mkdir()
    try:
        (root / review.name).symlink_to(review)
    except (OSError, NotImplementedError):
        pytest.skip("No symlink permission")
    with pytest.raises(ValueError, match="LOCAL_RENDER_LINKED_PATH|LOCAL_EDIT_PACK"):
        cal.compile_review_calibration_pack(
            preview_root=root, example_store=tmp_path / "missing.jsonl",
        )


def test_readonly_default_and_create_only_cli(tmp_path):
    root = tmp_path / "previews"
    _create_review(root, "f", kind="blackout")
    output = tmp_path / "pack.json"
    args = [
        "render-review-calibration-pack", "--preview-root", str(root),
        "--example-store", str(tmp_path / "missing.jsonl"),
        "--output", str(output),
    ]
    pre = CliRunner().invoke(cli.app, args)
    assert pre.exit_code == 0, pre.stdout
    assert not output.exists()
    assert json.loads(pre.stdout)["candidate_count"] == 1
    live = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert live.exit_code == 0, live.stdout
    assert json.loads(output.read_text())["precision"] is None
    old = output.read_bytes()
    again = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert again.exit_code == 6
    assert output.read_bytes() == old


def test_insufficient_corpus_is_not_human_quality_gold(tmp_path):
    root = tmp_path / "previews"
    _create_review(root, "f", kind=None)
    store = tmp_path / "examples.jsonl"
    store.write_text(
        json.dumps({
            "id": "example", "clip_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "clip_title": "Some human clip", "source_start": 10, "source_end": 20,
            "alignment_confidence": 0.50,
        }) + "\n",
        encoding="utf-8",
    )
    report = cal.compile_review_calibration_pack(
        preview_root=root, example_store=store,
    )
    assert report["human_reference"]["store_available"] is True
    assert report["human_reference"]["trusted_alignment_examples"] == 0
    assert report["human_reference"]["existing_human_edit_quality_labels"] == 0
    assert report["status"] == "LOCAL_EDIT_PACK_UNLABELLED_REVIEW_READY"



def test_large_backlog_paginates_without_silently_discarding_reviews(tmp_path):
    root = tmp_path / "previews"
    root.mkdir()
    # 47 distinct, entirely synthetic, hash-verified historical media records.
    for i in range(47):
        job = "renderqueue_" + f"{i:032x}"
        media = root / f"{job}.mp4"
        media.write_bytes(f"synthetic media bytes {i}".encode())
        sha = hashlib.sha256(media.read_bytes()).hexdigest()
        review = {
            "version": cal.REVIEW_VERSION,
            "diagnostic_status": "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS",
            "base_sha256": sha,
            "final_sha256": sha,
            "candidate_count": 0,
            "candidates": [],
            "ready": False, "production_ready": False,
            "human_editorial_qa_completed": False,
            "publication_performed": False,
        }
        (root / f"{job}.review.json").write_text(
            json.dumps(review), encoding="utf-8",
        )
    first = cal.compile_review_calibration_pack(
        preview_root=root, example_store=tmp_path / "missing",
    )
    assert first["window_job_count"] == 40
    assert first["inventory_job_count"] == 47
    assert first["reviewed_job_count"] == 40
    assert first["has_more"] is True
    assert first["inventory_complete"] is False
    assert first["next_cursor"] == "renderqueue_" + f"{39:032x}"
    second = cal.compile_review_calibration_pack(
        preview_root=root, example_store=tmp_path / "missing",
        after_job_id=first["next_cursor"],
    )
    assert second["window_job_count"] == 7
    assert second["reviewed_job_count"] == 7
    assert len(second["unlabelled_controls"]) == 7
    assert second["has_more"] is False
    assert second["after_job_id"] == first["next_cursor"]
    assert second["inventory_complete"] is False
    assert second["next_cursor"] == "renderqueue_" + f"{46:032x}"
    assert {
        item["job_id"] for item in first["unlabelled_controls"]
    }.isdisjoint({
        item["job_id"] for item in second["unlabelled_controls"]
    })


def test_invalid_cursor_rejected_without_touching_disk(tmp_path):
    root = tmp_path / "reviews"
    for bad in ("..", "", "renderqueue_" + "z" * 32, "renderqueue_" + "a" * 33):
        with pytest.raises(ValueError, match="LOCAL_EDIT_PACK_CURSOR_UNTRUSTED"):
            cal.compile_review_calibration_pack(
                preview_root=root, example_store=tmp_path / "missing",
                after_job_id=bad,
            )
    assert not root.exists()


def test_cli_second_page_uses_cursor_without_dangerous_rewrites(tmp_path):
    root = tmp_path / "previews"
    _create_review(root, "a", kind="blackout")
    _create_review(root, "b", kind="freeze")
    runner = CliRunner()
    args = [
        "render-review-calibration-pack", "--preview-root", str(root),
        "--example-store", str(tmp_path / "missing"),
        "--max-jobs", "1",
    ]
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.stdout
    first_doc = json.loads(first.stdout)
    assert first_doc["has_more"] is True
    second = runner.invoke(
        cli.app, [*args, "--after-job-id", first_doc["next_cursor"]],
    )
    assert second.exit_code == 0, second.stdout
    second_doc = json.loads(second.stdout)
    assert second_doc["has_more"] is False
    assert second_doc["candidate_samples"][0]["kind"] == "freeze"
    assert second_doc["candidate_samples"][0]["job_id"] != (
        first_doc["candidate_samples"][0]["job_id"]
    )


def test_incremental_archive_creates_independent_immutable_unlabelled_receipt(tmp_path):
    root = tmp_path / "previews"
    media, final, ledger = _create_review(root, "a", kind="blackout")
    ledger_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    base_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    final_sha = hashlib.sha256(final.read_bytes()).hexdigest()
    params = dict(
        mp4_output=media, review_sha256=ledger_sha,
        video_sha256=base_sha, final_sha256=final_sha,
    )
    before = (media.read_bytes(), final.read_bytes(), ledger.read_bytes())
    first = cal.archive_incremental_review(**params)
    assert first["status"] == "LOCAL_EDIT_ARCHIVE_CREATED_UNLABELLED"
    assert first["candidate_count"] == 1
    archive = root / cal.ARCHIVE_SUBDIR / f"{media.stem}.json"
    assert archive.is_file()
    data = archive.read_bytes()
    assert hashlib.sha256(data).hexdigest() == first["sha256"]
    snapshot = json.loads(data)
    assert snapshot["review_sha256"] == ledger_sha
    assert snapshot["base_sha256"] == base_sha
    assert snapshot["final_sha256"] == final_sha
    assert snapshot["anomaly_counts"] == {"blackout": 1}
    assert snapshot["media_hash_reverified_for_calibration"] is False
    assert snapshot["human_quality_labels_available"] is False
    assert snapshot["auto_repair_allowed"] is False
    assert snapshot["ready"] is False
    assert (media.read_bytes(), final.read_bytes(), ledger.read_bytes()) == before
    assert cal.archive_incremental_review(**params)["status"] == (
        "LOCAL_EDIT_ARCHIVE_UNAVAILABLE"
    )
    assert archive.read_bytes() == data


def test_incremental_archive_rejects_changed_review_and_wrong_qa_identity(tmp_path):
    root = tmp_path / "previews"
    media, final, ledger = _create_review(root, "a", kind="blackout")
    base_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    final_sha = hashlib.sha256(final.read_bytes()).hexdigest()
    params = dict(
        mp4_output=media, review_sha256="f" * 64,
        video_sha256=base_sha, final_sha256=final_sha,
    )
    assert cal.archive_incremental_review(**params)["status"] == (
        "LOCAL_EDIT_ARCHIVE_UNAVAILABLE"
    )
    params["review_sha256"] = hashlib.sha256(ledger.read_bytes()).hexdigest()
    params["final_sha256"] = "a" * 64
    assert cal.archive_incremental_review(**params)["status"] == (
        "LOCAL_EDIT_ARCHIVE_UNAVAILABLE"
    )
    assert not (root / cal.ARCHIVE_SUBDIR).exists()


def test_incremental_archive_does_not_mistake_invalid_flags_for_human_gold(tmp_path):
    root = tmp_path / "previews"
    media, final, ledger = _create_review(root, "a", kind="blackout")
    doc = json.loads(ledger.read_text())
    doc["candidates"][0]["auto_repair_allowed"] = True
    ledger.write_text(json.dumps(doc), encoding="utf-8")
    evidence = cal.archive_incremental_review(
        mp4_output=media,
        review_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
        video_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(final.read_bytes()).hexdigest(),
    )
    assert evidence["status"] == "LOCAL_EDIT_ARCHIVE_UNAVAILABLE"
    assert not (root / cal.ARCHIVE_SUBDIR).exists()
