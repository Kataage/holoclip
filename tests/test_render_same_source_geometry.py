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
        "transcript_root": tmp_path / "transcripts",
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


def test_exact_same_selected_intervals_can_have_nonchronological_playback():
    human = [(10, 12), (20, 22), (30, 32)]
    generated = [(30, 32), (10, 12), (20, 22)]
    score = geo._geometry(generated, human)
    assert score["selected_source_jaccard"] == 1
    diag = score["selection_difference_diagnostics"]
    assert diag["generated_source_time_backtracks"] == 1
    assert diag["human_alignment_source_time_backtracks"] == 0
    assert diag["generated_only"]["window_count"] == 0
    assert diag["human_only"]["window_count"] == 0
    assert diag["source_backtrack_is_not_an_editing_error"] is True


def test_exclusive_source_windows_are_true_subtraction_not_overlap_heuristics():
    score = geo._geometry(
        [(10, 12), (30, 32), (33, 34)],
        [(11, 13), (20, 22), (30, 31)],
    )
    diag = score["selection_difference_diagnostics"]
    assert diag["generated_only"]["window_count"] == 3
    assert diag["generated_only"]["top_windows_by_duration"] == [
        {"source_start_seconds": 31, "source_end_seconds": 32,
         "duration_seconds": 1},
        {"source_start_seconds": 33, "source_end_seconds": 34,
         "duration_seconds": 1},
        {"source_start_seconds": 10, "source_end_seconds": 11,
         "duration_seconds": 1},
    ] or diag["generated_only"]["top_windows_by_duration"] == [
        {"source_start_seconds": 10, "source_end_seconds": 11,
         "duration_seconds": 1},
        {"source_start_seconds": 31, "source_end_seconds": 32,
         "duration_seconds": 1},
        {"source_start_seconds": 33, "source_end_seconds": 34,
         "duration_seconds": 1},
    ]
    assert diag["human_only"]["top_windows_by_duration"][0] == {
        "source_start_seconds": 20,
        "source_end_seconds": 22,
        "duration_seconds": 2,
    }
    assert diag["difference_windows_are_not_edit_instructions"] is True


def test_overlapping_repeated_segments_do_not_invent_extra_source_gaps():
    score = geo._geometry(
        [(0, 10), (2, 3), (11, 12)],
        [(0, 10), (11, 12)],
    )
    assert score["generated_observed_source_gaps"] == 1
    assert score["human_observed_source_gaps"] == 1
    assert score["nearest_human_gap_start_distance_seconds"] == 0
    assert score["selection_difference_diagnostics"]["generated_only"]["window_count"] == 0


def test_top_windows_bounded_and_deterministic_for_many_disagreements():
    generated = [(i * 10.0, i * 10.0 + 1) for i in range(50)]
    human = [(i * 10.0 + 2, i * 10.0 + 3) for i in range(50)]
    score = geo._geometry(generated, human)
    diag = score["selection_difference_diagnostics"]
    assert diag["generated_only"]["window_count"] == 50
    assert len(diag["generated_only"]["top_windows_by_duration"]) == 3
    assert [x["source_start_seconds"] for x in
            diag["generated_only"]["top_windows_by_duration"]] == [0, 10, 20]
    assert score["shared_source_seconds"] == 0


