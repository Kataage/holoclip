"""Offline regression tests for source-grounded finished A/B speech audit."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_finish_ab as finish
from holoclip import render_cut_extension_speech_audit as audit
from test_render_cut_extension_finish_ab import _monkeypatch_video, _stages
from test_render_cut_extension_mp4_ab import _args, _inputs


def _ready(tmp_path, monkeypatch):
    params, _, _, media, transcript = _inputs(tmp_path)
    _stages(params, media)
    prepared = finish.prepare_matched_finishing_trial(**params)
    _monkeypatch_video(monkeypatch)
    saved = finish.run_matched_finishing_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert saved["stage_comparable"] is True
    monkeypatch.setattr(
        audit, "_probe", lambda path: (
            7.0 if "-extension-" in path.name else 6.0, True, True,
        ),
    )
    return params, prepared, media, transcript


def _speech_path(prepared):
    return prepared["matched_receipt"].with_name(
        f"{prepared['trial']['output'].stem}.speech-audit.json"
    )


def test_default_real_asr_evidence_never_decodes_or_changes_media(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    original_mp4 = media.read_bytes()
    original_asr = transcript.read_bytes()
    monkeypatch.setattr(
        audit, "_measure",
        lambda *args: pytest.fail("dry-run must not decode audio"),
    )
    report = audit.audit_finished_extension(prepared)
    assert report["status"] == "LOCAL_SPEECH_AB_AUDIT_PREVIEW_ONLY"
    assert report["asr"]["status"] == (
        "SOURCE_ASR_TIMING_OBSERVED_UNVERIFIED_AUDIO"
    )
    assert report["asr"]["added_interval_full_utterance_count"] >= 1
    assert report["asr"]["added_interval_partial_utterance_count"] == 0
    assert report["audio"]["status"] == "NOT_MEASURED"
    assert report["editorial_quality_improvement"] is None
    assert report["source_asr_linguistically_verified"] is False
    assert report["ready"] is False
    assert not _speech_path(prepared).exists()
    assert media.read_bytes() == original_mp4
    assert transcript.read_bytes() == original_asr
    raw = json.dumps(report, ensure_ascii=False)
    assert "切れていた続きの反応" not in raw
    assert str(tmp_path) not in raw


def test_execute_measures_local_audio_and_saves_immutable_sha_bound_report(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _ready(tmp_path, monkeypatch)
    original_mp4 = media.read_bytes()
    original_asr = transcript.read_bytes()
    calls = []
    monkeypatch.setattr(audit.shutil, "which", lambda _: "ffmpeg-test")
    def measure(exe, file, start, end):
        calls.append((exe, file, start, end))
        return (-22.5, -4.0) if len(calls) == 1 else (-21.0, -3.0)
    monkeypatch.setattr(audit, "_measure", measure)
    report = audit.audit_finished_extension(prepared, execute=True)
    assert report["status"] == "LOCAL_SPEECH_AB_EVIDENCE_RECORDED_UNQUALIFIED"
    assert report["audio"]["status"] == (
        "LOCAL_AUDIO_SIGNALS_MEASURED_NOT_PREFERENCE"
    )
    assert report["audio"]["baseline_adjacent"]["mean_dbfs"] == -22.5
    assert report["audio"]["trial_added"]["mean_dbfs"] == -21.0
    assert len(calls) == 2
    assert all(exe == "ffmpeg-test" and 0 < end - start <= 1.0
               for exe, _, start, end in calls)
    assert json.loads(_speech_path(prepared).read_text(
        encoding="utf-8",
    )) == report
    with pytest.raises(ValueError, match="RECEIPT_EXISTS"):
        audit.audit_finished_extension(prepared, execute=True)
    assert len(calls) == 2
    assert media.read_bytes() == original_mp4
    assert transcript.read_bytes() == original_asr
    assert report["auto_adoption_allowed"] is False
    assert report["publication_performed"] is False


def test_changed_finished_trial_blocks_without_audio_read(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    out = finish._stage_paths(prepared["trial"]["output"])["burnin"]
    out.write_bytes(b"replaced captioned B")
    monkeypatch.setattr(
        audit, "_measure",
        lambda *args: pytest.fail("never decode changed media"),
    )
    with pytest.raises(ValueError, match="TRIAL_CHANGED"):
        audit.audit_finished_extension(prepared, execute=True)
    assert not _speech_path(prepared).exists()


def test_changed_matched_receipt_lineage_blocks(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    path = prepared["matched_receipt"]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["trial_id"] = "wrong"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="RECEIPT_LINEAGE_CHANGED"):
        audit.audit_finished_extension(prepared)
    assert not _speech_path(prepared).exists()


def test_changed_local_asr_digest_blocks_claimed_boundary_evidence(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    load = audit._read_transcript

    def tampered(*args, **kwargs):
        value = load(*args, **kwargs)
        assert value is not None
        return value[0], "f" * 64, value[2]

    monkeypatch.setattr(audit, "_read_transcript", tampered)
    with pytest.raises(ValueError, match="ASR_LINEAGE_CHANGED"):
        audit.audit_finished_extension(prepared)
    assert not _speech_path(prepared).exists()


def test_audio_measurement_failure_stays_unqualified_with_no_partial_values(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(audit.shutil, "which", lambda _: "ffmpeg-test")
    counter = []

    def fail_second(*args):
        counter.append(1)
        if len(counter) == 2:
            raise ValueError("local decoder unavailable")
        return -22.0, -3.0

    monkeypatch.setattr(audit, "_measure", fail_second)
    report = audit.audit_finished_extension(prepared, execute=True)
    assert report["audio"] == {
        "status": "AUDIO_MEASUREMENT_UNAVAILABLE",
        "baseline_adjacent": None,
        "trial_added": None,
    }
    assert report["ready"] is False


def test_media_changed_during_audio_measurement_blocks_receipt(
    tmp_path, monkeypatch,
):
    _, prepared, media, _ = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(audit.shutil, "which", lambda _: "ffmpeg-test")

    def mutate(*args):
        media.write_bytes(b"changed during decode")
        return -22.0, -4.0

    monkeypatch.setattr(audit, "_measure", mutate)
    with pytest.raises(ValueError, match="MEDIA_CHANGED_DURING_AUDIT"):
        audit.audit_finished_extension(prepared, execute=True)
    assert not _speech_path(prepared).exists()


def test_cli_default_no_model_and_no_second_receipt(tmp_path, monkeypatch):
    params, prepared, _, _ = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *args, **kw: pytest.fail("no external model"),
    )
    args = ["render-cut-extension-speech-audit", *_args(params)[1:]]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == (
        "LOCAL_SPEECH_AB_AUDIT_PREVIEW_ONLY"
    )
    assert not _speech_path(prepared).exists()

