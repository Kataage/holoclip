"""Real aligned-corpus omission probes: strict offline, unqualified evidence."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_real_source_omission_probe as probes
from holoclip.corpus_growth import CorpusGrowthState
from test_render_same_source_geometry import _build, _human_example
from test_render_ready_queue import VIDEO


_THREE = [
    (0, 2, 10, 12), (2, 5, 21, 24), (5, 8, 31, 34),
]


def _scenario(tmp_path, *, clips=None, with_asr=True, metadata=True):
    examples = clips if clips is not None else [_human_example(ranges=_THREE)]
    params, _, _, media = _build(tmp_path, clips=examples)
    if with_asr:
        cache = params["transcript_root"]
        cache.mkdir(exist_ok=True)
        payload = {
            "segments": [
                {"start": 10.4, "end": 11.5, "text": "最初の導入例"},
                {"start": 21.4, "end": 22.6, "text": "中盤の会話例"},
                {"start": 31.3, "end": 32.6, "text": "最後の反応例"},
                {"start": 50, "end": 51, "text": "遠くの無関係な配信"},
            ],
        }
        if metadata:
            payload["video_id"] = VIDEO
        (cache / f"{VIDEO}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return params, media


def _probe(params, *, max_examples=12):
    return probes.probe_real_source_omissions(
        example_store=params["example_store"],
        growth_state=params["growth_state"],
        transcript_root=params["transcript_root"],
        max_examples=max_examples,
    )


def _command(params, *extra):
    return [
        "render-real-source-omission-probe",
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
        *extra,
    ]


def test_real_three_segment_alignment_produces_source_timed_omissions(tmp_path):
    params, media = _scenario(tmp_path)
    original = media.read_bytes()
    report = _probe(params)
    assert report["status"] == "LOCAL_REAL_PROBE_SOURCE_CLOCK_OBSERVED_UNQUALIFIED"
    assert report["reference_count"] == 1
    assert report["candidate_trusted_reference_count"] == 1
    assert report["holdout_reservation_metadata_verified"] is True
    assert report["source_alignments_from_training_not_independent_gold"] is True
    assert report["real_video_semantic_accuracy"] is None
    assert report["narrative_quality_precision"] is None
    assert report["ready"] is False and report["production_ready"] is False
    assert report["external_ai_requests"] == 0
    assert report["video_renders_performed"] == 0
    result = report["results"][0]
    assert len(result["probes"]) == 3
    assert result["human_aligned_segment_count"] == 3
    assert result["human_aligned_unique_source_seconds"] == 8
    positions = {x["omitted_position"]: x for x in result["probes"]}
    assert set(positions) == {
        "opening_aligned", "intermediate_aligned", "closing_aligned",
    }
    assert positions["opening_aligned"]["source_seconds_lost_after_union"] == 2
    assert positions["intermediate_aligned"]["source_seconds_lost_after_union"] == 3
    assert positions["closing_aligned"]["source_seconds_lost_after_union"] == 3
    for name, group in positions.items():
        assert group["source_clock_utterances_with_observed_loss"] == 1, name
        assert len(group["sampled_source_asr_utterances"]) == 1
        observation = group["sampled_source_asr_utterances"][0]
        assert observation["lost_source_clock_seconds"] > 0
        assert observation["counterfactual_source_clock_status"] == (
            "SOURCE_CLOCK_NOT_SELECTED"
        )
        assert len(observation["asr_text_sha256"]) == 64
        assert group["human_aligned_position_not_semantic_role"] is True
    serialized = json.dumps(report, ensure_ascii=False)
    assert VIDEO not in serialized
    assert "最初の導入例" not in serialized
    assert "中盤の会話例" not in serialized
    assert "最後の反応例" not in serialized
    assert media.read_bytes() == original


def test_two_aligned_spans_only_test_opening_and_closing(tmp_path):
    params, _ = _scenario(
        tmp_path, clips=[_human_example()], with_asr=True
    )
    result = _probe(params)
    assert result["reference_count"] == 1
    positions = [p["omitted_position"] for p in result["results"][0]["probes"]]
    assert positions == ["opening_aligned", "closing_aligned"]
    assert "intermediate_aligned" not in positions


def test_overlapping_reference_spans_count_unique_source_seconds(tmp_path):
    from holoclip.example_quality import evaluate_human_example_learning_quality
    example = _human_example(ranges=[
        (0, 2, 10, 12), (2, 5, 10, 13), (5, 8, 31, 34),
    ])
    # Learning gates remain authoritative. This test exercises the overlap
    # arithmetic only rather than weakening eligibility for unusual edits.
    if evaluate_human_example_learning_quality(example).decision.value != "TRUSTED":
        pytest.skip("Overlapping source spans not eligible for real-corpus trusted learning")
    params, _ = _scenario(tmp_path, clips=[example])
    result = _probe(params)
    assert result["reference_count"] == 1
    rows = {x["omitted_position"]: x for x in result["results"][0]["probes"]}
    assert rows["opening_aligned"]["source_seconds_lost_after_union"] == 0
    assert rows["opening_aligned"][
        "overlap_recovered_by_other_aligned_spans_seconds"
    ] == 2
    assert rows["intermediate_aligned"]["source_seconds_lost_after_union"] == 1


def test_no_transcript_never_fabricates_source_evidence(tmp_path):
    params, _ = _scenario(tmp_path, with_asr=False)
    result = _probe(params)
    assert result["reference_count"] == 0
    assert result["unavailable_local_asr_count"] == 1
    assert result["results"] == []
    assert result["status"] == "LOCAL_REAL_PROBE_UNQUALIFIED_NO_ELIGIBLE_EXAMPLES"


def test_filename_only_or_mismatched_asr_never_treated_as_verified_metadata(tmp_path):
    params, _ = _scenario(tmp_path, metadata=False)
    result = _probe(params)
    assert result["reference_count"] == 0
    assert result["untrusted_or_ambiguous_local_asr_count"] == 1
    path = params["transcript_root"] / f"{VIDEO}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stream_id"] = "wrong-source"
    path.write_text(json.dumps(payload), encoding="utf-8")
    changed = _probe(params)
    assert changed["reference_count"] == 0
    assert changed["untrusted_or_ambiguous_local_asr_count"] == 1


def test_missing_holdout_metadata_blocks_all_omission_probes(tmp_path):
    params, _ = _scenario(tmp_path)
    params["growth_state"].unlink()
    result = _probe(params)
    assert result["status"] == "LOCAL_REAL_PROBE_HOLDOUT_GATES_BLOCKED"
    assert result["results"] == []
    assert result["ready"] is False


def test_new_holdout_collision_blocks_all_real_reference_data(tmp_path):
    params, _ = _scenario(tmp_path)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    result = _probe(params)
    assert result["status"] == "LOCAL_REAL_PROBE_HOLDOUT_GATES_BLOCKED"
    assert result["results"] == []
    assert "最初の導入例" not in json.dumps(result, ensure_ascii=False)


def test_page_limit_and_deterministic_stable_hashed_reference_order(tmp_path):
    clips = [
        _human_example(clip=f"human-{i}", ranges=_THREE)
        for i in range(3)
    ]
    params, _ = _scenario(tmp_path, clips=clips)
    one = _probe(params, max_examples=1)
    again = _probe(params, max_examples=1)
    assert one == again
    assert one["reference_count"] == 1
    assert one["candidate_trusted_reference_count"] == 3
    assert one["listed_reference_limit_reached"] is True
    assert len(one["results"][0]["human_clip_identity_sha256"]) == 64


@pytest.mark.parametrize("bad", [0, -1, 21, 1.2, True])
def test_invalid_limits_block_before_expensive_corpus_reads(tmp_path, bad):
    params, _ = _scenario(tmp_path)
    with pytest.raises(ValueError, match="LIMIT_INVALID"):
        _probe(params, max_examples=bad)


def test_cli_dry_run_and_opt_in_immutable_json_are_equal(tmp_path):
    params, media = _scenario(tmp_path)
    original = media.read_bytes()
    target = tmp_path / "real-omission.json"
    runner = CliRunner()
    dry = runner.invoke(cli.app, _command(params))
    assert dry.exit_code == 0, dry.stdout
    assert not target.exists()
    doc = json.loads(dry.stdout)
    assert doc["reference_count"] == 1
    saved = runner.invoke(cli.app, _command(params, "--output", str(target)))
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(saved.stdout) == doc
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    repeat = runner.invoke(cli.app, _command(params, "--output", str(target)))
    assert repeat.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    assert media.read_bytes() == original


def test_cli_holdout_failure_is_nonzero_and_does_not_autopromote(tmp_path):
    params, _ = _scenario(tmp_path)
    params["growth_state"].unlink()
    result = CliRunner().invoke(cli.app, _command(params))
    assert result.exit_code == 6
    payload = json.loads(result.stdout)
    assert payload["status"] == "LOCAL_REAL_PROBE_HOLDOUT_GATES_BLOCKED"
    assert payload["ready"] is False


def test_multiple_source_transcript_locations_are_untrusted(tmp_path):
    params, _ = _scenario(tmp_path)
    root = params["transcript_root"]
    duplicate = root / "sources"
    duplicate.mkdir()
    (duplicate / f"{VIDEO}.json").write_text(json.dumps({
        "video_id": VIDEO, "segments": [],
    }), encoding="utf-8")
    result = _probe(params)
    assert result["reference_count"] == 0
    assert result["untrusted_or_ambiguous_local_asr_count"] == 1
