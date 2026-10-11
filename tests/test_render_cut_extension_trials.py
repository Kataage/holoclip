"""Real, nonexecuting cut-extension trials backed by local source ASR clock."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_trials as cut
from holoclip.models import TranscriptSegment
from holoclip.transcript import save_transcript
from test_render_same_source_geometry import _build, _human_example
from test_render_ready_queue import VIDEO


def _case(tmp_path, *, with_asr=True, holdout=False, manifest=True):
    transcript_root = tmp_path / "transcripts"
    path = transcript_root / "sources" / f"{VIDEO}.json"
    human = _human_example()
    if manifest:
        human.source_transcript_path = str(path)
    params, receipt, timeline, media = _build(
        tmp_path, clips=[human], reserved=[VIDEO] if holdout else [],
    )
    if with_asr:
        save_transcript([
            TranscriptSegment(start=10.25, end=11.7, text="既存の前半の発話"),
            TranscriptSegment(start=12.1, end=12.8, text="既存にない前半"),
            TranscriptSegment(start=21.2, end=22.2, text="後半のもともとの発話"),
            TranscriptSegment(start=23.10, end=23.80, text="切れていた続きの反応"),
        ], path)
    return params, receipt, timeline, media, path


def _prepare(params):
    return cut.preview_cut_extensions(**params)


def _cli(params, *extra):
    return [
        "render-cut-extension-trials",
        "--queue", str(params["queue"]),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--preview-root", str(params["preview_root"]),
        "--transcript-root", str(params["transcript_root"]),
        *extra,
    ]


def test_asr_recovery_proposes_exact_concrete_source_cut_extension(tmp_path):
    params, receipt, timeline, media, path = _case(tmp_path)
    before = media.read_bytes()
    transcript_before = path.read_bytes()
    report = _prepare(params)
    assert report["status"] == "LOCAL_CUT_EXTENSION_TRIALS_PROPOSED_UNQUALIFIED"
    assert report["proposed_count"] >= 1
    match = next(item for item in report["proposals"]
                 if item["trial_action"] == "EXTEND_END"
                 and item["source_cut_index"] == 1)
    assert match["job_id"] == receipt["job_id"]
    assert match["baseline_source_intervals"] == [
        {"source_start_seconds": 10.0, "source_end_seconds": 13.0},
        {"source_start_seconds": 20.0, "source_end_seconds": 23.0},
    ]
    assert match["trial_source_intervals"] == [
        {"source_start_seconds": 10.0, "source_end_seconds": 13.0},
        {"source_start_seconds": 20.0, "source_end_seconds": 24.0},
    ]
    assert match["additional_playback_seconds"] == 1.0
    assert match["newly_selected_unique_source_seconds"] == 1.0
    assert match["asr_source_clock_seconds_gained"] >= .5
    assert match["asr_clock_evidence"][0]["source_clock_seconds_gained"] >= .5
    assert match["asr_clock_evidence"][0]["baseline_source_clock_selection"] == (
        "SOURCE_CLOCK_NOT_SELECTED"
    )
    assert match["asr_clock_evidence"][0]["trial_source_clock_selection"] == (
        "SOURCE_CLOCK_FULLY_SELECTED"
    )
    assert match["source_time_window_not_verified_narrative_role"] is True
    assert report["human_reference_is_training_in_sample_not_editorial_gold"] is True
    assert report["source_time_asr_not_audible_speech_or_context_truth"] is True
    assert report["trial_cuts_not_validated_against_source_media_duration"] is True
    assert report["editing_improvement_measured"] is False
    assert report["quality_precision"] is None
    assert report["remote_requests_performed"] == 0
    assert report["auto_adoption_allowed"] is False
    assert report["publication_performed"] is False
    assert report["ready"] is False and report["production_ready"] is False
    redacted = json.dumps(report, ensure_ascii=False)
    assert VIDEO not in redacted
    assert "切れていた続きの反応" not in redacted
    assert str(tmp_path) not in redacted
    assert media.read_bytes() == before
    assert path.read_bytes() == transcript_before
    assert len(timeline.segments) == 2


def test_invalid_or_missing_asr_never_yields_fake_extensions(tmp_path):
    params, *_ = _case(tmp_path, with_asr=False)
    result = _prepare(params)
    assert result["status"] == "LOCAL_CUT_EXTENSION_NO_GROUNDED_TRIALS"
    assert result["proposed_count"] == 0
    other = tmp_path / "other"
    other.mkdir()
    params, *_ = _case(other, manifest=False)
    assert _prepare(params)["proposed_count"] == 0


def test_wrong_embedded_source_metadata_blocks_extension(tmp_path):
    params, _, _, _, path = _case(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["video_id"] = "NotCorrect1"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert _prepare(params)["proposed_count"] == 0


def test_holdout_collision_blocks_all_proposals(tmp_path):
    params, *_ = _case(tmp_path, holdout=True)
    result = _prepare(params)
    assert result["status"] == "LOCAL_CUT_EXTENSION_GATES_BLOCKED"
    assert result["proposals"] == []
    assert result["ready"] is False


def test_missing_holdout_manifest_blocks_all_proposals(tmp_path):
    params, *_ = _case(tmp_path)
    params["growth_state"].unlink()
    result = _prepare(params)
    assert result["status"] == "LOCAL_CUT_EXTENSION_GATES_BLOCKED"
    assert result["proposed_count"] == 0


@pytest.mark.parametrize("cuts,human,expected", [
    ([(10, 13), (20, 23)], (23, 24), (1, "EXTEND_END")),
    ([(10, 13), (20, 23)], (19, 20), (1, "EXTEND_START")),
    ([(10, 13), (20, 23)], (0, 3), None),
    ([(10, 13), (20, 23)], (24, 35), None),
    ([(10, 13), (20, 23)], (25, 27), (1, "EXTEND_END")),
])
def test_adjacent_extensions_respect_source_gap_and_eight_second_cap(
    cuts, human, expected,
):
    found = cut._nearby_extensions(cuts, human)
    assert ((found[0][0], found[0][1]) if found else None) == expected


def test_overlap_with_other_cut_rejected_even_if_extension_is_short():
    # Time-adjacent input selection can overlap a different cut in output
    # order. The algorithm must not add duplicate source time silently.
    found = cut._nearby_extensions(
        [(23.5, 24.5), (20, 23)], (23, 23.6)
    )
    assert not any(i == 1 and side == "EXTEND_END" for i, side, *_ in found)


def test_asr_gained_not_counted_when_already_in_baseline():
    result = cut._asr_gained(
        [(10, 13)], [(10, 14)], [
            {"start_seconds": 10.5, "end_seconds": 11,
             "unverified_asr_text": "already included"},
        ]
    )
    assert result == []


def test_partial_asr_coverage_is_reported_as_unverified_source_time():
    evidence = cut._asr_gained(
        [(10, 13)], [(10, 14)], [
            {"start_seconds": 12.8, "end_seconds": 14.5,
             "unverified_asr_text": "straddling the boundary"},
        ],
    )
    assert len(evidence) == 1
    assert evidence[0]["baseline_source_clock_selection"] == (
        "SOURCE_CLOCK_PARTIALLY_SELECTED"
    )
    assert evidence[0]["trial_source_clock_selection"] == (
        "SOURCE_CLOCK_PARTIALLY_SELECTED"
    )
    assert evidence[0]["source_clock_seconds_gained"] == 1


def test_injected_non_dictionary_report_or_unverified_queue_fail_closed():
    assert cut.propose_cut_extensions(None)["status"] == (
        "LOCAL_CUT_EXTENSION_GATES_BLOCKED"
    )
    assert cut.propose_cut_extensions({"inspection_queue": {"results": []}})[
        "status"] == "LOCAL_CUT_EXTENSION_GATES_BLOCKED"


def test_proposals_derived_from_existing_source_selection_are_read_only(tmp_path, monkeypatch):
    params, _, _, media, _ = _case(tmp_path)
    before = media.read_bytes()
    class DenyLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("No network model permitted")
    monkeypatch.setattr(cli, "MeshLLMClient", DenyLLM)
    runner = CliRunner()
    target = tmp_path / "proposal.json"
    dry = runner.invoke(cli.app, _cli(params))
    assert dry.exit_code == 0, dry.stdout
    assert not target.exists()
    doc = json.loads(dry.stdout)
    assert doc["proposed_count"] > 0
    written = runner.invoke(cli.app, _cli(params, "--output", str(target)))
    assert written.exit_code == 0, written.stdout
    assert json.loads(written.stdout) == doc
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    same = runner.invoke(cli.app, _cli(params, "--output", str(target)))
    assert same.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == doc
    assert media.read_bytes() == before
