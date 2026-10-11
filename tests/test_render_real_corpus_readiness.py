"""Existing local corpus inventory: offline, source-diverse and unqualified."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_real_corpus_readiness as inventory
from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import load_human_clip_store, upsert_human_clip_examples
from holoclip.models import TranscriptSegment
from holoclip.transcript import save_transcript
from test_ingested_source_transcript_identity_bridge import _native
from test_render_same_source_geometry import _human_example
from test_render_ready_queue import VIDEO


def _inspect(params, **kwargs):
    return inventory.inspect_real_corpus_readiness(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
        **kwargs,
    )


def _cli(params, *args):
    return [
        "render-real-corpus-readiness",
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
        *args,
    ]


def test_original_ingest_no_embedded_id_generates_real_readiness_without_network(tmp_path):
    params, _, _, media, _ = _native(tmp_path)
    original = media.read_bytes()
    result = _inspect(params)
    assert result["status"] == "LOCAL_REAL_INVENTORY_POSITIONAL_CASES_AVAILABLE_UNQUALIFIED"
    assert result["trusted_stream_count"] == 1
    assert result["scanned_stream_count"] == 1
    assert result["scope_exhaustive"] is True
    assert result["source_asr_ingest_manifest_matched_stream_count"] == 1
    assert result["source_asr_metadata_verified_stream_count"] == 0
    assert result["eligible_two_segment_reference_count"] == 1
    assert result["role_description_complete_reference_count"] == 1
    assert result["positional_evidence_reference_count"] == 1
    assert result["role_hypothesis_case_possible_reference_count"] == 1
    assert result["positionally_observable_distinct_stream_count"] == 1
    assert result["selected_reference_count"] == 1
    chosen = result["suggested_in_sample_references"][0]
    assert chosen["local_asr_sampled_by_human_clip_position"] == {
        "opening_aligned": 1, "intermediate_aligned": 2,
        "closing_aligned": 1,
    }
    assert chosen["human_clip_level_role_descriptions_present"] is True
    assert chosen["human_reference_is_training_in_sample"] is True
    assert chosen["independent_gold_not_available"] is True
    assert result["real_video_role_accuracy"] is None
    assert result["ready"] is False
    assert result["external_ai_requests"] == 0
    assert result["new_asr_performed"] is False
    assert result["media_downloads_performed"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert VIDEO not in serialized
    assert "導入部分" not in serialized
    assert "ordinary setup" not in serialized
    assert str(tmp_path) not in serialized
    assert media.read_bytes() == original


def test_metadata_association_is_counted_separately(tmp_path):
    params, *_ = _native(tmp_path, manifest=False, metadata=True)
    result = _inspect(params)
    assert result["source_asr_metadata_verified_stream_count"] == 1
    assert result["source_asr_ingest_manifest_matched_stream_count"] == 0
    assert result["role_hypothesis_case_possible_reference_count"] == 1


def test_missing_manifest_cannot_upgrade_filename_only_evidence(tmp_path):
    params, *_ = _native(tmp_path, manifest=False)
    report = _inspect(params)
    assert report["source_asr_filename_only_stream_count"] == 1
    assert report["role_hypothesis_case_possible_reference_count"] == 0
    assert report["selected_reference_count"] == 0
    assert report["status"] == "LOCAL_REAL_INVENTORY_NO_ROLE_CASES_UNQUALIFIED"


def test_missing_transcript_and_wrong_source_id_report_distinct_reasons(tmp_path):
    params, _, _, _, path = _native(tmp_path)
    path.unlink()
    missing = _inspect(params)
    assert missing["source_asr_missing_stream_count"] == 1
    assert missing["source_asr_untrusted_or_ambiguous_stream_count"] == 0
    path.write_text(json.dumps({
        "video_id": "NOT_MATCH_0",
        "segments": [{"start": 11, "end": 12, "text": "invalid"}],
    }), encoding="utf-8")
    invalid = _inspect(params)
    assert invalid["source_asr_missing_stream_count"] == 0
    assert invalid["source_asr_untrusted_or_ambiguous_stream_count"] == 1
    assert invalid["selected_reference_count"] == 0


def test_two_human_clips_same_source_choose_one_without_double_counting_stream(tmp_path):
    params, *_ = _native(tmp_path, examples=2)
    report = _inspect(params)
    assert report["trusted_stream_count"] == 1
    assert report["scanned_stream_count"] == 1
    assert report["eligible_two_segment_reference_count"] == 2
    assert report["role_hypothesis_case_possible_reference_count"] == 2
    assert report["selected_reference_count"] == 1
    assert len(report["suggested_in_sample_references"]) == 1
    assert report == _inspect(params)


def test_missing_closing_asr_is_not_a_semantic_role_case(tmp_path):
    params, _, _, _, path = _native(tmp_path)
    save_transcript([
        TranscriptSegment(start=11, end=11.5, text="only opening"),
        TranscriptSegment(start=21, end=21.5, text="middle"),
    ], path)
    result = _inspect(params)
    assert result["eligible_two_segment_reference_count"] == 1
    assert result["role_description_complete_reference_count"] == 1
    assert result["positional_evidence_reference_count"] == 0
    assert result["role_hypothesis_case_possible_reference_count"] == 0
    assert result["selected_reference_count"] == 0
    assert result["status"] == "LOCAL_REAL_INVENTORY_NO_ROLE_CASES_UNQUALIFIED"


def test_source_diverse_selection_and_scan_truncation_are_explicit(tmp_path):
    params, *_ = _native(tmp_path)
    second_stream = "AbCdEfGhI01"
    second = _human_example(
        clip="human-second-source", stream=second_stream,
        ranges=[(0, 2, 10, 12), (2, 5, 21, 24)],
    )
    second.source_transcript_path = str(
        params["transcript_root"] / "sources" / f"{second_stream}.json"
    )
    examples = load_human_clip_store(params["example_store"])
    upsert_human_clip_examples(params["example_store"], [*examples, second])
    save_transcript([
        TranscriptSegment(start=10.3, end=11, text="second opening"),
        TranscriptSegment(start=21.3, end=22, text="second close"),
    ], params["transcript_root"] / "sources" / f"{second_stream}.json")
    entire = _inspect(params)
    assert entire["trusted_stream_count"] == 2
    assert entire["scanned_stream_count"] == 2
    assert entire["selected_reference_count"] == 2
    assert len({x["source_stream_sha256"] for x in
                entire["suggested_in_sample_references"]}) == 2
    one = _inspect(params, max_streams=1)
    assert one["trusted_stream_count"] == 2
    assert one["scanned_stream_count"] == 1
    assert one["remaining_stream_count_not_scanned"] == 1
    assert one["scope_exhaustive"] is False
    assert one == _inspect(params, max_streams=1)
    limited = _inspect(params, max_suggestions=1)
    assert limited["selected_reference_count"] == 1
    assert limited["role_hypothesis_case_possible_reference_count"] == 2


def test_holdout_conflict_blocks_all_source_scan_and_redacts_refs(tmp_path):
    params, *_ = _native(tmp_path)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    report = _inspect(params)
    assert report["status"] == "LOCAL_REAL_INVENTORY_HOLDOUT_BLOCKED"
    assert report["scanned_stream_count"] == 0
    assert report["suggested_in_sample_references"] == []
    assert report["ready"] is False


def test_missing_holdout_state_fail_closed(tmp_path):
    params, *_ = _native(tmp_path)
    params["growth_state"].unlink()
    report = _inspect(params)
    assert report["holdout_reservation_metadata_verified"] is False
    assert report["status"] == "LOCAL_REAL_INVENTORY_HOLDOUT_BLOCKED"
    assert report["selected_reference_count"] == 0


@pytest.mark.parametrize("options", [
    {"max_streams": 0}, {"max_streams": 129},
    {"max_streams": True}, {"max_streams": 2.5},
    {"max_suggestions": 0}, {"max_suggestions": 13},
    {"max_suggestions": False}, {"max_suggestions": "2"},
])
def test_bounds_rejected_before_any_asr_scan(tmp_path, options):
    params, *_ = _native(tmp_path)
    with pytest.raises(ValueError, match="LIMIT_INVALID"):
        _inspect(params, **options)


def test_cli_is_readonly_by_default_and_create_only_when_opted_in(tmp_path, monkeypatch):
    params, _, _, media, _ = _native(tmp_path)
    baseline = media.read_bytes()
    class DenyExternal:
        def __init__(self, *args, **kwargs):
            pytest.fail("inventory must not initialize model runtime")
    monkeypatch.setattr(cli, "MeshLLMClient", DenyExternal)
    runner = CliRunner()
    target = tmp_path / "summary.json"
    dry = runner.invoke(cli.app, _cli(params))
    assert dry.exit_code == 0, dry.stdout
    assert not target.exists()
    doc = json.loads(dry.stdout)
    assert doc["selected_reference_count"] == 1
    saved = runner.invoke(cli.app, _cli(params, "--output", str(target)))
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(saved.stdout) == doc
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    repeat = runner.invoke(cli.app, _cli(params, "--output", str(target)))
    assert repeat.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    assert media.read_bytes() == baseline


def test_cli_holdout_block_is_nonzero_without_content_leak(tmp_path):
    params, *_ = _native(tmp_path)
    params["growth_state"].unlink()
    res = CliRunner().invoke(cli.app, _cli(params))
    assert res.exit_code == 6
    data = json.loads(res.stdout)
    assert data["status"] == "LOCAL_REAL_INVENTORY_HOLDOUT_BLOCKED"
    assert data["ready"] is False
    assert "導入部分" not in res.stdout


def test_ambiguous_source_transcript_paths_are_untrusted(tmp_path):
    params, _, _, _, _ = _native(tmp_path)
    duplicate = params["transcript_root"] / f"{VIDEO}.json"
    duplicate.write_text('{"segments":[]}', encoding="utf-8")
    report = _inspect(params)
    assert report["source_asr_untrusted_or_ambiguous_stream_count"] == 1
    assert report["selected_reference_count"] == 0
