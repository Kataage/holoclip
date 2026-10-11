"""Offline A/B/B2 source-ASR hypotheses: grounded quotes, no quality promotion."""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_three_way_dialogue as dialogue
from test_render_cut_extension_three_way_triage import _prepared
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    params, reviewed, media, transcript, _ = _prepared(tmp_path, monkeypatch)
    result = dialogue.prepare_three_way_dialogue(
        **params,
        proposal_index=reviewed["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"],
    )
    return params, result, media, transcript


def _claims(pack, *, variant=None, misquote=False):
    rows = []
    for v in dialogue._VARIANTS:
        excerpts = pack["variants"][v]["excerpts"]
        for kind in dialogue._KINDS:
            yes = bool(excerpts) and kind == "SPEECH_EDGE"
            cite = excerpts[0] if yes else None
            rows.append({
                "variant": v,
                "question": kind,
                "status": "POSSIBLE_TEXTUAL_SIGNAL" if yes else "INSUFFICIENT_EVIDENCE",
                "evidence_id": (
                    "a-e99" if misquote and v == (variant or "a") and yes else
                    cite["evidence_id"] if cite else None
                ),
                "quote": (
                    "definitely not found" if misquote and v == (variant or "a") and yes
                    else cite["unverified_asr_text"][:min(8, len(cite["unverified_asr_text"]))]
                    if cite else None
                ),
            })
    return {"claims": rows}


class FakeClient:
    def __init__(self, result, hook=None):
        self.result = result
        self.hook = hook
        self.calls = 0
        self.last_provenance = {"provider": "synthetic_mock", "model": "no_network"}

    async def chat_json(self, messages, **kwargs):
        self.calls += 1
        assert messages[0]["role"] == "system"
        assert kwargs["temperature"] == 0
        assert kwargs["max_tokens"] <= 1800
        if self.hook:
            self.hook()
        return self.result


def test_readonly_preview_never_exposes_asr_or_modifies_media(tmp_path, monkeypatch):
    _, prepared, media, transcript = _case(tmp_path, monkeypatch)
    original = media.read_bytes()
    asr = transcript.read_bytes()
    report = dialogue.preview_three_way_dialogue(prepared)
    assert report["status"] == "LOCAL_DIALOGUE_ASR_EVIDENCE_PREPARED_UNVERIFIED"
    assert set(report["excerpts_per_variant"]) == {"a", "b", "b2"}
    assert report["source_asr_audio_verified"] is False
    assert report["model_inference_performed"] is False
    assert report["narrative_quality_verified"] is False
    assert report["ready"] is False
    assert report["best_variant"] is None
    assert report["auto_adoption_allowed"] is False
    assert not prepared["receipt"].exists()
    assert media.read_bytes() == original
    assert transcript.read_bytes() == asr
    serialized = json.dumps(report, ensure_ascii=False)
    assert "まだ発話は続いている" not in serialized
    assert str(tmp_path) not in serialized


