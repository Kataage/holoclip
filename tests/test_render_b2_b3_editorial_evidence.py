"""Offline regression coverage for creative-evidence triage (all media synthetic)."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_editorial_evidence as editorial
from holoclip import render_b2_b3_local_review_pack as pack
from test_render_b2_b3_local_review_pack import _case, _fake_media
from test_render_cut_extension_mp4_ab import _args


def _rendered(tmp_path, monkeypatch):
    options, *_ = _case(tmp_path, monkeypatch)
    state = pack._basis(options, 0)
    _fake_media(tmp_path, monkeypatch, state)
    output = pack.run_local_review_pack(
        **options, execute=True, approve_local_preview=True,
    )
    assert output["status"] == "LOCAL_REVIEW_PACK_LOCAL_CLIPS_UNRATED"
    return options, state


def test_no_pack_is_explicitly_unknown_without_video_or_network(tmp_path, monkeypatch):
    options, *_ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        editorial, "_hash",
        lambda *a, **kw: pytest.fail("no clips to hash"),
    )
    result = editorial.run_editorial_evidence(**options)
    assert result["status"] == "LOCAL_EDITORIAL_EVIDENCE_PLAYBACK_PACK_NOT_RENDERED"
    assert result["ready"] is False
    assert not list(options["preview_root"].rglob("*.creative-evidence.json"))


def test_audits_all_three_clips_and_abstains_from_all_creative_scores(
    tmp_path, monkeypatch,
):
    options, state = _rendered(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pack, "_encode", lambda *a, **kw: pytest.fail("no new FFmpeg"),
    )
    report = editorial.run_editorial_evidence(**options)
    assert report["status"] == "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED"
    assert set(report["verified_playback_clips"]) == {"source", "b2", "b3"}
    assert report["review_order"] == ["source", "b2", "b3"]
    assert set(report["dimensions"]) == {
        "speech_naturalness", "narrative_continuity", "payoff_completion",
    }
    assert all(v["score"] is None for v in report["dimensions"].values())
    assert report["automatic_winner"] is None
    assert report["ready"] is False and report["production_ready"] is False
    assert report["automatic_adoption_allowed"] is False
    assert report["publication_performed"] is False
    assert report["new_provider_request"] is False
    assert report["source_video_downloaded"] is False
    assert str(tmp_path) not in json.dumps(report)
    assert not list(options["preview_root"].rglob("*.creative-evidence.json"))
    assert state["receipt"].exists()


@pytest.mark.parametrize("role", ["source", "b2", "b3"])
def test_changed_clip_bytes_block_even_when_receipt_is_unchanged(
    tmp_path, monkeypatch, role,
):
    options, state = _rendered(tmp_path, monkeypatch)
    path = state["artifacts"][role]
    original = path.read_bytes()
    path.write_bytes(original + b"tamper")
    with pytest.raises(ValueError, match="CLIP_UNTRUSTED"):
        editorial.run_editorial_evidence(**options)
    path.write_bytes(original)


def test_modified_receipt_target_blocks_quality_review(tmp_path, monkeypatch):
    options, state = _rendered(tmp_path, monkeypatch)
    report = json.loads(state["receipt"].read_text(encoding="utf-8"))
    report["review_target_priority"] = "LOW_REVIEW"
    state["receipt"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="LINEAGE_CHANGED"):
        editorial.run_editorial_evidence(**options)


def test_saved_report_immutable_and_still_not_qualified(tmp_path, monkeypatch):
    options, state = _rendered(tmp_path, monkeypatch)
    report = editorial.run_editorial_evidence(**options, save=True)
    output = state["receipt"].with_name(
        f"{state['receipt'].stem}.creative-evidence.json"
    )
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["ready"] is False
    before = output.read_bytes()
    with pytest.raises((ValueError, FileExistsError, OSError)):
        editorial.run_editorial_evidence(**options, save=True)
    assert output.read_bytes() == before


def test_cli_offline_dry_run_and_fail_closed_on_tamper(tmp_path, monkeypatch):
    options, state = _rendered(tmp_path, monkeypatch)
    argv = [
        "render-b2-b3-editorial-evidence", *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["dimensions"]["payoff_completion"]["score"] is None
    state["artifacts"]["b2"].write_bytes(b"untrusted")
    denied = CliRunner().invoke(cli.app, argv)
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
