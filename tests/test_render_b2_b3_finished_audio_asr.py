"""B2/B3 finished-media ASR: opt-in, text-free, source-pinned review only."""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_finished_audio_asr as cross
from holoclip import render_four_way_local_diagnostics as four
from test_render_four_way_local_diagnostics import _ready, _probes
from test_render_cut_extension_three_way_clip_asr import _FakeAsr
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    options, source, transcript, edited = _ready(tmp_path, monkeypatch)
    _probes(monkeypatch)
    checked = four.run_four_way_diagnostics(
        **options, execute=True, approve_local_preview=True,
    )
    assert checked["status"] == "LOCAL_FOUR_WAY_TECHNICAL_ACOUSTIC_SIGNALS_ONLY"
    return options, source, transcript, edited


def _fake_extract(monkeypatch):
    calls = []
    monkeypatch.setattr(cross.shutil, "which", lambda name: "fake-ffmpeg")

    def extract(ffmpeg, media, target, *, start, end):
        assert ffmpeg == "fake-ffmpeg"
        assert media.is_file()
        assert target.suffix == ".mp3"
        assert .3 <= end - start <= 8
        target.write_bytes(b"offline audio stub" * 70)
        calls.append((media, start, end))
    monkeypatch.setattr(cross, "_read_clip", extract)
    return calls


def test_default_preview_validates_four_way_but_never_decodes(tmp_path, monkeypatch):
    options, source, transcript, edited = _case(tmp_path, monkeypatch)
    before = source.read_bytes(), transcript.read_bytes(), edited.read_bytes()
    monkeypatch.setattr(cross, "_read_clip",
                        lambda *a, **k: pytest.fail("read-only must not decode"))
    basis = cross.prepare_b2_b3_audio_asr(**options)
    preview = cross._public(basis)
    assert preview["status"] == "LOCAL_B2_B3_FINISHED_AUDIO_ASR_READ_ONLY_PREVIEW"
    assert set(preview["finished_mp4_sha256"]) == {"b2", "b3"}
    assert preview["edited_audio_transcribed"] is False
    assert preview["actual_finished_audio_sent_to_provider"] is False
    assert preview["public_data_authorization_required"] is True
    assert preview["ready"] is False and preview["best_variant"] is None
    assert preview["four_way_diagnostics_sha256"]
    assert not basis["receipt"].exists()
    assert str(tmp_path) not in json.dumps(preview)
    assert "まだ発話は続いている" not in json.dumps(preview, ensure_ascii=False)
    assert (source.read_bytes(), transcript.read_bytes(), edited.read_bytes()) == before


