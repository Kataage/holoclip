"""Offline tests of complete B2/B3 masked playback and independent rating packet."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_blind_full_study as study


def _fixture(tmp_path, monkeypatch):
    a = tmp_path / "real-b2.mp4"
    b = tmp_path / "real-b3.mp4"
    a.write_bytes(b"finished b2 full local video " * 24)
    b.write_bytes(b"finished b3 full local video " * 27)
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    state = {
        "status": "SUPPORTED",
        "receipt": tmp_path / "original.review-00.playback.json",
        "basis": {"context": {
            "job_id": "renderqueue_" + "a" * 32,
            "roles": {
                "b2": {"final": a, "final_sha256": digest(a)},
                "b3": {"final": b, "final_sha256": digest(b)},
            },
        }},
    }
    evidence = {
        "status": "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED",
        "job_id": state["basis"]["context"]["job_id"],
        "b2_id": "b2-variant",
        "b3_id": "b3-variant",
        "playback_pack_receipt_sha256": "d" * 64,
    }
    monkeypatch.setattr(study, "_basis", lambda *args, **kwargs: state)
    monkeypatch.setattr(study, "run_editorial_evidence", lambda **kw: evidence)
    monkeypatch.setattr(study, "_exclusive_run_lock", lambda *a, **k: nullcontext())
    return a, b, state, evidence


def test_preview_does_not_create_media_or_rating(tmp_path, monkeypatch):
    a, b, state, _ = _fixture(tmp_path, monkeypatch)
    old = (a.read_bytes(), b.read_bytes())
    report = study.run_blind_full_study(preview_root=tmp_path)
    assert report["status"] == "BLIND_STUDY_READ_ONLY_PLAN"
    assert report["human_labels_obtained"] is False
    assert report["automatic_winner"] is None
    assert report["ready"] is False
    assert report["evaluator_role_mapping_disclosed"] is False
    assert not list(tmp_path.glob("*.blind*"))
    assert (a.read_bytes(), b.read_bytes()) == old
    assert str(tmp_path) not in json.dumps(report)


def test_execute_requires_explicit_approval(tmp_path, monkeypatch):
    a, b, _, _ = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        study.run_blind_full_study(preview_root=tmp_path, execute=True)
    assert len(list(tmp_path.glob("*.mp4"))) == 2


def test_create_isolated_blind_full_videos_without_changing_originals(
    tmp_path, monkeypatch,
):
    a, b, state, _ = _fixture(tmp_path, monkeypatch)
    original_a, original_b = a.read_bytes(), b.read_bytes()
    result = study.run_blind_full_study(
        preview_root=tmp_path, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "BLIND_STUDY_FULL_MEDIA_READY_UNRATED"
    folder = tmp_path / "original.review-00.playback.blind-study"
    map_path = tmp_path / "original.review-00.playback.blind-map.json"
    public = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    private = json.loads(map_path.read_text(encoding="utf-8"))
    assert public == result
    assert set(private["role_mapping"]) == {"candidate-1", "candidate-2"}
    assert {v["role"] for v in private["role_mapping"].values()} == {"b2", "b3"}
    assert private["ready"] is False
    assert not any((folder / f).is_symlink() for f in ("candidate-1.mp4", "candidate-2.mp4"))
    assert sorted([
        (folder / "candidate-1.mp4").read_bytes(),
        (folder / "candidate-2.mp4").read_bytes(),
    ]) == sorted([original_a, original_b])
    browser = (folder / "review.html").read_text(encoding="utf-8")
    assert "candidate-1.mp4" in browser and "candidate-2.mp4" in browser
    assert "real-b2.mp4" not in browser and "real-b3.mp4" not in browser
    assert "role_mapping" not in browser
    assert result["human_labels_obtained"] is False
    assert result["automatic_adoption_allowed"] is False
    # Reviewer copies must never share writable inodes with originals.
    (folder / "candidate-1.mp4").write_bytes(b"rated video was edited")
    assert (a.read_bytes(), b.read_bytes()) == (original_a, original_b)
    with pytest.raises(ValueError, match="OUTPUT_ALREADY_EXISTS"):
        study.run_blind_full_study(
            preview_root=tmp_path, execute=True, approve_local_preview=True,
        )
    assert (a.read_bytes(), b.read_bytes()) == (original_a, original_b)


def test_changed_original_media_blocks_before_creating_study(tmp_path, monkeypatch):
    _, b, _, _ = _fixture(tmp_path, monkeypatch)
    b.write_bytes(b"stale video")
    with pytest.raises(ValueError, match="FINISHED_MEDIA_CHANGED"):
        study.run_blind_full_study(
            preview_root=tmp_path, execute=True, approve_local_preview=True,
        )
    assert not list(tmp_path.glob("*.blind*"))


def test_interrupted_copy_is_never_marked_as_qualified(tmp_path, monkeypatch):
    _, b, _, _ = _fixture(tmp_path, monkeypatch)
    real_copy = study.shutil.copyfileobj
    mutated = False

    def copy_then_change(source, out, length):
        nonlocal mutated
        real_copy(source, out, length)
        if not mutated:
            mutated = True
            b.write_bytes(b"changed while copying")

    monkeypatch.setattr(study.shutil, "copyfileobj", copy_then_change)
    with pytest.raises(ValueError, match="CHANGED_DURING_COPY|FINISHED_MEDIA_CHANGED"):
        study.run_blind_full_study(
            preview_root=tmp_path, execute=True, approve_local_preview=True,
        )
    assert not list(tmp_path.glob("*.blind-map.json"))


def test_cli_dry_run_denied_and_approved(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    args = ["render-b2-b3-blind-full-study", "--preview-root", str(tmp_path)]
    runner = CliRunner()
    preview = runner.invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == "BLIND_STUDY_READ_ONLY_PLAN"
    denied = runner.invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    allowed = runner.invoke(cli.app, [
        *args, "--execute", "--approve-local-preview",
    ])
    assert allowed.exit_code == 0, allowed.stdout
    assert json.loads(allowed.stdout)["status"] == "BLIND_STUDY_FULL_MEDIA_READY_UNRATED"
