"""Offline parity of optional audio/caption finishing for cut-extension B."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_mp4_ab as basic
from holoclip import render_cut_extension_finish_ab as finish
from test_render_cut_extension_mp4_ab import (
    _inputs, _offline_render, _args as basic_args,
)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stages(params, media, *, audio=True, caption=True, burnin=True):
    """Install synthetic copies of the exact original QA evidence schema."""
    assert not burnin or caption
    root = finish._stage_paths(media)
    for name, yes, body in (
        ("audio", audio, b"original leveled MP4"),
        ("caption", caption, b"1\n00:00:00,300 --> 00:00:00,900\noriginal\n"),
        ("burnin", burnin, b"original captioned MP4"),
    ):
        if yes:
            root[name].write_bytes(body)
    with sqlite3.connect(params["queue"]) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS render_caption_evidence (
            job_id TEXT PRIMARY KEY,status TEXT NOT NULL,
            cue_count INTEGER NOT NULL,dropped_cue_count INTEGER NOT NULL,
            output_sha256 TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS render_audio_evidence (
            job_id TEXT PRIMARY KEY,status TEXT NOT NULL,
            output_sha256 TEXT NOT NULL,cut_count INTEGER NOT NULL,
            max_abs_gain_db REAL NOT NULL,before_mean_spread_db REAL,
            after_mean_spread_db REAL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS render_burnin_evidence (
            job_id TEXT PRIMARY KEY,status TEXT NOT NULL,
            output_sha256 TEXT NOT NULL,subtitle_sha256 TEXT NOT NULL,
            updated_at REAL NOT NULL);
        """)
        job = db.execute("SELECT job_id FROM render_jobs").fetchone()[0]
        db.execute(
            "INSERT INTO render_caption_evidence VALUES(?,?,?,?,?,?)",
            (job, finish._SUCCESS["caption"] if caption else
             "LOCAL_SUBTITLE_NO_FULL_UTTERANCES",
             1 if caption else 0, 0, _sha(root["caption"]) if caption else "", time.time()),
        )
        db.execute(
            "INSERT INTO render_audio_evidence VALUES(?,?,?,?,?,?,?,?)",
            (job, finish._SUCCESS["audio"] if audio else
             "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED",
             _sha(root["audio"]) if audio else "", 2 if audio else 0,
             0.0, None, None, time.time()),
        )
        db.execute(
            "INSERT INTO render_burnin_evidence VALUES(?,?,?,?,?)",
            (job, finish._SUCCESS["burnin"] if burnin else
             "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
             _sha(root["burnin"]) if burnin else "",
             _sha(root["caption"]) if caption else "", time.time()),
        )
    return root


def _monkeypatch_video(monkeypatch):
    calls, policies = _offline_render(monkeypatch)
    monkeypatch.setattr(
        finish, "_probe",
        lambda path: (7.0 if "-extension-" in path.name else 6.0, True, True),
    )
    checked = []
    monkeypatch.setattr(
        finish, "_offline_render_policy",
        lambda *args: checked.append(("policy", len(args))),
    )
    subtitle = []
    def fake_caption(timeline, sources, *, transcript_root, mp4_output):
        subtitle.append((timeline, transcript_root))
        out = mp4_output.with_suffix(".srt")
        data = b"1\n00:00:00,300 --> 00:00:00,900\ntrial captions\n"
        out.write_bytes(data)
        return {
            "status": finish._SUCCESS["caption"],
            "output_sha256": hashlib.sha256(data).hexdigest(),
            "cue_count": 1, "dropped_cue_count": 0,
        }
    monkeypatch.setattr(finish, "export_cut_aligned_srt", fake_caption)
    leveling = []
    def fake_level(*, mp4_output, video_sha256, timeline):
        leveling.append((mp4_output, video_sha256, timeline))
        target = mp4_output.with_name(mp4_output.stem + ".leveled.mp4")
        target.write_bytes(b"trial leveled MP4")
        return {
            "status": finish._SUCCESS["audio"], "output_sha256": _sha(target),
            "max_abs_gain_db": 2.0,
        }
    monkeypatch.setattr(finish, "level_preview_audio", fake_level)
    burned = []
    def fake_burn(*, mp4_output, video_sha256, subtitle_sha256):
        burned.append((mp4_output, video_sha256, subtitle_sha256))
        stem = mp4_output.stem
        if stem.endswith(".leveled"):
            stem = stem[:-8]
        target = mp4_output.with_name(stem + ".captioned.mp4")
        target.write_bytes(b"trial captioned MP4")
        return {"status": finish._SUCCESS["burnin"], "output_sha256": _sha(target)}
    monkeypatch.setattr(finish, "burn_captioned_preview", fake_burn)
    return calls, policies, checked, subtitle, leveling, burned