def test_same_source_cli_exposes_diagnostics_without_qualification(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    report = geo.compare_same_source_edit_geometry(**params)
    geometry = report["results"][0]["matches"][0]["geometry"]
    assert geometry["selection_difference_diagnostics"]["generated_only"]["window_count"] >= 0
    assert report["selection_difference_is_not_a_human_quality_label"] is True
    assert report["ready"] is False
    assert report["auto_adoption_allowed"] is False



def test_inspection_queue_prioritizes_distinct_source_windows_per_job(tmp_path):
    params, receipt, _, _ = _build(tmp_path, clips=[_human_example()])
    report = geo.compare_same_source_edit_geometry(**params)
    queue = report["inspection_queue"]
    assert queue["scope"] == "CURRENT_JOB_PAGE_ONLY"
    assert queue["source_reservation_metadata_verified"] is True
    assert queue["matched_jobs_with_observable_differences"] == 1
    assert queue["listed_jobs"] == 1
    assert queue["one_reference_example_per_job"] is True
    entry = queue["results"][0]
    assert entry["job_id"] == receipt["job_id"]
    assert entry["evaluation_split"] == "TRAINING_REFERENCE_IN_SAMPLE"
    assert entry["action"] == "INSPECT_ONLY"
    assert entry["automatic_edit_recommendation"] is None
    assert entry["observed_nonshared_source_seconds"] == 3
    assert "OBSERVED_SOURCE_SELECTION_DIFFERENCE" in entry["inspection_reasons"]
    assert entry["generated_only_top_source_windows"]
    assert entry["human_only_top_source_windows"]
    assert queue["discrepancy_magnitude_is_not_editorial_quality"] is True
    assert report["ready"] is False


def test_identical_source_ranges_with_different_order_require_inspection():
    geom = geo._geometry(
        [(30, 32), (10, 12), (20, 22)],
        [(10, 12), (20, 22), (30, 32)],
    )
    assert geom["selected_source_jaccard"] == 1
    assert geom["selection_difference_diagnostics"][
        "exact_source_interval_order_differs"
    ] is True
    assert geom["generated_only_source_seconds"] == 0
    item = {
        "job_id": "renderqueue_" + "a" * 32,
        "canonical_source_stream_sha256": "s" * 64,
        "matches": [{
            "human_clip_identity_sha256": "h" * 64,
            "human_alignment_confidence": .95,
            "geometry": geom,
        }],
    }
    result = geo._inspection_queue([item], verified_reservations=True)
    assert result["listed_jobs"] == 1
    assert result["results"][0]["observed_nonshared_source_seconds"] == 0
    assert "EXACT_SPAN_PLAYBACK_ORDER_DIFFERENCE" in result["results"][0][
        "inspection_reasons"
    ]


def test_nonidentical_source_spans_cannot_claim_exact_order_difference():
    geom = geo._geometry([(10, 12)], [(10.001, 12)])
    assert geom["selection_difference_diagnostics"][
        "exact_source_interval_order_differs"
    ] is None
    equal = geo._geometry([(10, 12), (20, 22)], [(10, 12), (20, 22)])
    assert equal["selection_difference_diagnostics"][
        "exact_source_interval_order_differs"
    ] is False


def test_inspection_queue_is_bounded_sorted_and_deduplicates_jobs():
    def mk_match(seconds, identity):
        geom = geo._geometry([(0, seconds)], [(30, 31)])
        return {
            "human_clip_identity_sha256": identity * 64,
            "human_alignment_confidence": .95,
            "geometry": geom,
        }
    jobs = [
        {
            "job_id": "renderqueue_" + f"{i:032x}",
            "canonical_source_stream_sha256": "s" * 64,
            "matches": [mk_match(i + 1, "b"), mk_match(1, "a")],
        }
        for i in range(14)
    ]
    review = geo._inspection_queue(jobs, verified_reservations=True)
    assert review["matched_jobs_with_observable_differences"] == 14
    assert review["listed_jobs"] == 10
    assert review["omitted_jobs_due_to_limit"] == 4
    assert len({r["job_id"] for r in review["results"]}) == 10
    assert review["results"][0]["observed_nonshared_source_seconds"] == 15
    assert review["results"][0]["human_clip_identity_sha256"] == "b" * 64
    assert review["results"][-1]["observed_nonshared_source_seconds"] == 6


def test_empty_or_matching_page_has_no_inspection_items():
    report = geo._inspection_queue([], verified_reservations=False)
    assert report["source_reservation_metadata_verified"] is False
    assert report["listed_jobs"] == 0
    assert report["ready"] is False
    geom = geo._geometry([(10, 12)], [(10, 12)])
    row = {
        "job_id": "renderqueue_" + "a" * 32,
        "canonical_source_stream_sha256": "s" * 64,
        "matches": [{
            "human_clip_identity_sha256": "h" * 64,
            "human_alignment_confidence": .95,
            "geometry": geom,
        }],
    }
    assert geo._inspection_queue(
        [row], verified_reservations=True
    )["results"] == []


def test_holdout_conflict_clears_derived_inspection_queue(tmp_path):
    params, *_ = _build(
        tmp_path, clips=[_human_example()], reserved=[VIDEO],
    )
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["status"] == "LOCAL_SAME_SOURCE_TRAIN_HOLDOUT_CONFLICT"
    assert report["results"] == []
    assert report["inspection_queue"]["results"] == []
    assert report["inspection_queue"]["listed_jobs"] == 0


def test_inspection_queue_saved_without_overwriting_or_modifying_media(tmp_path):
    params, _, _, media = _build(tmp_path, clips=[_human_example()])
    before = media.read_bytes()
    output = tmp_path / "review.json"
    saved = geo.export_same_source_edit_geometry(
        **params, output=output, execute=True
    )
    assert saved["inspection_queue"]["listed_jobs"] == 1
    assert json.loads(output.read_text(encoding="utf-8")) == saved
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE"):
        geo.export_same_source_edit_geometry(
            **params, output=output, execute=True
        )
    assert media.read_bytes() == before


def test_context_uses_local_timed_asr_for_same_source_only(tmp_path):
    params, receipt, _, _ = _build(tmp_path, clips=[_human_example()])
    local = params["transcript_root"] / "sources"
    local.mkdir(parents=True)
    path = local / f"{VIDEO}.json"
    path.write_text(json.dumps({
        "video_id": VIDEO, "segments": [
            {"start": 7.0, "end": 9.0, "text": "始まり"},
            {"start": 12.0, "end": 13.0, "text": "途中"},
            {"start": 20.0, "end": 21.0, "text": "直前"},
            {"start": 23.0, "end": 23.7, "text": "オチ"},
            {"start": 24.0, "end": 25.0, "text": "反応"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    report = geo.compare_same_source_edit_geometry(**params)
    entries = report["inspection_queue"]["results"]
    assert len(entries) == 1 and entries[0]["job_id"] == receipt["job_id"]
    context = entries[0]["transcript_context"]
    assert context["status"] == "TRANSCRIPT_CONTEXT_AVAILABLE"
    assert context["source_identity_basis"] == (
        "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED"
    )
    assert context["transcript_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(context["windows"]) == 2
    assert all(not w["human_edit_intent_inferred"] for w in context["windows"])
    assert "オチ" in json.dumps(context, ensure_ascii=False)
    assert context["local_asr_audio_independently_verified"] is False
    assert report["ready"] is False


def test_absent_transcript_expressly_unavailable(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    result = geo.compare_same_source_edit_geometry(**params)
    assert result["inspection_queue"]["results"][0]["transcript_context"] == {
        "status": "TRANSCRIPT_UNAVAILABLE"
    }


def test_filename_only_source_identity_remains_unverified(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    root = params["transcript_root"]
    root.mkdir()
    (root / f"{VIDEO}.json").write_text(json.dumps({
        "segments": [{"start": 11, "end": 12, "text": "発話"}]
    }, ensure_ascii=False), encoding="utf-8")
    context = geo.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"]
    assert context["source_identity_basis"] == "FILENAME_ONLY_IDENTITY_UNVERIFIED"


def test_mismatched_and_duplicate_transcripts_are_never_quoted(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    root = params["transcript_root"]
    root.mkdir()
    p = root / f"{VIDEO}.json"
    p.write_text(json.dumps({
        "video_id": "wrong-video",
        "segments": [{"start": 11, "end": 12, "text": "not allowed"}]
    }), encoding="utf-8")
    assert geo.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"] == {
            "status": "TRANSCRIPT_UNTRUSTED"
    }
    p.write_text(json.dumps({"video_id": VIDEO, "segments": []}), encoding="utf-8")
    (root / "sources").mkdir()
    (root / "sources" / f"{VIDEO}.json").write_text(
        json.dumps({"video_id": VIDEO, "segments": []}), encoding="utf-8"
    )
    assert geo.compare_same_source_edit_geometry(**params)[
        "inspection_queue"]["results"][0]["transcript_context"] == {
            "status": "TRANSCRIPT_UNTRUSTED"
    }


def test_source_speech_context_is_bounded_and_rejects_control_text():
    from holoclip.models import TranscriptSegment
    from holoclip.render_same_source_transcript_context import _utterances

    segments = [
        TranscriptSegment(start=0, end=1, text="A" * 161),
        TranscriptSegment(start=1, end=2, text="unsafe\u202e"),
    ] + [
        TranscriptSegment(start=float(i), end=float(i) + .5, text="発話")
        for i in range(2, 10)
    ]
    texts = _utterances(segments, 0, 10)
    assert len(texts) == 3
    assert all(t["unverified_asr_text"] == "発話" for t in texts)


def test_holdout_conflict_prevents_transcript_context_from_leaking(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()], reserved=[VIDEO])
    root = params["transcript_root"]
    root.mkdir()
    (root / f"{VIDEO}.json").write_text(json.dumps({
        "video_id": VIDEO,
        "segments": [{"start": 10, "end": 12, "text": "confidential-test"}],
    }), encoding="utf-8")
    report = geo.compare_same_source_edit_geometry(**params)
    assert report["inspection_queue"]["results"] == []
    assert "confidential-test" not in json.dumps(report)


def test_cli_local_asr_context_readonly_create_only(tmp_path):
    params, *_ = _build(tmp_path, clips=[_human_example()])
    root = params["transcript_root"]
    root.mkdir()
    (root / f"{VIDEO}.json").write_text(json.dumps({
        "video_id": VIDEO,
        "segments": [{"start": 10, "end": 12, "text": "日本語発話"}],
    }, ensure_ascii=False), encoding="utf-8")
    target = tmp_path / "inspection.json"
    args = [
        "render-same-source-geometry", "--queue", str(params["queue"]),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--preview-root", str(params["preview_root"]),
        "--transcript-root", str(root), "--output", str(target),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert not target.exists()
    document = json.loads(preview.stdout)
    assert document["inspection_queue"]["results"][0]["transcript_context"][
        "status"] == "TRANSCRIPT_CONTEXT_AVAILABLE"
    saved = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(target.read_text(encoding="utf-8")) == document
    again = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert again.exit_code == 6
