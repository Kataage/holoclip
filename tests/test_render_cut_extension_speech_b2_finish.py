"""B2 matched audio, caption and burn-in: offline no-overwrite regressions."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_finish_ab as ab_finish
from holoclip import render_cut_extension_speech_b2 as b2
from holoclip import render_cut_extension_speech_b2_finish as finish
from test_render_cut_extension_mp4_ab import _args
from test_render_cut_extension_speech_b2 import _fake_renderer, _setup


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(tmp_path, monkeypatch):
    params, prepared, media, transcript = _setup(tmp_path, monkeypatch)
    _fake_renderer(monkeypatch)
    done = b2.run_speech_b2_preview(
        prepared, execute=True, approve_local_preview=True,
    )
    assert done["status"] == "LOCAL_B2_RENDERED_THREE_WAY_TECHNICAL_ONLY"
    prepared2 = finish.prepare_speech_b2_finishing(
        **params,
        proposal_index=prepared["prepared"]["trial"]["proposal_index"],
    )
    monkeypatch.setattr(finish, "_probe", lambda path: (
        7.5 if "-speechb2-" in path.name
        else 7.0 if "-extension-" in path.name
        else 6.0,
        True, True,
    ))
    return params, prepared2, media, transcript


def _stages(monkeypatch, *, fail=None):
    calls = {"caption": 0, "audio": 0, "burnin": 0}

    def fake_caption(timeline, sources, *, transcript_root, mp4_output):
        calls["caption"] += 1
        if fail == "caption":
            return {"status": "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"}
        output = mp4_output.with_suffix(".srt")
        output.write_bytes(b"1\n00:00:00,300 --> 00:00:00,900\nsubtitles\n")
        return {
            "status": ab_finish._SUCCESS["caption"],
            "output_sha256": _sha(output), "cue_count": 1,
            "dropped_cue_count": 0,
        }

    def fake_audio(*, mp4_output, video_sha256, timeline):
        calls["audio"] += 1
        if fail == "audio":
            return {"status": "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"}
        output = mp4_output.with_name(f"{mp4_output.stem}.leveled.mp4")
        output.write_bytes(b"mock leveled B2")
        return {
            "status": ab_finish._SUCCESS["audio"],
            "output_sha256": _sha(output), "max_abs_gain_db": 1.0,
        }

    def fake_burn(*, mp4_output, video_sha256, subtitle_sha256):
        calls["burnin"] += 1
        if fail == "burnin":
            return {"status": "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"}
        stem = mp4_output.stem.removesuffix(".leveled")
        output = mp4_output.with_name(f"{stem}.captioned.mp4")
        output.write_bytes(b"mock captioned B2")
        return {
            "status": ab_finish._SUCCESS["burnin"],
            "output_sha256": _sha(output),
        }

    monkeypatch.setattr(finish, "export_cut_aligned_srt", fake_caption)
    monkeypatch.setattr(finish, "level_preview_audio", fake_audio)
    monkeypatch.setattr(finish, "burn_captioned_preview", fake_burn)
    monkeypatch.setattr(finish, "_offline_render_policy", lambda *args: None)
    return calls


def test_dry_run_reuses_B2_without_writes_or_ffmpeg(tmp_path, monkeypatch):
    _, plan, original, transcript = _case(tmp_path, monkeypatch)
    b2_mp4 = plan["b2"]["output"]
    before = (original.read_bytes(), b2_mp4.read_bytes(), transcript.read_bytes())
    monkeypatch.setattr(
        finish, "level_preview_audio",
        lambda **kwargs: pytest.fail("dry-run must not process audio"),
    )
    report = finish.finish_speech_b2(plan)
    assert report["status"] == "LOCAL_B2_FINISH_PREVIEW_ONLY"
    assert report["baseline_audio_leveling_expected"] is True
    assert report["baseline_caption_expected"] is True
    assert report["baseline_burnin_expected"] is True
    assert report["stage_comparable_to_a_and_b"] is False
    assert report["editorial_quality_improvement"] is None
    assert report["ready"] is False
    assert not plan["finished_receipt"].exists()
    assert (original.read_bytes(), b2_mp4.read_bytes(), transcript.read_bytes()) == before
    assert str(tmp_path) not in json.dumps(report)


def test_execution_matches_all_stages_and_pins_final_sha(tmp_path, monkeypatch):
    _, plan, original, transcript = _case(tmp_path, monkeypatch)
    calls = _stages(monkeypatch)
    current_a = original.read_bytes()
    current_b = plan["b2"]["prepared"]["trial"]["output"].read_bytes()
    current_b2 = plan["b2"]["output"].read_bytes()
    current_asr = transcript.read_bytes()
    finished = finish.finish_speech_b2(
        plan, execute=True, approve_local_preview=True,
    )
    assert finished["status"] == "LOCAL_B2_FINISH_STAGES_TECHNICALLY_COMPARABLE"
    assert calls == {"caption": 1, "audio": 1, "burnin": 1}
    assert finished["stage_comparable_to_a_and_b"] is True
    assert finished["b2_caption_audio_finishing_matched_to_a_b"] is True
    assert finished["b2_audio_status"] == ab_finish._SUCCESS["audio"]
    assert finished["b2_caption_status"] == ab_finish._SUCCESS["caption"]
    assert finished["b2_burnin_status"] == ab_finish._SUCCESS["burnin"]
    assert finished["b2_measured_final_seconds"] == 7.5
    assert finished["b2_final_filename"].endswith(".captioned.mp4")
    target = ab_finish._stage_paths(plan["b2"]["output"])["burnin"]
    assert finished["b2_final_sha256"] == _sha(target)
    assert json.loads(plan["finished_receipt"].read_text(encoding="utf-8")) == finished
    assert finished["editorial_quality_improvement"] is None
    assert finished["auto_adoption_allowed"] is False
    assert finished["ready"] is False
    assert finished["publication_performed"] is False
    assert original.read_bytes() == current_a
    assert plan["b2"]["prepared"]["trial"]["output"].read_bytes() == current_b
    assert plan["b2"]["output"].read_bytes() == current_b2
    assert transcript.read_bytes() == current_asr
    with pytest.raises(ValueError, match="RECEIPT_EXISTS"):
        finish.finish_speech_b2(
            plan, execute=True, approve_local_preview=True,
        )


def test_explicit_permission_is_required_before_stage_calls(tmp_path, monkeypatch):
    _, plan, _, _ = _case(tmp_path, monkeypatch)
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
        finish.finish_speech_b2(plan, execute=True)
    assert calls == {"caption": 0, "audio": 0, "burnin": 0}


@pytest.mark.parametrize("stage", ["audio", "caption", "burnin"])
def test_existing_sibling_refuses_before_any_write(tmp_path, monkeypatch, stage):
    _, plan, _, _ = _case(tmp_path, monkeypatch)
    target = ab_finish._stage_paths(plan["b2"]["output"])[stage]
    target.write_bytes(b"operator's existing output")
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match="STAGE_EXISTS"):
        finish.finish_speech_b2(plan, execute=True, approve_local_preview=True)
    assert target.read_bytes() == b"operator's existing output"
    assert calls == {"caption": 0, "audio": 0, "burnin": 0}
    assert not plan["finished_receipt"].exists()


@pytest.mark.parametrize("stage", ["audio", "caption", "burnin"])
def test_failed_optional_stage_never_claims_parity(tmp_path, monkeypatch, stage):
    _, plan, _, _ = _case(tmp_path, monkeypatch)
    _stages(monkeypatch, fail=stage)
    report = finish.finish_speech_b2(
        plan, execute=True, approve_local_preview=True,
    )
    assert report["status"] == "LOCAL_B2_FINISH_INCOMPLETE_NOT_COMPARABLE"
    assert report["stage_comparable_to_a_and_b"] is False
    assert report["b2_caption_audio_finishing_matched_to_a_b"] is False
    assert report["b2_final_sha256"] is None
    assert report["ready"] is False
    assert json.loads(plan["finished_receipt"].read_text(encoding="utf-8")) == report


@pytest.mark.parametrize("which", ["b2", "base_a", "final_b", "matched_receipt"])
def test_tampered_preexisting_evidence_refuses_before_encode(
    tmp_path, monkeypatch, which,
):
    _, plan, original, _ = _case(tmp_path, monkeypatch)
    if which == "b2":
        file = plan["b2"]["output"]
    elif which == "base_a":
        file = original
    elif which == "final_b":
        file = ab_finish._stage_paths(
            plan["b2"]["prepared"]["trial"]["output"]
        )["burnin"]
    else:
        file = plan["b2"]["prepared"]["matched_receipt"]
    file.write_bytes(b"tampered")
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        finish.finish_speech_b2(
            plan, execute=True, approve_local_preview=True,
        )
    assert calls == {"caption": 0, "audio": 0, "burnin": 0}
    assert not plan["finished_receipt"].exists()


def test_modified_A_during_B2_finishing_blocks_receipt(tmp_path, monkeypatch):
    _, plan, original, _ = _case(tmp_path, monkeypatch)
    _stages(monkeypatch)
    call = finish.level_preview_audio
    def corrupt(*args, **kwargs):
        result = call(*args, **kwargs)
        original.write_bytes(b"modified during B2 leveling")
        return result
    monkeypatch.setattr(finish, "level_preview_audio", corrupt)
    with pytest.raises(ValueError, match="ORIGINAL_CHANGED"):
        finish.finish_speech_b2(
            plan, execute=True, approve_local_preview=True,
        )
    assert not plan["finished_receipt"].exists()


def test_source_ASR_modified_during_B2_finishing_blocks_receipt(
    tmp_path, monkeypatch,
):
    _, plan, _, transcript = _case(tmp_path, monkeypatch)
    _stages(monkeypatch)
    call = finish.level_preview_audio
    def corrupt(*args, **kwargs):
        result = call(*args, **kwargs)
        transcript.write_bytes(b"changed ASR during optional stage")
        return result
    monkeypatch.setattr(finish, "level_preview_audio", corrupt)
    with pytest.raises(ValueError, match="ASR_LINEAGE_CHANGED"):
        finish.finish_speech_b2(
            plan, execute=True, approve_local_preview=True,
        )
    assert not plan["finished_receipt"].exists()


def test_missing_render_receipt_never_starts_finishing(tmp_path, monkeypatch):
    _, plan, _, _ = _case(tmp_path, monkeypatch)
    plan["b2"]["receipt"].unlink()
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match="RENDER_RECEIPT_MISSING"):
        finish.finish_speech_b2(
            plan, execute=True, approve_local_preview=True,
        )
    assert calls == {"caption": 0, "audio": 0, "burnin": 0}


def test_cli_read_only_and_permission_denied(tmp_path, monkeypatch):
    params, plan, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kwargs: pytest.fail("no external model"),
    )
    args = [
        "render-cut-extension-speech-b2-finish",
        *_args(params)[1:],
        "--proposal-index", str(plan["b2"]["prepared"]["trial"]["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == "LOCAL_B2_FINISH_PREVIEW_ONLY"
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["status"] == "LOCAL_B2_FINISH_BLOCKED"
    assert not plan["finished_receipt"].exists()
