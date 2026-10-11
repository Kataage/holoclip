"""Non-destructive, SHA-pinned edit review ledgers and cut-localized candidates."""
from __future__ import annotations

import hashlib
import json

import pytest

from holoclip.domain import ClipTimeline, TimelineSegment
from holoclip import render_quality_review as review


def timeline():
    return ClipTimeline(
        id="two-cut-review", segments=[
            TimelineSegment(
                id="one", source_asset_id="asset-one",
                source_start=10, source_end=12, output_order=0,
            ),
            TimelineSegment(
                id="two", source_asset_id="asset-two",
                source_start=30, source_end=33, output_order=1,
            ),
        ], estimated_duration=5,
    )


def video(tmp_path):
    path = tmp_path / "renderqueue_" + "a" * 32
    return path


def _case(tmp_path):
    mp4 = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    mp4.write_bytes(b"already QA passed original MP4")
    sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    return mp4, sha


def test_cut_seam_review_mentions_both_segments_and_never_auto_edits():
    items = review.candidates_for_review(timeline(), [
        {"kind": "blackout", "start_seconds": 1.4, "end_seconds": 2.4},
        {"kind": "freeze", "start_seconds": 3.2, "end_seconds": None},
        {"kind": "fast_subtitle", "start_seconds": 0.5, "end_seconds": 1.0,
         "cue_number": 7},
        {"kind": "audio_peak", "start_seconds": 0, "end_seconds": 5},
    ])
    assert items[0]["segment_ids"] == ["one", "two"]
    assert items[0]["position"] == "cut_boundary"
    assert items[1]["segment_ids"] == ["two"]
    assert items[2]["cue_number"] == 7
    assert items[3]["recommendation"] == "LISTEN_TO_INTENTIONAL_PEAK_VS_CLIPPING"
    assert all(item["auto_repair_allowed"] is False for item in items)
    assert all(item["human_editorial_qa_completed"] is False for item in items)


@pytest.mark.parametrize("bad", [
    {"kind": "unknown", "start_seconds": 0.1, "end_seconds": 1.1},
    {"kind": "blackout", "start_seconds": -1, "end_seconds": 1},
    {"kind": "blackout", "start_seconds": float("nan"), "end_seconds": None},
    {"kind": "blackout", "start_seconds": 4, "end_seconds": 2},
    {"kind": "fast_subtitle", "start_seconds": 1, "end_seconds": 2},
])
def test_bad_observations_are_never_committed(bad):
    with pytest.raises(ValueError, match="LOCAL_EDIT_REVIEW"):
        review.candidates_for_review(timeline(), [bad])


def test_ledger_written_only_once_with_sha_and_no_media_mutation(tmp_path):
    original, sha = _case(tmp_path)
    unchanged = original.read_bytes()
    observation = [{
        "kind": "blackout", "start_seconds": 1.4, "end_seconds": 2.4,
    }]
    kwargs = {
        "mp4_output": original, "video_sha256": sha,
        "final_sha256": "b" * 64,
        "diagnostic_status": "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
        "timeline": timeline(), "observed": observation,
    }
    result = review.store_review_ledger(**kwargs)
    assert result["status"] == "LOCAL_EDIT_REVIEW_LEDGER_CREATED_UNVERIFIED_EDITORIAL"
    assert result["candidate_count"] == 1
    ledger_path = original.with_suffix(".review.json")
    assert ledger_path.is_file()
    data = ledger_path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == result["sha256"]
    doc = json.loads(data)
    assert doc["candidates"][0]["segment_ids"] == ["one", "two"]
    assert doc["source_of_observations"] == "OFFLINE_FFMPEG_AND_HASH_VERIFIED_SRT"
    assert doc["ready"] is False and doc["production_ready"] is False
    assert doc["publication_performed"] is False
    assert "already QA passed" not in str(doc)
    assert unchanged == original.read_bytes()
    second = review.store_review_ledger(**kwargs)
    assert second["status"] == "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE"
    assert ledger_path.read_bytes() == data


def test_ledger_failed_signal_is_not_claimed(tmp_path):
    original, sha = _case(tmp_path)
    report = review.store_review_ledger(
        mp4_output=original, video_sha256=sha, final_sha256="c" * 64,
        diagnostic_status="LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE",
        timeline=timeline(), observed=[],
    )
    assert report["status"] == "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE"
    assert not original.with_suffix(".review.json").exists()


def test_changed_source_blocks_ledger_precommit(tmp_path):
    original, sha = _case(tmp_path)
    original.write_bytes(b"changed bytes")
    report = review.store_review_ledger(
        mp4_output=original, video_sha256=sha, final_sha256="a" * 64,
        diagnostic_status="LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
        timeline=timeline(), observed=[],
    )
    assert report["status"] == "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE"
    assert not original.with_suffix(".review.json").exists()


def test_unrelated_existing_ledger_remains_untouched(tmp_path):
    original, sha = _case(tmp_path)
    target = original.with_suffix(".review.json")
    target.write_bytes(b"trusted human notes")
    outcome = review.store_review_ledger(
        mp4_output=original, video_sha256=sha, final_sha256="a" * 64,
        diagnostic_status="LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
        timeline=timeline(), observed=[],
    )
    assert outcome["status"] == "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE"
    assert target.read_bytes() == b"trusted human notes"
