"""Same canonical source geometry is training context, never editorial gold."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_same_source_geometry as geo
from holoclip import render_ready_queue as ready
from holoclip.corpus_growth import CorpusGrowthState
from holoclip.domain import ClipTimeline
from holoclip.examples import AlignmentSegmentExample, upsert_human_clip_examples
from holoclip.example_quality import LearningQuality, evaluate_human_example_learning_quality
from test_editorial_profile import trusted_example
from test_render_ready_queue import VIDEO, _case


def _human_example(*, clip="human-1", stream=VIDEO, url=None, ranges=None):
    ranges = ranges or [(0, 2, 10, 12), (2, 5, 21, 24)]
    ex = trusted_example(clip, stream_id=stream, start=10)
    ex.source_url = url if url is not None else f"https://youtu.be/{stream}"
    ex.source_start = min(r[2] for r in ranges)
    ex.source_end = max(r[3] for r in ranges)
    ex.clip_duration_seconds = max(r[1] for r in ranges)
    ex.alignment_segments = [
        AlignmentSegmentExample(
            clip_start=a, clip_end=b, source_start=c, source_end=d,
            confidence=.95, matched_anchors=3,
        ) for a, b, c, d in ranges
    ]
    ex.alignment_matched_anchors = len(ranges) * 3
    ex.alignment_total_anchors = len(ranges) * 3
    assert evaluate_human_example_learning_quality(ex).decision is LearningQuality.TRUSTED
    return ex


def _build(tmp_path, *, clips=None, reserved=()):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    root.mkdir(parents=True)
    media = root / f"{receipt['job_id']}.mp4"
    media.write_bytes(b"offline pre-existing technically QA-passed preview")
    conn = ready._db(queue, write=True)
    assert conn is not None
    conn.execute(
        "INSERT INTO render_jobs VALUES (?,?,?,?,?,?,?,?,?)",
        (
            receipt["job_id"], "QA_PASSED", "", "NONE", str(media),
            hashlib.sha256(media.read_bytes()).hexdigest(), "plan-test",
            6.0, 123.0,
        ),
    )
    conn.commit()
    conn.close()
    corpus = tmp_path / "human.jsonl"
    if clips is not None:
        upsert_human_clip_examples(corpus, clips)
    state = tmp_path / "growth.json"
    state.write_text(
        CorpusGrowthState(holdout_stream_ids=list(reserved)).model_dump_json(),
        encoding="utf-8",
    )
    kwargs = {
        "queue": queue, "example_store": corpus,
        "growth_state": state, "preview_root": root,
    }
    return kwargs, receipt, timeline, media


def test_same_verified_source_reports_overlap_and_gap_distance_not_human_quality(tmp_path):
    example = _human_example()
    args, receipt, timeline, media = _build(tmp_path, clips=[example])
    report = geo.compare_same_source_edit_geometry(**args)
    assert report["status"] == "LOCAL_SAME_SOURCE_UNQUALIFIED_GEOMETRY"
    assert report["same_source_compared_jobs"] == 1
    assert report["same_source_pairs"] == 1
    assert report["unmatched_source_jobs"] == 0
    assert report["same_source_identity_method"] == "canonical_repository_youtube_id"
    assert report["baseline_mp4_independently_rehashed"] is False
    assert report["human_examples_from_training_not_independent_holdout"] is True
    assert report["human_alignment_is_ground_truth_for_exact_cuts"] is False
    assert report["measured_edit_geometry_not_editorial_quality"] is True
    assert report["heldout_qualification_allowed"] is False
    assert report["ready"] is False and report["production_ready"] is False
    assert report["precision"] is None and report["recall"] is None
    result = report["results"][0]
    assert result["job_id"] == receipt["job_id"]
    assert result["human_match_count"] == 1
    assert result["qualified_editorial_benchmark"] is False
    assert result["rendered_media_redecoded"] is False
    row = result["matches"][0]
    assert row["evaluation_split"] == "TRAINING_REFERENCE_IN_SAMPLE"
    assert row["human_example_id"] == "human-1"
    values = row["geometry"]
    assert values["generated_unique_source_seconds"] == 6
    assert values["human_aligned_unique_source_seconds"] == 5
    assert values["shared_source_seconds"] == 4
    assert values["generated_only_source_seconds"] == 2
    assert values["human_only_source_seconds"] == 1
    assert values["generated_selection_overlap_fraction"] == pytest.approx(4 / 6, abs=1e-6)
    assert values["human_aligned_coverage_fraction"] == .8
    assert values["selected_source_jaccard"] == pytest.approx(4 / 7, abs=1e-6)
    assert values["generated_observed_source_gaps"] == 1
    assert values["human_observed_source_gaps"] == 1
    assert values["nearest_human_gap_start_distance_seconds"] == 1
    assert values["creative_quality_label"] is None
    assert VIDEO not in json.dumps(report)
    assert "youtu.be" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.parametrize("stream,url", [
    ("badSTRM_02", "https://youtu.be/badSTRM_02"),
    (VIDEO, "https://youtu.be/badSTRM_02"),
    (VIDEO, "https://fake.youtube.com/watch?v=abcDEFgh012"),
])
def test_different_or_spoofed_source_never_matches(tmp_path, stream, url):
    human = _human_example(stream=stream, url=url)
    params, *_ = _build(tmp_path, clips=[human])
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["same_source_compared_jobs"] == 0
    assert report["same_source_pairs"] == 0
    assert report["unmatched_source_jobs"] == 1
    assert report["results"] == []


def test_reserved_holdout_colliding_with_training_disables_all_results(tmp_path):
    example = _human_example()
    params, *_ = _build(tmp_path, clips=[example], reserved=[VIDEO])
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["status"] == "LOCAL_SAME_SOURCE_TRAIN_HOLDOUT_CONFLICT"
    assert report["corpus"]["holdout_training_conflicts"] == 1
    assert report["holdout_source_blocked_jobs"] == 1
    assert report["results"] == []
    assert report["same_source_pairs"] == 0
    assert report["heldout_qualification_allowed"] is False


def test_unverified_growth_state_never_claims_holdout_provenance(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    params["growth_state"].unlink()
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["holdout_reservation_metadata_available"] is False
    assert report["growth_state_sha256"] is None
    assert report["results"][0]["matches"][0]["evaluation_split"] == (
        "TRAINING_REFERENCE_IN_SAMPLE"
    )
    assert report["heldout_qualification_allowed"] is False


def test_untrusted_alignment_discarded_instead_of_treated_as_negative(tmp_path):
    item = _human_example()
    item.alignment_confidence = .4
    args, *_ = _build(tmp_path, clips=[item])
    result = geo.compare_same_source_edit_geometry(**args)
    assert result["corpus"]["excluded_alignment_untrusted"] == 1
    assert result["unmatched_source_jobs"] == 1
    assert result["results"] == []


def test_two_source_render_not_implicitly_paired_with_one_human_stream(tmp_path):
    params, receipt, timeline, _ = _build(tmp_path, clips=[_human_example()])
    changed = ClipTimeline(
        id=timeline.id, segments=[
            timeline.segments[0],
            timeline.segments[1].model_copy(update={"source_asset_id": "foreign"}),
        ], estimated_duration=6,
    )
    with sqlite3.connect(params["queue"]) as conn:
        conn.execute(
            "UPDATE queue_jobs SET timeline_json=? WHERE job_id=?",
            (changed.model_dump_json(), receipt["job_id"]),
        )
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["multi_source_jobs_excluded"] == 1
    assert report["same_source_pairs"] == 0


def test_previously_qa_passed_video_missing_is_stale_not_qualified(tmp_path):
    params, _, _, media = _build(tmp_path, clips=[_human_example()])
    media.unlink()
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["ineligible_or_stale_jobs"] == 1
    assert report["same_source_pairs"] == 0


def test_empty_or_missing_queue_is_readonly(tmp_path):
    path = tmp_path / "absent.sqlite3"
    result = geo.compare_same_source_edit_geometry(
        queue=path, example_store=tmp_path / "none.jsonl",
        growth_state=tmp_path / "none.json", preview_root=tmp_path / "none",
    )
    assert result["status"] == "LOCAL_SAME_SOURCE_NO_COMPLETED_RENDER_JOBS"
    assert result["same_source_pairs"] == 0
    assert not path.exists()


def test_duplicate_trusted_canonical_clip_fails_closed(tmp_path):
    example = _human_example()
    params, *_ = _build(tmp_path, clips=[example])
    corpus = params["example_store"]
    line = corpus.read_text(encoding="utf-8")
    corpus.write_text(line + line, encoding="utf-8")
    with pytest.raises(ValueError, match="DUPLICATE"):
        geo.compare_same_source_edit_geometry(**params)


@pytest.mark.parametrize("cursor", [
    "../source", "", "renderqueue_" + "x"*32, "renderqueue_" + "a"*31,
])
def test_invalid_cursors_rejected(tmp_path, cursor):
    with pytest.raises(ValueError):
        geo.compare_same_source_edit_geometry(
            queue=tmp_path / "none.db",
            example_store=tmp_path / "none",
            growth_state=tmp_path / "none.json",
            preview_root=tmp_path / "none",
            after_job_id=cursor,
        )


def test_exact_source_geometry_does_not_use_clip_source_duration_as_precision():
    result = geo._geometry([(10, 13), (20, 23)], [(100, 110)])
    assert result["shared_source_seconds"] == 0
    assert result["generated_selection_overlap_fraction"] == 0
    assert result["human_aligned_coverage_fraction"] == 0
    assert result["selected_source_jaccard"] == 0
    assert result["nearest_human_gap_start_distance_seconds"] is None
    assert result["creative_quality_label"] is None


def test_intervals_union_prevents_double_count_of_repeated_source_fragments():
    result = geo._geometry(
        [(10, 15), (13, 18), (20, 22)],
        [(10, 13), (15, 20)],
    )
    assert result["generated_unique_source_seconds"] == 10
    assert result["human_aligned_unique_source_seconds"] == 8
    assert result["shared_source_seconds"] == 6


def test_cli_readonly_then_create_only_with_no_writes_to_master(tmp_path):
    params, receipt, timeline, media = _build(tmp_path, clips=[_human_example()])
    output = tmp_path / "geometry.json"
    cliargs = [
        "render-same-source-geometry",
        "--queue", str(params["queue"]),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--preview-root", str(params["preview_root"]),
        "--output", str(output),
    ]
    before = media.read_bytes()
    first = CliRunner().invoke(cli.app, cliargs)
    assert first.exit_code == 0, first.stdout
    assert not output.exists()
    doc = json.loads(first.stdout)
    assert doc["same_source_pairs"] == 1
    saved = CliRunner().invoke(cli.app, [*cliargs, "--execute"])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(output.read_text(encoding="utf-8")) == doc
    repeat = CliRunner().invoke(cli.app, [*cliargs, "--execute"])
    assert repeat.exit_code == 6
    assert media.read_bytes() == before
    assert output.is_file()
