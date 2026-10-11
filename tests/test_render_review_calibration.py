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
