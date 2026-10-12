"""Offline ASR re-observation of real edited A/B/B2 audio: never quality proof."""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.models import TranscriptSegment
from holoclip import render_cut_extension_three_way_clip_asr as cross
from test_render_cut_extension_three_way_triage import _prepared
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    params, review, media, transcript, _ = _prepared(tmp_path, monkeypatch)
    proposal_index = review["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    prepared = cross.prepare_three_way_clip_asr(
        **params, proposal_index=proposal_index,
    )
    return params, prepared, media, transcript, proposal_index


class _FakeAsr:
    def __init__(self, *, texts=None, fail_at=None, hook=None):
        self.texts = texts or {}
        self.fail_at = fail_at
        self.hook = hook
        self.calls = []
        self.last_provenance = {"provider": "offline_mock", "model": "synthetic"}

    async def transcribe_file(self, path):
        role = path.stem
        assert path.exists() and path.stat().st_size >= 512
        self.calls.append(role)
        if self.hook is not None:
            self.hook(role)
        if self.fail_at == len(self.calls):
            raise ValueError("LOCAL_ASR_MOCK_DEFERRED_QUOTA")
        phrase = self.texts.get(role, "まだ発話は続いている")
        if phrase is None:
            return []
        return [TranscriptSegment(start=0.1, end=0.8, text=phrase)]


def _fake_extract(monkeypatch):
    calls = []
    monkeypatch.setattr(cross.shutil, "which", lambda cmd: "mock-ffmpeg")

    def extract(ffmpeg, media, target, *, start, end):
        assert ffmpeg == "mock-ffmpeg"
        assert media.is_file()
        assert 0 <= start < end <= 180
        assert .3 <= end - start <= 8
        assert target.suffix == ".mp3"
        target.write_bytes(b"x" * 800)
        calls.append((media, start, end))

    monkeypatch.setattr(cross, "_read_clip", extract)
    return calls


def test_default_preview_has_no_audio_text_upload_or_file_creation(
    tmp_path, monkeypatch,
):
    _, prepared, media, source, _ = _case(tmp_path, monkeypatch)
    previous = (media.read_bytes(), source.read_bytes())
    monkeypatch.setattr(
        cross, "_read_clip",
        lambda *args, **kwargs: pytest.fail("decoder should not run"),
    )
    report = cross.preview_clip_asr(prepared)
    assert report["status"] == "LOCAL_CLIP_ASR_CROSSCHECK_PREVIEW_ONLY"
    assert report["clip_audio_transcribed"] is False
    assert report["transcript_text_exported"] is False
    assert report["source_asr_is_not_ground_truth"] is True
    assert report["missing_or_repeated_speech_confirmed"] is False
    assert report["best_variant"] is None and report["ready"] is False
    assert set(report["finished_mp4_sha256"]) == {"a", "b", "b2"}
    assert not prepared["receipt"].exists()
    assert (media.read_bytes(), source.read_bytes()) == previous
    assert "まだ発話は続いている" not in json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in json.dumps(report)


def test_local_finished_audio_asr_differences_are_review_hints_only(
    tmp_path, monkeypatch,
):
    _, prepared, media, source, _ = _case(tmp_path, monkeypatch)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    original = media.read_bytes(), source.read_bytes()
    client = _FakeAsr(texts={"a": "まだ発話は続いている",
                              "b": "まったく違う音声",
                              "b2": None})
    report = asyncio.run(cross.observe_three_way_clip_asr(prepared, client))
    assert client.calls == ["a", "b", "b2"]
    assert report["status"] == "LOCAL_CLIP_ASR_TEXTUAL_MISMATCH_HINTS_NOT_VERIFIED_SPEECH"
    assert report["clip_audio_transcribed"] is True
    assert report["actual_finished_clip_audio_sent_to_asr_provider"] is True
    assert report["transcript_text_exported"] is False
    assert report["human_editorial_qa_completed"] is False
    assert report["auto_adoption_allowed"] is False
    assert report["ready"] is False
    assert report["best_variant"] is None
    assert report["missing_or_repeated_speech_confirmed"] is False
    assert report["variants"]["b2"]["recognized_segment_count"] == 0
    assert report["variants"]["b2"]["no_recognized_text_is_not_proof_of_silence"]
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    assert str(tmp_path) not in json.dumps(report)
    assert "まだ発話は続いている" not in json.dumps(report, ensure_ascii=False)
    assert (media.read_bytes(), source.read_bytes()) == original
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE"):
        asyncio.run(cross.observe_three_way_clip_asr(prepared, _FakeAsr()))


