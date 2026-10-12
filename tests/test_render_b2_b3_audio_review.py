"""Offline SHA-bound B2/B3 review targets: text-free, never winners."""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_audio_review as review
from holoclip import render_b2_b3_finished_audio_asr as asr
from test_render_b2_b3_finished_audio_asr import _case, _fake_extract
from test_render_cut_extension_three_way_clip_asr import _FakeAsr
from test_render_cut_extension_mp4_ab import _args


def _measured(tmp_path, monkeypatch):
    options, source, transcript, edited = _case(tmp_path, monkeypatch)
    basis = asr.prepare_b2_b3_audio_asr(**options)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    measured = asyncio.run(asr.observe_b2_b3_audio_asr(
        basis, _FakeAsr(texts={
            "b2": "まだ発話は続いている",
            "b3": "認識が違っている発話かもしれない",
        }),
    ))
    assert measured["edited_audio_transcribed"] is True
    return options, basis, source, transcript, edited


def test_no_asr_receipt_means_unknown_not_zero_quality_defects(tmp_path, monkeypatch):
    options, _, source, transcript, edited = _case(tmp_path, monkeypatch)
    original = source.read_bytes(), transcript.read_bytes(), edited.read_bytes()
    monkeypatch.setattr(
        asr, "_read_clip", lambda *a, **kw: pytest.fail("review must not decode audio"),
    )
    result = review.run_b2_b3_review(**options)
    assert result["status"] == "LOCAL_B2_B3_REVIEW_ASR_NOT_MEASURED"
    assert result["missing_asr_means_unknown_not_clean"] is True
    assert result["ready"] is False
    result = review.run_b2_b3_review(**options, save=True)
    assert result["status"] == "LOCAL_B2_B3_REVIEW_ASR_NOT_MEASURED"
    assert not list(options["preview_root"].rglob("*.b2-b3-review.json"))
    assert (source.read_bytes(), transcript.read_bytes(), edited.read_bytes()) == original


def test_measured_b2_b3_generates_priorities_without_retranscribing(
    tmp_path, monkeypatch,
):
    options, basis, source, transcript, edited = _measured(tmp_path, monkeypatch)
    previous = (
        source.read_bytes(), transcript.read_bytes(), edited.read_bytes(),
        basis["receipt"].read_bytes(),
    )
    monkeypatch.setattr(
        asr, "_read_clip", lambda *a, **kw: pytest.fail("ASR already saved"),
    )
    monkeypatch.setattr(
        review, "save_semantic_review",
        lambda *args: pytest.fail("preview must not write"),
    )
    result = review.run_b2_b3_review(**options)
    assert result["status"] == "LOCAL_B2_B3_REVIEW_TARGETS_UNRATED_ASR_AND_QA"
    assert result["four_way_diagnostics_sha256"] == basis["four_way_sha256"]
    assert result["b2_b3_audio_asr_receipt_sha256"]
    assert result["finished_mp4_sha256"] == asr._public(basis)["finished_mp4_sha256"]
    assert result["review_target_count"] == len(result["targets"])
    assert result["high_review_count"] + result["normal_review_count"] + (
        result["low_review_count"]
    ) == len(result["targets"])
    assert all(t["reviewed_by_human"] is False for t in result["targets"])
    assert all(t["missing_speech_or_repetition_proven"] is False
               for t in result["targets"])
    assert result["video_or_audio_decoding_performed"] is False
    assert result["provider_requests_performed"] is False
    assert result["automatic_winner"] is None
    assert result["ready"] is False and result["publication_performed"] is False
    assert not list(options["preview_root"].rglob("*.b2-b3-review.json"))
    raw = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in raw
    assert "まだ発話は続いている" not in raw
    assert (source.read_bytes(), transcript.read_bytes(), edited.read_bytes(),
            basis["receipt"].read_bytes()) == previous


def test_save_creates_one_immutable_review_only(tmp_path, monkeypatch):
    options, basis, source, transcript, _ = _measured(tmp_path, monkeypatch)
    old = source.read_bytes(), transcript.read_bytes(), basis["receipt"].read_bytes()
    first = review.run_b2_b3_review(**options, save=True)
    receipts = list(options["preview_root"].rglob("*.b2-b3-review.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8")) == first
    assert first["automatic_adoption_allowed"] is False
    assert first["editorial_quality_improvement"] is None
    with pytest.raises(ValueError):
        review.run_b2_b3_review(**options, save=True)
    assert json.loads(receipts[0].read_text(encoding="utf-8")) == first
    assert (source.read_bytes(), transcript.read_bytes(), basis["receipt"].read_bytes()) == old


@pytest.mark.parametrize("tamper", [
    "lineage", "focus", "alignment_id", "warning_count", "comparison",
    "human_qa", "recipient_text",
])
def test_tampered_audio_asr_receipt_fails_closed(tmp_path, monkeypatch, tamper):
    options, basis, *_ = _measured(tmp_path, monkeypatch)
    original = json.loads(basis["receipt"].read_text(encoding="utf-8"))
    if tamper == "lineage":
        original["source_asr_sha256"] = "0" * 64
    elif tamper == "focus":
        original["focus_by_variant"]["b3"]["output_start_seconds"] += 1
    elif tamper == "alignment_id":
        alignment = original["variants"]["b3"]["temporal_asr_review"]["source_utterance_alignment"]
        if alignment:
            alignment[0]["source_evidence_id"] = "unknown"
        else:
            original["variants"]["b3"]["temporal_asr_review"]["expected_utterance_count"] += 1
    elif tamper == "warning_count":
        original["variants"]["b3"]["temporal_asr_review"]["time_anchor_warning_count"] += 1
    elif tamper == "comparison":
        original["comparison"]["b3_more_temporal_warnings_on_shared_intervals"] += 1
    elif tamper == "human_qa":
        original["human_audio_review_completed"] = True
    else:
        original["transcript_text"] = "injected"
    basis["receipt"].write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError):
        review.run_b2_b3_review(**options, save=True)
    assert not list(options["preview_root"].rglob("*.b2-b3-review.json"))


