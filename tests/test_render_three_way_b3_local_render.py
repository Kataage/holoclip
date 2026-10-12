"""Guarded local B3 MP4 trial: fresh lineage, opt-in and immutable output."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_b3_local_render as render
from holoclip.render_cut_extension_mp4_ab import _trial_timeline
from test_render_three_way_existing_media_review_batch import (
    _completed, _with_saved_clip_asr,
)
from test_render_cut_extension_mp4_ab import _args


def _eligible(tmp_path, monkeypatch):
    options, completed, media, asr = _completed(tmp_path, monkeypatch)
    observed_audio = _with_saved_clip_asr(options, monkeypatch)

    def safe_hint(*, prepared, plan, coverage, edited_asr):
        b2 = prepared["diagnostics"]["finished"]["b2"]
        trial = b2["prepared"]["trial"]
        a = trial["original"]["baseline_timeline"]
        current = sorted(b2["timeline"].segments, key=lambda s: s.output_order)
        idx = trial["source_cut_index"]
        side = trial["trial_action"]
        original = sorted(a.segments, key=lambda s: s.output_order)
        before = [{"source_start_seconds": s.source_start,
                   "source_end_seconds": s.source_end} for s in original]
        after = [{"source_start_seconds": s.source_start,
                  "source_end_seconds": s.source_end} for s in current]
        if side == "EXTEND_START":
            after[idx]["source_start_seconds"] -= .3
        else:
            after[idx]["source_end_seconds"] += .3
        built = _trial_timeline(a, {
            "baseline_source_intervals": before,
            "trial_source_intervals": after,
            "source_cut_index": idx, "trial_action": side,
        })
        candidate = {
            "source_cut_index": idx, "trial_action": side,
            "proposed_source_start_seconds": after[idx]["source_start_seconds"],
            "proposed_source_end_seconds": after[idx]["source_end_seconds"],
            "proposed_timeline_sha256": hashlib.sha256(
                built.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "proposed_playback_seconds": round(built.estimated_duration, 3),
            "extra_seconds_beyond_b2": .3,
            "requires_new_render_and_independent_audio_review": True,
        }
        return {"status": "LOCAL_B3_UNRENDERED_SOURCE_CLOCK_REPAIR_HYPOTHESES",
                "proposed_count": 1, "proposals": [candidate]}

    monkeypatch.setattr(render, "propose_guarded_b3_boundaries", safe_hint)
    return options, completed, media, asr, observed_audio


def test_read_only_default_never_renders_or_creates_receipts(tmp_path, monkeypatch):
    options, _, media, asr, audio = _eligible(tmp_path, monkeypatch)
    before = media.read_bytes(), asr.read_bytes(), audio.read_bytes()
    monkeypatch.setattr(
        render, "render_local_preview",
        lambda *a: pytest.fail("read-only preview must not render"),
    )
    result = render.run_existing_b3_preview(**options)
    assert result["status"] == "LOCAL_B3_PREVIEW_NOT_RENDERED"
    assert result["source_clock_edit_hint_available"] is True
    assert result["new_b3_local_mp4_rendered"] is False
    assert result["ready"] is False
    assert result["automatic_adoption_allowed"] is False
    assert result["human_auditory_qa_completed"] is False
    assert not list(options["preview_root"].rglob("*-speechb3-*.mp4"))
    assert not list(options["preview_root"].rglob("*-speechb3-*.json"))
    assert (media.read_bytes(), asr.read_bytes(), audio.read_bytes()) == before
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "まだ発話は続いている" not in serialized


def test_execute_requires_separate_explicit_approval(tmp_path, monkeypatch):
    options, _, _, _, _ = _eligible(tmp_path, monkeypatch)
    monkeypatch.setattr(render, "render_local_preview",
                        lambda *a: pytest.fail("no render without approval"))
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        render.run_existing_b3_preview(**options, execute=True)
    assert not list(options["preview_root"].rglob("*-speechb3-*.mp4"))


def _fake_render(monkeypatch, duration):
    runs = []
    monkeypatch.setattr(render, "resolve_cached_render_sources",
                        lambda *a, **k: [])
    monkeypatch.setattr(render, "_offline_render_policy", lambda *a: None)
    monkeypatch.setattr(
        render, "compile_local_render_plan",
        lambda timeline, sources, target: SimpleNamespace(
            output=target, expected_duration=timeline.estimated_duration,
            id="synthetic-local-b3-render-plan",
        ),
    )
    def fake_render(plan):
        runs.append(plan)
        payload = b"new isolated B3 trial without audio-finish claim" * 120
        plan.output.write_bytes(payload)
        return {
            "status": render._EXPECTED_QA,
            "output_sha256": hashlib.sha256(payload).hexdigest(),
        }
    monkeypatch.setattr(render, "render_local_preview", fake_render)
    monkeypatch.setattr(
        render, "_probe",
        lambda path: (
            duration if "-speechb3-" in path.name else 7.0,
            True, True,
        ),
    )
    return runs


def test_explicit_b3_renders_once_with_technical_qa_and_immutable_receipt(
    tmp_path, monkeypatch,
):
    options, _, media, asr, audio = _eligible(tmp_path, monkeypatch)
    before = media.read_bytes(), asr.read_bytes(), audio.read_bytes()
    preview = render.run_existing_b3_preview(**options)
    runs = _fake_render(monkeypatch, preview["b3_expected_seconds"])
    result = render.run_existing_b3_preview(
        **options, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_B3_RENDERED_BASIC_AV_QA_NOT_EDITED_QUALITY"
    assert result["b3_technical_qa"] == render._EXPECTED_QA
    assert len(runs) == 1
    assert result["b3_measured_seconds"] == preview["b3_expected_seconds"]
    assert result["b3_vs_b2_duration_difference_seconds"] > 0
    assert result["a_b_b2_b3_duration_compared"] is True
    assert result["new_b3_local_mp4_rendered"] is True
    assert result["b3_finished_audio_or_captions_matched"] is False
    assert result["human_auditory_qa_completed"] is False
    assert result["editorial_quality_improvement"] is None
    assert result["publication_performed"] is False and result["ready"] is False
    rendered = list(options["preview_root"].rglob("*-speechb3-*.mp4"))
    reports = list(options["preview_root"].rglob("*-speechb3-*.json"))
    assert len(rendered) == len(reports) == 1
    assert hashlib.sha256(rendered[0].read_bytes()).hexdigest() == (
        result["b3_mp4_sha256"]
    )
    assert json.loads(reports[0].read_text(encoding="utf-8")) == result
    assert (media.read_bytes(), asr.read_bytes(), audio.read_bytes()) == before
    with pytest.raises(ValueError, match="UNSAFE|EXISTS"):
        render.run_existing_b3_preview(
            **options, execute=True, approve_local_preview=True,
        )
    assert len(runs) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("occupied", ["mp4", "receipt"])
def test_existing_user_owned_b3_file_is_not_overwritten(tmp_path, monkeypatch, occupied):
    options, _, _, _, _ = _eligible(tmp_path, monkeypatch)
    preview = render.run_existing_b3_preview(**options)
    job = options["job_id"]
    target = options["preview_root"] / preview["b3_mp4_filename"]
    sidecar = options["preview_root"] / preview["b3_receipt_filename"]
    assert job in target.name
    existing = target if occupied == "mp4" else sidecar
    existing.write_bytes(b"user-owned file")
    runs = _fake_render(monkeypatch, preview["b3_expected_seconds"])
    with pytest.raises(ValueError, match="UNSAFE|EXISTS"):
        render.run_existing_b3_preview(
            **options, execute=True, approve_local_preview=True,
        )
    assert existing.read_bytes() == b"user-owned file"
    assert runs == []


def test_changed_source_asr_or_edited_asr_blocks_b3_before_render(
    tmp_path, monkeypatch,
):
    options, _, media, asr, audio = _eligible(tmp_path, monkeypatch)
    old = audio.read_bytes()
    audio.write_bytes(b'{"status":"UNTRUSTED"}')
    runs = _fake_render(monkeypatch, 8.0)
    with pytest.raises(ValueError):
        render.run_existing_b3_preview(
            **options, execute=True, approve_local_preview=True,
        )
    assert runs == []
    assert not list(options["preview_root"].rglob("*-speechb3-*.mp4"))
    audio.write_bytes(old)
    asr.write_bytes(b'{"segments":[]}')
    with pytest.raises(ValueError):
        render.run_existing_b3_preview(
            **options, execute=True, approve_local_preview=True,
        )
    assert runs == []


def test_no_candidate_never_executes_an_arbitrary_b3(tmp_path, monkeypatch):
    options, _, _, _, _ = _eligible(tmp_path, monkeypatch)
    monkeypatch.setattr(
        render, "propose_guarded_b3_boundaries",
        lambda **kw: {"status": "LOCAL_B3_NO_SUPPORTED_BOUNDARY_REPAIR",
                      "proposed_count": 0, "proposals": []},
    )
    result = render.run_existing_b3_preview(**options)
    assert result["status"] == "LOCAL_B3_NO_ELIGIBLE_HINT"
    assert result["new_b3_local_mp4_rendered"] is False
    with pytest.raises(ValueError, match="NO_PROVEN_CANDIDATE"):
        render.run_existing_b3_preview(
            **options, execute=True, approve_local_preview=True,
        )
    assert not list(options["preview_root"].rglob("*-speechb3-*.mp4"))


def test_cli_preview_and_explicit_permission(tmp_path, monkeypatch):
    options, _, _, _, _ = _eligible(tmp_path, monkeypatch)
    args = ["render-three-way-b3-local-preview", *_args(options)[1:],
            "--job-id", options["job_id"],
            "--proposal-index", str(options["proposal_index"])]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "LOCAL_B3_PREVIEW_NOT_RENDERED"
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
