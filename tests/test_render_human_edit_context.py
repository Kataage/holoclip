"""Offline human alignment references and unscored A/B metrics stay separate."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_human_edit_context as ctx
from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import upsert_human_clip_examples
from holoclip.render_ab_metrics import VERSION as METRICS_VERSION
from test_editorial_profile import trusted_example


def _human(tmp_path, *, n=3, reserved=(), no_state=False):
    store = tmp_path / "clips.jsonl"
    state_file = tmp_path / "growth.json"
    examples = [
        trusted_example(f"clip-{i}", stream_id=f"stream-{i}", start=100 + 200 * i)
        for i in range(n)
    ]
    upsert_human_clip_examples(store, examples)
    if not no_state:
        state_file.write_text(
            CorpusGrowthState(holdout_stream_ids=list(reserved)).model_dump_json(),
            encoding="utf-8",
        )
    return store, state_file


def _metric(root, digit, *, kind="audio", tamper=False):
    root.mkdir(parents=True, exist_ok=True)
    stem = "renderqueue_" + digit * 32
    triage = root / f"{stem}.triage.json"
    triage.write_bytes(b'{"ready":false,"auto_repair_allowed":false}')
    t_sha = hashlib.sha256(triage.read_bytes()).hexdigest()
    ext = {
        "audio": ".ab-preview",
        "subtitle": ".subtitle-ab",
        "cut": ".cut-ab",
    }[kind]
    receipt = root / f"{stem}{ext}" / "receipt.json"
    receipt.parent.mkdir()
    receipt.write_bytes(b'{"test":"synthetic-verified-upstream"}')
    receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    metrics = {
        "version": METRICS_VERSION,
        "triage_sha256": t_sha,
        "ready": False, "production_ready": False,
        "human_quality_qualified": False,
        "auto_adoption_allowed": False,
        "objective_technical_metrics_only": True,
        "creative_quality_ground_truth_available": False,
        "measured_pair_count": 1,
        "comparisons": [{
            "kind": kind, "ab_receipt_sha256": receipt_sha,
            "decoded_video_ssim": .88 if kind == "cut" else .97,
            "after_minus_before_mean_db": -1.5 if kind == "audio" else 0.0,
        }],
    }
    metrics_path = root / f"{stem}.ab-metrics.json"
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    if tamper:
        receipt.write_bytes(b"corrupt upstream receipt")
    return metrics_path


def test_trusted_human_clips_have_geometry_not_quality_gold(tmp_path):
    store, state = _human(tmp_path)
    root = tmp_path / "render"
    _metric(root, "a", kind="audio")
    _metric(root, "b", kind="cut")
    report = ctx.compile_human_edit_context(
        example_store=store, growth_state=state, preview_root=root,
    )
    human = report["human_alignment_reference"]
    signals = report["generated_ab_technical_signals"]
    assert report["status"] == "LOCAL_HUMAN_EDIT_CONTEXT_STRUCTURAL_ONLY"
    assert human["provenance_quality"] == "TRUSTED_ALIGNMENT_METADATA_ONLY"
    assert human["trusted_clip_count"] == 3
    assert human["trusted_source_streams"] == 3
    assert human["aligned_spans_observed"] == 6
    assert human["median_aligned_span_seconds"] == 30.0
    assert human["positive_source_gaps_observed"] == 3
    assert human["median_positive_source_gap_seconds"] == 20.0
    assert human["median_stream_level_clip_length_seconds"] == 60.0
    assert human["human_labeled_a_b_preferences"] == 0
    assert signals["verified_metric_packs"] == 2
    assert signals["technical_pair_count"] == 2
    assert signals["pair_kind_counts"] == {
        "audio": 1, "cut": 1, "subtitle": 0,
    }
    assert signals["video_ssim_medians"]["cut"] == .88
    assert signals["audio_mean_delta_medians_db"]["audio"] == -1.5
    assert signals["all_signal_metrics_remeasured_this_pass"] is False
    assert report["metric_domains_directly_comparable"] is False
    assert report["source_overlap_with_generated_jobs_verified"] is False
    assert report["ab_preference_accuracy"] is None
    assert report["precision"] is None
    assert report["production_qualification_allowed"] is False
    assert report["auto_adoption_allowed"] is False
    assert report["ready"] is False and report["production_ready"] is False
    encoded = json.dumps(report)
    assert "https://youtu.be" not in encoded
    assert str(tmp_path) not in encoded


def test_corrupted_receipt_is_excluded_without_inventing_a_bad_ab_label(tmp_path):
    store, state = _human(tmp_path)
    root = tmp_path / "render"
    _metric(root, "a")
    _metric(root, "b", tamper=True)
    report = ctx.compile_human_edit_context(
        example_store=store, growth_state=state, preview_root=root,
    )
    signals = report["generated_ab_technical_signals"]
    assert signals["verified_metric_packs"] == 1
    assert signals["stale_or_invalid_metric_packs"] == 1
    assert report["human_editorial_quality_labels_available"] is False


def test_reserved_holdout_source_intersection_blocks_reference(tmp_path):
    store, state = _human(tmp_path, reserved=["stream-0"])
    report = ctx.compile_human_edit_context(
        example_store=store, growth_state=state,
        preview_root=tmp_path / "no-renders",
    )
    human = report["human_alignment_reference"]
    assert report["status"] == "LOCAL_HUMAN_EDIT_CONTEXT_SPLIT_CONFLICT"
    assert human["split_conflict_count"] == 1
    assert human["excluded_holdout_stream"] == 1
    assert human["trusted_clip_count"] == 0
    assert human["trusted_source_streams"] == 0
    assert human["median_aligned_span_seconds"] is None
    assert report["production_qualification_allowed"] is False


def test_uncertified_holdout_state_does_not_claim_verified_isolation(tmp_path):
    store, state = _human(tmp_path, no_state=True)
    report = ctx.compile_human_edit_context(
        example_store=store, growth_state=state,
        preview_root=tmp_path / "none",
    )
    human = report["human_alignment_reference"]
    assert human["growth_state_available"] is False
    assert human["provenance_quality"] == "HOLDOUT_RESERVATIONS_NOT_VERIFIABLE"
    assert report["source_overlap_with_generated_jobs_verified"] is False


def test_missing_corpus_or_signal_inventory_never_claims_quality(tmp_path):
    report = ctx.compile_human_edit_context(
        example_store=tmp_path / "missing.jsonl",
        growth_state=tmp_path / "missing.json",
        preview_root=tmp_path / "no-video",
    )
    assert report["status"] == "LOCAL_HUMAN_EDIT_CONTEXT_NO_TRUSTED_REFERENCES"
    assert report["human_alignment_reference"]["trusted_clip_count"] == 0
    assert report["generated_ab_technical_signals"]["verified_metric_packs"] == 0
    assert report["ready"] is False


def test_pagination_is_stable_and_does_not_discard_later_signal_receipts(tmp_path):
    store, state = _human(tmp_path)
    root = tmp_path / "render"
    for digit in "12345":
        _metric(root, digit, kind="cut")
    first = ctx.compile_human_edit_context(
        example_store=store, growth_state=state,
        preview_root=root, max_jobs=2,
    )
    assert first["generated_ab_technical_signals"]["has_more"] is True
    assert first["generated_ab_technical_signals"]["window_count"] == 2
    cursor = first["generated_ab_technical_signals"]["next_cursor"]
    assert cursor == "renderqueue_" + "2" * 32
    second = ctx.compile_human_edit_context(
        example_store=store, growth_state=state,
        preview_root=root, max_jobs=2, after_job_id=cursor,
    )
    assert second["generated_ab_technical_signals"]["window_count"] == 2
    assert second["generated_ab_technical_signals"]["next_cursor"] == (
        "renderqueue_" + "4" * 32
    )
    third = ctx.compile_human_edit_context(
        example_store=store, growth_state=state, preview_root=root,
        max_jobs=2, after_job_id=second["generated_ab_technical_signals"]["next_cursor"],
    )
    assert third["generated_ab_technical_signals"]["verified_metric_packs"] == 1
    assert third["generated_ab_technical_signals"]["has_more"] is False


@pytest.mark.parametrize("bad", ["z" * 32, "../bad", "", "renderqueue_"+"x"*32])
def test_invalid_page_cursor_fails_closed(tmp_path, bad):
    with pytest.raises(ValueError):
        ctx.compile_human_edit_context(
            example_store=tmp_path / "absent", growth_state=tmp_path / "none",
            preview_root=tmp_path / "no", after_job_id=bad,
        )


def test_duplicate_canonical_human_clips_fail_closed(tmp_path):
    store, state = _human(tmp_path, n=1)
    data = store.read_text(encoding="utf-8")
    store.write_text(data + data, encoding="utf-8")
    with pytest.raises(ValueError, match="DUPLICATE"):
        ctx.compile_human_edit_context(
            example_store=store, growth_state=state,
            preview_root=tmp_path / "none",
        )


def test_cli_defaults_to_read_only_and_can_export_create_only(tmp_path):
    store, state = _human(tmp_path)
    root = tmp_path / "renders"
    _metric(root, "a")
    output = tmp_path / "report.json"
    args = [
        "render-ab-human-context", "--example-store", str(store),
        "--growth-state", str(state), "--preview-root", str(root),
        "--output", str(output),
    ]
    first = CliRunner().invoke(cli.app, args)
    assert first.exit_code == 0, first.stdout
    assert not output.exists()
    result = json.loads(first.stdout)
    assert result["metric_domains_directly_comparable"] is False
    next_run = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert next_run.exit_code == 0, next_run.stdout
    assert json.loads(output.read_text(encoding="utf-8")) == result
    before = output.read_bytes()
    repeated = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert repeated.exit_code == 6
    assert output.read_bytes() == before


def test_untrusted_human_alignment_excluded_instead_of_becoming_label(tmp_path):
    store, state = _human(tmp_path, n=1)
    example = json.loads(store.read_text(encoding="utf-8").splitlines()[0])
    example["alignment_confidence"] = .2
    store.write_text(json.dumps(example) + "\n", encoding="utf-8")
    human = ctx.compile_human_edit_context(
        example_store=store, growth_state=state,
        preview_root=tmp_path / "none",
    )["human_alignment_reference"]
    assert human["trusted_clip_count"] == 0
    assert human["excluded_untrusted"] == 1
    assert human["human_labeled_media_defects"] == 0