def test_review_can_rank_specific_asr_and_technical_hints_without_winner(monkeypatch):
    """A synthetic verified basis exercises *which* events rise to the top."""
    def r(key, clock):
        return {"id": key, "source_start_seconds": clock,
                "source_end_seconds": clock + 1,
                "output_start_seconds": clock / 10.,
                "output_end_seconds": clock / 10. + 1}
    b2_refs = [r("shared-warning", 10), r("b2-only", 40), r("shared-good", 20)]
    b3_refs = [r("shared-warning", 10), r("b3-only", 50), r("shared-good", 20)]
    def observation(refs, statuses):
        return {"temporal_asr_review": {"source_utterance_alignment": [
            {"source_evidence_id": x["id"], "status": statuses[x["id"]]}
            for x in refs
        ]}}
    b2 = observation(b2_refs, {
        "shared-warning": asr._NO_WARNING, "b2-only": asr._NO_WARNING,
        "shared-good": "NO_EDITED_ASR_NEAR_EXPECTED_TIME",
    })
    b3 = observation(b3_refs, {
        "shared-warning": "PARTIAL_ASR_TEXT_OVERLAP_REVIEW",
        "b3-only": asr._NO_WARNING, "shared-good": asr._NO_WARNING,
    })
    qa = {"variants": {
        "b2": {
            "technical_diagnostic": {
                "flag_count": 1, "black_interval_count": 0,
                "freeze_interval_count": 1,
            },
            "edge_audio_samples": [
                {"kind": "BEFORE_EDIT_EDGE_NOT_SPEECH_PROOF",
                 "near_silent_energy_hint": False},
            ],
        },
        "b3": {
            "technical_diagnostic": {
                "flag_count": 4, "black_interval_count": 1,
                "freeze_interval_count": 2,
            },
            "edge_audio_samples": [
                {"kind": "BEFORE_EDIT_EDGE_NOT_SPEECH_PROOF",
                 "near_silent_energy_hint": True},
            ],
        },
    }}
    monkeypatch.setattr(review, "_four_way", lambda context: (qa, "f" * 64))
    basis = {
        "context": {"job_id": "case", "trial_id": "trial", "b2_id": "b2",
                    "b3_id": "b3", "source_cut_index": 0, "candidate_index": 0},
        "four_way_sha256": "f" * 64, "source_sha256": "a" * 64,
        "references": {"b2": b2_refs, "b3": b3_refs},
    }
    # Only public SHA fields are needed for the mocked evidence summary.
    monkeypatch.setattr(review, "_asr_preview", lambda _: {
        "finished_mp4_sha256": {"b2": "b" * 64, "b3": "c" * 64},
    })
    outcome = review._review(basis, {"variants": {"b2": b2, "b3": b3},
                                    "comparison": {"no_winner": True}}, "d" * 64)
    kinds = [x["kind"] for x in outcome["targets"]]
    assert kinds[0] == "B3_MORE_TECHNICAL_FLAGS_NOT_QUALITY"
    assert "B3_NEW_ASR_WARNING" in kinds
    assert "B2_ONLY_SOURCE_ASR_INTERVAL_RECHECK" in kinds
    assert "B3_NEW_SOURCE_ASR_INTERVAL_UNVERIFIED" in kinds
    assert "B3_FEWER_WARNINGS_NOT_VERIFIED_BETTER" in kinds
    assert "B3_NEAR_SILENT_ENERGY_AT_EDIT_EDGE" in kinds
    assert outcome["automatic_winner"] is None
    assert outcome["human_audio_review_completed"] is False


def test_cli_default_missing_and_valid_saved_asr(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    argv = [
        "render-b2-b3-audio-review", *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    missing = CliRunner().invoke(cli.app, argv)
    assert missing.exit_code == 0, missing.stdout
    assert json.loads(missing.stdout)["status"] == "LOCAL_B2_B3_REVIEW_ASR_NOT_MEASURED"
    # CLI must not reach an ASR provider even with the public-data flag set.
    monkeypatch.setattr(cli, "MeshTranscriber",
                        lambda: pytest.fail("review never contacts provider"))
    basis = asr.prepare_b2_b3_audio_asr(**options)
    _fake_extract(monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    asyncio.run(asr.observe_b2_b3_audio_asr(basis, _FakeAsr()))
    valid = CliRunner().invoke(cli.app, argv)
    assert valid.exit_code == 0, valid.stdout
    assert json.loads(valid.stdout)["status"] == "LOCAL_B2_B3_REVIEW_TARGETS_UNRATED_ASR_AND_QA"
    saved = CliRunner().invoke(cli.app, [*argv, "--save"])
    assert saved.exit_code == 0, saved.stdout
    assert list(options["preview_root"].rglob("*.b2-b3-review.json"))
    denied = CliRunner().invoke(cli.app, [*argv, "--save"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