def test_default_preview_has_no_writes_and_refuses_fake_editorial_quality(tmp_path):
    params, _, _, media, _ = _inputs(tmp_path)
    base = _stages(params, media)
    original = {k: p.read_bytes() for k, p in base.items() if p.is_file()}
    prepared = finish.prepare_matched_finishing_trial(**params)
    report = finish.run_matched_finishing_trial(prepared)
    assert report["status"] == "LOCAL_MATCHED_AB_NOT_EXECUTED"
    assert report["baseline_audio_leveling_expected"] is True
    assert report["baseline_srt_expected"] is True
    assert report["baseline_burnin_expected"] is True
    assert report["stage_comparable"] is False
    assert report["editorial_comparable"] is False
    assert report["semantic_quality_measured"] is False
    assert report["quality_difference"] is None
    assert report["ready"] is False and report["auto_adoption_allowed"] is False
    assert not prepared["matched_receipt"].exists()
    assert all(p.read_bytes() == original[k] for k, p in base.items() if k in original)
    assert params["growth_state"].exists()
    assert "00:00:00,300" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
    assert "abcDEFgh012" not in json.dumps(report)


def test_no_hidden_permission_to_start_ffmpeg(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media)
    prepared = finish.prepare_matched_finishing_trial(**params)
    monkeypatch.setattr(
        finish, "run_cut_extension_mp4_trial",
        lambda *a, **kw: pytest.fail("must not render"),
    )
    with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
        finish.run_matched_finishing_trial(prepared, execute=True)
    assert not prepared["trial"]["output"].exists()


def test_missing_original_audio_subtitle_qa_rows_fail_closed(tmp_path):
    params, *_ = _inputs(tmp_path)
    with pytest.raises(ValueError, match="EVIDENCE_(MISSING|UNTRUSTED)"):
        finish.prepare_matched_finishing_trial(**params)