def test_model_returns_cited_unverified_hypotheses_not_quality_scores(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _case(tmp_path, monkeypatch)
    pack = dialogue._pack(prepared)
    sample = _claims(pack)
    before = (media.read_bytes(), transcript.read_bytes())
    client = FakeClient(sample)
    report = asyncio.run(dialogue.infer_three_way_dialogue(prepared, client))
    assert client.calls == 1
    assert report["status"] == "LOCAL_DIALOGUE_TEXTUAL_HYPOTHESES_NOT_EDITORIAL_QA"
    assert len(report["claims"]) == 9
    assert report["provider"] == "synthetic_mock"
    assert report["model_inference_performed"] is True
    assert report["actual_audio_or_video_interpreted_by_model"] is False
    assert report["narrative_quality_verified"] is False
    assert report["human_editorial_qa_completed"] is False
    assert report["best_variant"] is None
    assert report["ready"] is False
    assert report["publication_performed"] is False
    assert report["auto_adoption_allowed"] is False
    assert all(x["actual_story_role_or_speech_accuracy_verified"] is False
               for x in report["claims"])
    dialogue.save_three_way_dialogue(report, prepared)
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8")) == report
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE"):
        dialogue.save_three_way_dialogue(report, prepared)
    assert (media.read_bytes(), transcript.read_bytes()) == before


@pytest.mark.parametrize("kind", [
    "wrong_id", "wrong_text", "foreign_variant", "invalid_label",
    "duplicate", "fewer", "invented_structure", "insufficient_with_quote",
])
def test_model_unverified_output_cannot_bypass_grounding(
    tmp_path, monkeypatch, kind,
):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    pack = dialogue._pack(prepared)
    response = _claims(pack)
    # Choose a positive citation to attack; other claims abstain.
    positive = next((row for row in response["claims"]
                     if row["status"] == "POSSIBLE_TEXTUAL_SIGNAL"), None)
    if positive is None:
        pytest.skip("no fully-contained synthetic ASR excerpt to cite")
    if kind == "wrong_id":
        positive["evidence_id"] = "b2-e999"
    elif kind == "wrong_text":
        positive["quote"] = "not a contiguous ASR quote"
    elif kind == "foreign_variant":
        positive["variant"] = "injected"
    elif kind == "invalid_label":
        positive["status"] = "NARRATIVE_CONFIRMED"
    elif kind == "duplicate":
        response["claims"][-1] = dict(response["claims"][0])
    elif kind == "fewer":
        response["claims"].pop()
    elif kind == "invented_structure":
        response["other"] = "unauthorized"
    else:
        positive["status"] = "INSUFFICIENT_EVIDENCE"
    client = FakeClient(response)
    with pytest.raises(ValueError):
        asyncio.run(dialogue.infer_three_way_dialogue(prepared, client))
    assert not prepared["receipt"].exists()


def test_asr_or_media_changed_during_external_inference_blocks_report(
    tmp_path, monkeypatch,
):
    _, prepared, media, transcript = _case(tmp_path, monkeypatch)
    pack = dialogue._pack(prepared)
    reply = _claims(pack)
    original = json.loads(transcript.read_text(encoding="utf-8"))

    def change_source():
        amended = dict(original)
        amended["segments"] = [dict(s) for s in original["segments"]]
        amended["segments"][-1]["end"] = 25.0
        transcript.write_text(
            json.dumps(amended, ensure_ascii=False), encoding="utf-8",
        )

    client = FakeClient(reply, hook=change_source)
    with pytest.raises(ValueError):
        asyncio.run(dialogue.infer_three_way_dialogue(prepared, client))
    assert not prepared["receipt"].exists()
    assert media.is_file()


def test_stale_diagnostic_blocks_model_preparation(
    tmp_path, monkeypatch,
):
    _, prepared, _, _ = _case(tmp_path, monkeypatch)
    receipt = prepared["review"]["diagnostics"]["receipt"]
    receipt.write_bytes(b"bad diagnostic evidence")
    with pytest.raises(ValueError):
        dialogue.preview_three_way_dialogue(prepared)
    assert not prepared["receipt"].exists()


def test_cli_preview_and_save_are_local_and_public_gate_blocks_model(
    tmp_path, monkeypatch,
):
    params, prepared, _, _ = _case(tmp_path, monkeypatch)
    index = prepared["review"]["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    args = [
        "render-cut-extension-three-way-dialogue",
        *_args(params)[1:],
        "--proposal-index", str(index),
    ]
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *a, **k: pytest.fail("preview cannot create external clients"),
    )
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    plain = CliRunner().invoke(cli.app, args)
    assert plain.exit_code == 0, plain.stdout
    assert json.loads(plain.stdout)["asr_text_exported"] is False
    assert not prepared["receipt"].exists()
    blocked = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert blocked.exit_code == 6
    assert json.loads(blocked.stdout)["status"] == "LOCAL_THREE_WAY_DIALOGUE_BLOCKED"
    assert not prepared["receipt"].exists()
    saved = CliRunner().invoke(cli.app, [*args, "--save"])
    assert saved.exit_code == 0, saved.stdout
    assert prepared["receipt"].is_file()
    assert json.loads(prepared["receipt"].read_text(encoding="utf-8"))["model_inference_performed"] is False
    refused = CliRunner().invoke(cli.app, [*args, "--save"])
    assert refused.exit_code == 6


def test_cli_never_overwrites_preexisting_dialogue_before_provider_call(
    tmp_path, monkeypatch,
):
    params, prepared, _, _ = _case(tmp_path, monkeypatch)
    index = prepared["review"]["diagnostics"]["finished"]["b2"]["prepared"]["trial"]["proposal_index"]
    prepared["receipt"].write_bytes(b"operator existing sidecar")
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *a, **k: pytest.fail("no call with occupied output"),
    )
    result = CliRunner().invoke(cli.app, [
        "render-cut-extension-three-way-dialogue",
        *_args(params)[1:], "--proposal-index", str(index), "--execute",
    ])
    assert result.exit_code == 6
    assert prepared["receipt"].read_bytes() == b"operator existing sidecar"