def test_explicit_public_data_gate_blocks_before_provider(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    basis = cross.prepare_b2_b3_audio_asr(**options)
    _fake_extract(monkeypatch)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    with pytest.raises(ValueError, match="PUBLIC_DATA_REQUIRED"):
        asyncio.run(cross.observe_b2_b3_audio_asr(basis, _FakeAsr()))
    assert not basis["receipt"].exists()


def test_finished_b2_b3_asr_produces_only_review_hints(tmp_path, monkeypatch):
    options, source, transcript, edited = _case(tmp_path, monkeypatch)
    before = source.read_bytes(), transcript.read_bytes(), edited.read_bytes()
    basis = cross.prepare_b2_b3_audio_asr(**options)
    calls = _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    transcriber = _FakeAsr(texts={
        "b2": "まだ発話は続いている",
        "b3": "まだ発話は続いている続きもある",
    })
    result = asyncio.run(cross.observe_b2_b3_audio_asr(basis, transcriber))
    assert result["status"] == "LOCAL_B2_B3_ASR_DISCREPANCIES_NOT_VERIFIED_SPEECH"
    assert transcriber.calls == ["b2", "b3"]
    assert len(calls) == 2
    assert result["actual_finished_audio_sent_to_provider"] is True
    assert result["edited_audio_transcribed"] is True
    assert result["comparison"]["no_automatic_winner_selected"] is True
    assert result["comparison"]["b3_newly_included_is_not_proven_spoken"] is True
    assert result["human_audio_review_completed"] is False
    assert result["missing_or_repeated_words_proven"] is False
    assert result["editorial_quality_improvement"] is None
    assert result["best_variant"] is None and result["ready"] is False
    assert all(
        role["temporal_asr_review"]["human_audio_review_completed"] is False
        for role in result["variants"].values()
    )
    assert json.loads(basis["receipt"].read_text("utf-8")) == result
    text = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in text
    assert "まだ発話は続いている" not in text
    assert (source.read_bytes(), transcript.read_bytes(), edited.read_bytes()) == before
    with pytest.raises(ValueError):
        asyncio.run(cross.observe_b2_b3_audio_asr(basis, _FakeAsr()))
    assert transcriber.calls == ["b2", "b3"]


def test_b2_b3_shared_source_id_warning_delta_remains_unverified():
    def row(key, status):
        return {"source_evidence_id": key, "status": status}
    result = cross._comparison({
        "b2": {"temporal_asr_review": {"source_utterance_alignment": [
            row("a", "PARTIAL_ASR_TEXT_OVERLAP_REVIEW"),
            row("b", "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"),
            row("c", "SHORT_TEXT_AMBIGUOUS_REVIEW"),
        ]}},
        "b3": {"temporal_asr_review": {"source_utterance_alignment": [
            row("a", "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"),
            row("b", "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK"),
            row("new", "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"),
        ]}},
    }, {})
    assert result["shared_source_asr_interval_count"] == 2
    assert result["b3_newly_fully_included_source_asr_count"] == 1
    assert result["b2_only_fully_included_source_asr_count"] == 1
    assert result["b3_fewer_temporal_warnings_on_shared_intervals"] == 1
    assert result["b3_more_temporal_warnings_on_shared_intervals"] == 1
    assert result["no_automatic_winner_selected"] is True


def test_quota_failure_stops_second_asr_without_receipt(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    basis = cross.prepare_b2_b3_audio_asr(**options)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    provider = _FakeAsr(fail_at=2)
    with pytest.raises(ValueError, match="DEFERRED"):
        asyncio.run(cross.observe_b2_b3_audio_asr(basis, provider))
    assert provider.calls == ["b2", "b3"]
    assert not basis["receipt"].exists()


def test_changed_source_transcript_during_asr_blocks_receipt(tmp_path, monkeypatch):
    options, _, transcript, _ = _case(tmp_path, monkeypatch)
    basis = cross.prepare_b2_b3_audio_asr(**options)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    def mutate(role):
        if role == "b2":
            transcript.write_bytes(b'{"segments":[]}')
    with pytest.raises(ValueError):
        asyncio.run(cross.observe_b2_b3_audio_asr(basis, _FakeAsr(hook=mutate)))
    assert not basis["receipt"].exists()


@pytest.mark.parametrize("change", [
    "four_way_receipt", "mp4_sha", "source_clock", "candidate_id",
])
def test_stale_four_way_provenance_never_contacts_asr(tmp_path, monkeypatch, change):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    basis = cross.prepare_b2_b3_audio_asr(**options)
    report = basis["receipt"].with_name(
        basis["context"]["found"]["target"].stem + ".four-way-diagnostics.json"
    )
    original = json.loads(report.read_text("utf-8"))
    if change == "four_way_receipt":
        original["status"] = "unverified"
    elif change == "mp4_sha":
        original["variants"]["b3"]["final_sha256"] = "0" * 64
    elif change == "source_clock":
        original["source_asr_sha256"] = "0" * 64
    else:
        original["b3_id"] = "unrelated"
    report.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(cross, "_read_clip",
                        lambda *a, **k: pytest.fail("untrusted QA must not decode"))
    with pytest.raises(ValueError):
        cross.prepare_b2_b3_audio_asr(**options)
    assert not basis["receipt"].exists()


def test_cli_preview_and_explicit_double_gate(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    monkeypatch.setattr(cli, "MeshTranscriber",
                        lambda: pytest.fail("no provider in preview/denial"))
    args = [
        "render-b2-b3-finished-audio-asr", *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["edited_audio_transcribed"] is False
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "private")
    denied = CliRunner().invoke(cli.app, [
        *args, "--execute", "--approve-audio-asr",
    ])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