def test_comparator_only_indicates_textual_discrepancies():
    examples = [
        {"id": "b2-e01", "text": cross._normalize("あの話")},
        {"id": "b2-e02", "text": cross._normalize("続きがあります")},
    ]
    report = cross._compare_observation(
        source=examples, output_asr=cross._normalize("あの話です"), output_segment_count=1,
    )
    assert report["comparison_count"] == 2
    assert report["comparison"][0]["exact_normalized_containment"] is True
    assert report["comparison"][0]["signal"] == "TEXTUAL_OVERLAP_OBSERVED_UNVERIFIED"
    assert report["comparison"][1]["signal"] == "LOW_TEXTUAL_OVERLAP_REVIEW_REQUIRED"
    assert report["low_overlap_review_count"] == 1
    assert all(row["spoken_words_verified"] is False for row in report["comparison"])
    assert all(row["missing_speech_proven"] is False for row in report["comparison"])


@pytest.mark.parametrize("corrupt", [
    "out_of_range", "nan", "malformed_text", "too_many", "unexpected_type",
])
def test_malformed_ASR_result_never_writes_report(tmp_path, monkeypatch, corrupt):
    _, prepared, _, _, _ = _case(tmp_path, monkeypatch)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    bad = _FakeAsr()
    async def broken(path):
        bad.calls.append(path.stem)
        if corrupt == "out_of_range":
            return [TranscriptSegment(start=0, end=100, text="text")]
        if corrupt == "nan":
            return [TranscriptSegment(start=0, end=float("nan"), text="text")]
        if corrupt == "malformed_text":
            return [TranscriptSegment(start=0, end=0.8, text="do not\nuse")]
        if corrupt == "too_many":
            return [TranscriptSegment(start=0, end=0.8, text="text") for _ in range(26)]
        return [{"start": 0.1, "end": 1, "text": "not validated"}]
    bad.transcribe_file = broken
    with pytest.raises((ValueError, TypeError)):
        asyncio.run(cross.observe_three_way_clip_asr(prepared, bad))
    assert not prepared["receipt"].exists()


def test_quota_defer_stops_without_uploading_more_or_saving(
    tmp_path, monkeypatch,
):
    _, prepared, _, _, _ = _case(tmp_path, monkeypatch)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    bad = _FakeAsr(fail_at=2)
    with pytest.raises(ValueError, match="DEFERRED"):
        asyncio.run(cross.observe_three_way_clip_asr(prepared, bad))
    assert bad.calls == ["a", "b"]
    assert not prepared["receipt"].exists()


@pytest.mark.parametrize("which", ["asr", "media", "diagnostic"])
def test_mutation_during_transcription_never_commits(
    tmp_path, monkeypatch, which,
):
    _, prepared, media, transcript, _ = _case(tmp_path, monkeypatch)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    target = {
        "asr": transcript,
        "media": media,
        "diagnostic": prepared["acoustic"]["dialogue"]["review"]["diagnostics"]["receipt"],
    }[which]

    def mutate(role):
        if role != "a":
            return
        if which == "asr":
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["segments"][-1]["end"] = 26.0
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        else:
            target.write_bytes(b"changed during inference")

    with pytest.raises(ValueError):
        asyncio.run(cross.observe_three_way_clip_asr(
            prepared, _FakeAsr(hook=mutate),
        ))
    assert not prepared["receipt"].exists()


def test_public_data_gate_blocks_even_direct_api_before_provider(
    tmp_path, monkeypatch,
):
    _, prepared, _, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    _fake_extract(monkeypatch)
    with pytest.raises(ValueError, match="PUBLIC_DATA_REQUIRED"):
        asyncio.run(cross.observe_three_way_clip_asr(
            prepared, _FakeAsr(),
        ))
    assert not prepared["receipt"].exists()


def test_cli_preview_is_local_and_execute_requires_public_flag(
    tmp_path, monkeypatch,
):
    params, prepared, _, _, index = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "MeshTranscriber",
        lambda: pytest.fail("No provider allowed in default preview"),
    )
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    args = [
        "render-cut-extension-three-way-clip-asr", *_args(params)[1:],
        "--proposal-index", str(index),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["clip_audio_transcribed"] is False
    assert not prepared["receipt"].exists()
    gated = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert gated.exit_code == 6
    assert json.loads(gated.stdout)["status"] == "LOCAL_THREE_WAY_CLIP_ASR_BLOCKED"
    assert not prepared["receipt"].exists()