def test_original_srt_hash_mismatch_refuses_before_trial_render(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    base = _stages(params, media)
    prepared = finish.prepare_matched_finishing_trial(**params)
    base["caption"].write_text("changed private subtitle", encoding="utf-8")
    _monkeypatch_video(monkeypatch)
    with pytest.raises(ValueError, match="BASELINE_STAGE_CHANGED"):
        finish.run_matched_finishing_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not prepared["trial"]["output"].exists()


def test_undocumented_original_leveled_file_rejected(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    base = _stages(params, media, audio=False, caption=False, burnin=False)
    prepared = finish.prepare_matched_finishing_trial(**params)
    base["audio"].write_bytes(b"not recorded by queue")
    _monkeypatch_video(monkeypatch)
    with pytest.raises(ValueError, match="BASELINE_STAGE_AMBIGUOUS"):
        finish.run_matched_finishing_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not prepared["trial"]["output"].exists()


def test_full_finished_trial_reuses_base_protection_and_creates_new_stage_artifacts(
    tmp_path, monkeypatch,
):
    params, original_receipt, _, media, transcript = _inputs(tmp_path)
    base = _stages(params, media)
    original = {k: p.read_bytes() for k, p in base.items() if p.is_file()}
    original_transcript = transcript.read_bytes()
    baseline_hash = _sha(media)
    prepared = finish.prepare_matched_finishing_trial(**params)
    calls, policies, checked, subs, levels, burns = _monkeypatch_video(monkeypatch)
    result = finish.run_matched_finishing_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_MATCHED_AB_STAGES_TECHNICALLY_COMPARABLE"
    assert result["stage_comparable"] is True
    assert result["editorial_comparable"] is False
    assert len(calls) == len(policies) == len(checked) == 1
    assert len(subs) == len(levels) == len(burns) == 1
    assert result["baseline_final_filename"] == base["burnin"].name
    assert result["baseline_final_sha256"] == _sha(base["burnin"])
    assert result["trial_final_filename"].endswith(".captioned.mp4")
    assert result["trial_final_sha256"] == _sha(
        prepared["trial"]["output"].with_name(result["trial_final_filename"])
    )
    assert result["trial_caption_cue_count"] == 1
    assert result["trial_audio_max_abs_gain_db"] == 2.0
    assert result["trial_audio_status"] == finish._SUCCESS["audio"]
    assert result["trial_burnin_status"] == finish._SUCCESS["burnin"]
    assert result["trial_caption_status"] == finish._SUCCESS["caption"]
    assert result["quality_difference"] is None
    assert result["semantic_quality_measured"] is False
    assert result["ready"] is False and result["auto_adoption_allowed"] is False
    assert result["publication_performed"] is False
    assert _sha(media) == baseline_hash
    assert transcript.read_bytes() == original_transcript
    assert all(p.read_bytes() == original[k] for k, p in base.items() if k in original)
    receipt = json.loads(prepared["matched_receipt"].read_text(encoding="utf-8"))
    assert result == receipt
    with sqlite3.connect(params["queue"]) as db:
        assert db.execute(
            "SELECT output_sha256 FROM render_jobs WHERE job_id=?",
            (original_receipt["job_id"],),
        ).fetchone()[0] == baseline_hash
    with pytest.raises(ValueError, match="RECEIPT_EXISTS"):
        finish.run_matched_finishing_trial(
            prepared, execute=True, approve_local_preview=True,
        )


def test_existing_pr240_trial_can_be_reused_without_rerender(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media, audio=False, caption=False, burnin=False)
    prepared = finish.prepare_matched_finishing_trial(**params)
    _monkeypatch_video(monkeypatch)
    basic.run_cut_extension_mp4_trial(
        prepared["trial"], execute=True, approve_local_preview=True,
    )
    monkeypatch.setattr(
        finish, "run_cut_extension_mp4_trial",
        lambda *args, **kwargs: pytest.fail("must reuse verified PR240 MP4"),
    )
    result = finish.run_matched_finishing_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert result["stage_comparable"] is True
    assert result["baseline_final_filename"] == media.name
    assert result["trial_final_filename"] == prepared["trial"]["output"].name


def test_existing_trial_receipt_with_changed_provenance_blocks_reuse(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media, audio=False, caption=False, burnin=False)
    prepared = finish.prepare_matched_finishing_trial(**params)
    _monkeypatch_video(monkeypatch)
    basic.run_cut_extension_mp4_trial(
        prepared["trial"], execute=True, approve_local_preview=True,
    )
    path = prepared["trial"]["receipt"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["local_source_asr_sha256"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="TRIAL_RECEIPT_INCONSISTENT"):
        finish.run_matched_finishing_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not prepared["matched_receipt"].exists()


def test_failed_trial_caption_marks_incomplete_not_comparable(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media, audio=False, caption=True, burnin=False)
    prepared = finish.prepare_matched_finishing_trial(**params)
    _monkeypatch_video(monkeypatch)
    monkeypatch.setattr(
        finish, "export_cut_aligned_srt",
        lambda *a, **kw: {"status": "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"},
    )
    result = finish.run_matched_finishing_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_MATCHED_AB_FINISH_INCOMPLETE_NOT_COMPARABLE"
    assert result["trial_final_sha256"] is None
    assert result["stage_comparable"] is False
    assert result["ready"] is False


def test_cli_default_preview_never_invokes_external_models_or_writes(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media)
    class Deny:
        def __init__(self, *a, **kw):
            pytest.fail("must never start LLM")
    monkeypatch.setattr(cli, "MeshLLMClient", Deny)
    args = ["render-cut-extension-finished-ab", *basic_args(params)[1:]]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "LOCAL_MATCHED_AB_NOT_EXECUTED"
    assert not finish.prepare_matched_finishing_trial(**params)[
        "matched_receipt"].exists()


def test_cli_execute_without_preview_permission_refuses(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    _stages(params, media)
    monkeypatch.setattr(
        finish, "run_cut_extension_mp4_trial",
        lambda *a, **kw: pytest.fail("must not render without permission"),
    )
    result = CliRunner().invoke(
        cli.app,
        ["render-cut-extension-finished-ab", *basic_args(params)[1:], "--execute"],
    )
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "LOCAL_MATCHED_AB_BLOCKED"
