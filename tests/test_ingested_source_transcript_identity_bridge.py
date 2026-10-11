"""The native ingest format has segments but no embedded source video ID.

Use trusted source URL + manifest path association, never just filename or
fabricated metadata. All test media is synthetic/local.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_same_source_geometry as geometry
from holoclip import render_same_source_semantic_review as semantic
from holoclip import render_real_source_omission_probe as omission
from holoclip.models import TranscriptSegment
from holoclip.transcript import save_transcript
from test_render_same_source_geometry import _build, _human_example
from test_render_ready_queue import VIDEO


def _native(tmp_path, *, examples=1, manifest=True, metadata=False):
    root = tmp_path / "transcripts"
    source_path = root / "sources" / f"{VIDEO}.json"
    clips = []
    for index in range(examples):
        human = _human_example(
            clip=f"human-{index}", ranges=[
                (0, 2, 10, 12), (2, 5, 21, 24), (5, 7, 31, 33),
            ],
        )
        if manifest:
            human.source_transcript_path = str(source_path)
        clips.append(human)
    params, receipt, timeline, media = _build(tmp_path, clips=clips)
    save_transcript([
        TranscriptSegment(start=11.0, end=11.6, text="導入部分"),
        TranscriptSegment(start=12.0, end=12.8, text="次の話"),
        TranscriptSegment(start=21.0, end=21.8, text="中盤の話"),
        TranscriptSegment(start=23.0, end=23.6, text="反応"),
        TranscriptSegment(start=31.0, end=31.9, text="結末"),
    ], source_path)
    if metadata:
        data = json.loads(source_path.read_text(encoding="utf-8"))
        data["video_id"] = VIDEO
        source_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return params, receipt, timeline, media, source_path


def test_native_ingest_segments_only_qualifies_for_real_observations(tmp_path):
    from holoclip.render_same_source_transcript_context import (
        INGESTED_MANIFEST_BASIS,
    )
    params, _, _, media, source = _native(tmp_path)
    assert set(json.loads(source.read_text(encoding="utf-8"))) == {"segments"}
    original = media.read_bytes()
    report = omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )
    assert report["status"] == "LOCAL_REAL_PROBE_SOURCE_CLOCK_OBSERVED_UNQUALIFIED"
    assert report["reference_count"] == 1
    assert len(report["results"][0]["probes"]) == 3
    assert report["results"][0]["probes"][0][
        "source_clock_utterances_with_observed_loss"
    ] >= 1
    assert report["real_video_semantic_accuracy"] is None
    assert report["ready"] is False
    assert media.read_bytes() == original
    context = geometry.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"]
    assert context["status"] == "TRANSCRIPT_CONTEXT_AVAILABLE"
    assert context["source_identity_basis"] == INGESTED_MANIFEST_BASIS
    assert context["local_asr_audio_independently_verified"] is False


def test_native_ingest_source_also_enables_opt_in_semantic_evidence_without_inference(tmp_path):
    params, *_ = _native(tmp_path)
    report = geometry.compare_same_source_edit_geometry(**params)
    prepared = semantic.prepare_semantic_evidence(report)
    assert prepared["status"] == "LOCAL_SEMANTIC_EVIDENCE_PREPARED"
    assert prepared["external_inference_performed"] is False
    assert prepared["cases"][0]["source_timed_unverified_asr"]
    assert prepared["ready"] is False
    assert prepared["human_training_references_not_holdout_gold"] is True


def test_missing_manifest_does_not_promote_filename_identity(tmp_path):
    params, _, _, _, source = _native(tmp_path, manifest=False)
    assert source.is_file()
    ctx = geometry.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"]
    assert ctx["source_identity_basis"] == "FILENAME_ONLY_IDENTITY_UNVERIFIED"
    assert semantic.prepare_semantic_evidence(
        geometry.compare_same_source_edit_geometry(**params)
    )["cases"] == []
    probe = omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )
    assert probe["reference_count"] == 0
    assert probe["untrusted_or_ambiguous_local_asr_count"] == 1


def test_incorrect_manifest_location_not_accepted_as_ingest_evidence(tmp_path):
    params, _, _, _, source = _native(tmp_path)
    from holoclip.examples import load_human_clip_store, upsert_human_clip_examples
    examples = load_human_clip_store(params["example_store"])
    examples[0].source_transcript_path = str(source.parent / "another-stream.json")
    upsert_human_clip_examples(params["example_store"], examples)
    ctx = geometry.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"]
    assert ctx["source_identity_basis"] == "FILENAME_ONLY_IDENTITY_UNVERIFIED"
    assert omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )["reference_count"] == 0


def test_conflicting_trusted_manifests_cannot_choose_one_for_same_stream(tmp_path):
    from holoclip.examples import load_human_clip_store, upsert_human_clip_examples
    params, *_ = _native(tmp_path, examples=2)
    examples = load_human_clip_store(params["example_store"])
    examples[1].source_transcript_path = str(
        params["transcript_root"] / "sources" / "elsewhere.json"
    )
    upsert_human_clip_examples(params["example_store"], examples)
    report = geometry.compare_same_source_edit_geometry(**params)
    for item in report["inspection_queue"]["results"]:
        assert item["transcript_context"]["source_identity_basis"] == (
            "FILENAME_ONLY_IDENTITY_UNVERIFIED"
        )
    assert semantic.prepare_semantic_evidence(report)["cases"] == []
    probes = omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )
    assert probes["reference_count"] == 0


def test_wrong_embedded_video_id_overrides_valid_manifest_and_is_rejected(tmp_path):
    params, _, _, _, source = _native(tmp_path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["video_id"] = "INVALID_ID_1"
    source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    result = geometry.compare_same_source_edit_geometry(**params)
    assert result["inspection_queue"]["results"][0]["transcript_context"][
        "status"
    ] == "TRANSCRIPT_UNTRUSTED"
    assert semantic.prepare_semantic_evidence(result)["cases"] == []
    probes = omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )
    assert probes["reference_count"] == 0
    assert probes["untrusted_or_ambiguous_local_asr_count"] == 1


def test_metadata_source_format_keeps_existing_valid_behavior(tmp_path):
    params, *_ = _native(tmp_path, manifest=False, metadata=True)
    report = geometry.compare_same_source_edit_geometry(**params)
    ctx = report["inspection_queue"]["results"][0]["transcript_context"]
    assert ctx["source_identity_basis"] == (
        "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED"
    )
    assert semantic.prepare_semantic_evidence(report)["cases"]


def test_filename_only_other_location_cannot_impersonate_ingest_artifact(tmp_path):
    params, _, _, _, source = _native(tmp_path)
    old_raw = source.read_bytes()
    source.unlink()
    alternate = params["transcript_root"] / f"{VIDEO}.json"
    alternate.write_bytes(old_raw)
    report = geometry.compare_same_source_edit_geometry(**params)
    ctx = report["inspection_queue"]["results"][0]["transcript_context"]
    assert ctx["source_identity_basis"] == "FILENAME_ONLY_IDENTITY_UNVERIFIED"
    assert semantic.prepare_semantic_evidence(report)["cases"] == []


def test_holdout_reservations_still_block_all_native_transcript_uses(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthState
    params, *_ = _native(tmp_path)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    report = geometry.compare_same_source_edit_geometry(**params)
    assert report["inspection_queue"]["results"] == []
    assert semantic.prepare_semantic_evidence(report)["cases"] == []
    assert omission.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
    )["status"] == "LOCAL_REAL_PROBE_HOLDOUT_GATES_BLOCKED"


def test_cli_native_ingest_report_readonly_with_zero_new_asr_or_network(tmp_path, monkeypatch):
    params, _, _, media, _ = _native(tmp_path)
    before = media.read_bytes()
    class NoProvider:
        def __init__(self, *args, **kwargs):
            pytest.fail("Cannot invoke AI in default local probe")
    monkeypatch.setattr(cli, "MeshLLMClient", NoProvider)
    command = [
        "render-real-source-omission-probe",
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
    ]
    res = CliRunner().invoke(cli.app, command)
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["reference_count"] == 1
    assert payload["external_ai_requests"] == 0
    assert media.read_bytes() == before
    assert VIDEO not in res.stdout
    assert "導入部分" not in res.stdout
