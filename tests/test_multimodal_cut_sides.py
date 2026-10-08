"""Bounded source/finished candidate cut sides: image, policy and integrity tests."""
import json
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from test_editorial_profile import trusted_example
from holoclip.holdout_cut_side_pipeline import inspect_reserved_cut_sides
from holoclip.multimodal_cut_sides import (
    HEIGHT, WIDTH, compare_transition_sides, decode_short_frames,
)
from test_acoustic_cut_brackets import _run as run_cut
from test_source_frame_links import fixture as frame_fixture, run as run_links
from test_visual_edit_evidence import run_visual, texture


def moving_frames(seed):
    frames = []
    for i in range(12):
        data = cv2.resize(texture(seed + i * 13), (WIDTH, HEIGHT))
        frames.append(data)
    return np.asarray(frames)


def supported_transition():
    sections = [{
        "source_video_path": "bounded-original.mkv",
        "source_start": 37., "source_end": 67.,
        "source_measured_duration_seconds": 30.,
    }]
    clocks = [{
        "status": "PARTIAL_SECTION_AUDIO_CLOCK", "origin_seconds": 40.,
        "frame_pts_verified": True, "original_video_absolute_pts_verified": False,
    }]
    refinement = {
        "points": [
            {"group": 0, "clip_start": 4., "clip_end": 6.,
             "source_start": 44., "source_end": 46.},
            {"group": 0, "clip_start": 8., "clip_end": 10.,
             "source_start": 60., "source_end": 62.},
        ],
    }
    transition = {
        "status": "CUT_INTERVAL_CANDIDATE", "interval": [6., 8.],
        "left_group": 0, "right_group": 0,
        "offset_change_seconds": 12.,
    }
    jump = {"relation": "CORROBORATING_OUTPUT_CHANGE"}
    return sections, clocks, refinement, transition, jump


def test_actual_image_correspondence_both_sides_supports_candidate_not_gold():
    left, right = moving_frames(23), moving_frames(199)
    sections, clocks, refinement, transition, jump = supported_transition()

    def decode(path, center, duration):
        if "bounded-original" in str(path):
            return (left if center < 15 else right), list(range(12))
        return (left if center < 7 else right), list(range(12))

    result = compare_transition_sides(
        Path("finished.mp4"), sections, clocks, refinement, transition,
        jump, clip_duration=25., decode=decode,
    )
    assert result["status"] == "MULTIMODAL_EDIT_JUMP_CANDIDATE"
    assert len(result["side_matches"]) == 2
    assert all(item["status"] == "PARTIAL_SAMPLED_SIDE_FRAME_MATCH"
               for item in result["side_matches"])
    assert result["skipped_source_time_candidate_seconds"] == 12.
    assert result["verified_edit_cut"] is False
    assert result["original_video_absolute_pts_verified"] is False
    assert result["output_source_map_verified"] is False
    assert result["unknowns"]


def test_identical_visual_content_on_both_sides_is_unknown():
    images = moving_frames(37)
    sections, clocks, refinement, transition, jump = supported_transition()
    result = compare_transition_sides(
        Path("finished.mp4"), sections, clocks, refinement, transition,
        jump, clip_duration=25.,
        decode=lambda path, center, duration: (images, list(range(12))),
    )
    assert result["status"] == "UNKNOWN_ONE_OR_BOTH_SIDES_UNVERIFIED"
    assert result["verified_edit_cut"] is False


def test_missing_clocks_or_output_jumps_or_source_reordering_remain_unknown():
    sections, clocks, refinement, transition, jump = supported_transition()
    def forbidden(*args):
        raise AssertionError("Unknown evidence must not decode full media")
    assert compare_transition_sides(
        Path("x"), sections, clocks, refinement, transition,
        {}, clip_duration=25., decode=forbidden,
    )["status"] == "UNKNOWN_OUTPUT_VISUAL_JUMP"
    clocks[0]["status"] = "UNKNOWN_NONUNIQUE_SOURCE_AUDIO"
    assert compare_transition_sides(
        Path("x"), sections, clocks, refinement, transition,
        jump, clip_duration=25., decode=forbidden,
    )["status"] == "UNKNOWN_SECTION_CLOCK"
    clocks[0]["status"] = "PARTIAL_SECTION_AUDIO_CLOCK"
    refinement["points"][1]["source_start"] = 42.
    refinement["points"][1]["source_end"] = 44.
    assert compare_transition_sides(
        Path("x"), sections, clocks, refinement, transition,
        jump, clip_duration=25., decode=forbidden,
    )["status"] == "UNKNOWN_REORDERED_SOURCE"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_bounded_real_ffmpeg_decode_and_seek_guard(tmp_path):
    video = tmp_path / "moving.mkv"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=8:duration=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True, timeout=90)
    frames, times = decode_short_frames(video, center=2.5, duration=5.)
    assert 8 <= len(frames) <= 16
    assert frames.shape[1:] == (HEIGHT, WIDTH)
    assert len(frames) == len(times)
    assert 1.5 <= times[0] <= 2.5
    with pytest.raises(ValueError):
        decode_short_frames(video, center=0.2, duration=5.)
    with pytest.raises(ValueError):
        decode_short_frames(video, center=30., duration=31.)


def _pipeline_fixture(tmp_path):
    provider, store, state, prepared, audio_root = frame_fixture(tmp_path)
    assert run_links(provider, store, state, prepared, audio_root)["counts"] == {
        "MEASURED_UNREVIEWED_FRAME_LINKS": 1
    }
    assert run_visual(provider, store, state, prepared, audio_root)["counts"] == {
        "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY": 1
    }
    def fake_refiner(*args, **kwargs):
        _, _, refinement, transition, _ = supported_transition()
        return {"version": "bounded-acoustic-cut-brackets-v2",
                "status": "AUDIO_SUPPORTED_INTERVALS",
                "points": refinement["points"], "transitions": [transition]}
    def fake_jump(*args):
        return {"status": "VISUAL_JUMP_CANDIDATES", "peaks": [
            {"time_bracket_seconds": [6.9, 7.0]}
        ]}
    assert run_cut(
        provider, store, state, prepared, audio_root,
        fingerprint=lambda *args: np.zeros((200, 12), dtype=np.float32),
        refine=fake_refiner, measure_jump=fake_jump,
    )["counts"] == {"MEASURED_UNREVIEWED_CUT_BRACKETS": 1}
    return provider, store, state, prepared, audio_root


def _run(provider, store, state, prepared, audio_root, **kwargs):
    return inspect_reserved_cut_sides(
        provider, store=store, state_path=state, prepared_root=prepared,
        audio_root=audio_root, pairs_root=prepared / "source_pairs",
        clocks_root=prepared / "source_clocks", frame_links_root=prepared / "frame_links",
        cuts_root=prepared / "cut_brackets", output=prepared / "cut_sides",
        policy_rules=kwargs.pop("policy_rules", []), max_clips=2, **kwargs,
    )



def test_legacy_acoustic_refinement_blocked_before_provider_call(tmp_path):
    _, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    path = prepared / "cut_brackets" / "holdout-cut-brackets.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = next(iter(payload["records"].values()))
    record["refinement"]["version"] = "bounded-acoustic-cut-brackets-v1"
    path.write_text(json.dumps(payload), encoding="utf-8")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Stale v1 measurement must be rejected before probing")

    report = _run(NoNetwork(), store, state, prepared, audio_root)
    assert report["counts"] == {"BLOCKED_UPSTREAM_PROVENANCE": 1}



def test_integrated_cut_side_checkpoint_and_resumption(tmp_path):
    provider, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    calls = []
    def measured(*args, **kwargs):
        calls.append(1)
        return {"status": "MULTIMODAL_EDIT_JUMP_CANDIDATE",
                "verified_edit_cut": False, "side_matches": [],
                "original_video_absolute_pts_verified": False}

    result = _run(provider, store, state, prepared, audio_root, compare=measured)
    assert result["counts"] == {"MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE": 1}
    report = json.loads(
        (prepared / "cut_sides" / "holdout-cut-side-evidence.json").read_text(encoding="utf-8")
    )
    for key in (
        "qualification_allowed", "benchmark_held_out",
        "reviewed_gold_available", "event_split_verified",
        "frame_accurate_cuts_verified", "original_video_absolute_pts_verified",
        "dense_output_source_map_verified",
    ):
        assert report[key] is False
    row = next(iter(report["records"].values()))
    assert row["transitions"][0]["candidate"]["status"] == "MULTIMODAL_EDIT_JUMP_CANDIDATE"
    assert row["transitions"][0]["editorial_gold"] is False
    assert row["source_visual_method_version"] == (
        "bounded-output-source-visual-crosscheck-v1"
    )
    assert row["independent_visual_source_checks"]["verified_edit_cut"] is False
    assert row["independent_visual_source_checks"]["reviewed_gold_available"] is False
    assert row["visual_only_frame_search_version"] == (
        "bounded-visual-only-frame-search-v1"
    )
    assert row["visual_only_frame_search"]["qualification_allowed"] is False
    assert len(calls) == 1
    def no_decode(*args, **kwargs):
        raise AssertionError("Unmodified checked frame evidence should be reused")
    assert _run(
        provider, store, state, prepared, audio_root, compare=no_decode,
    )["counts"] == result["counts"]


def test_corruption_and_new_train_collision_block_before_metadata(tmp_path):
    _, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    pair = next(iter(json.loads(
        (prepared / "source_pairs" / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )["records"].values()))["pairs"][0]
    Path(pair["source_video_path"]).write_bytes(b"invalid-bounded-source")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Do not probe for corrupt/unreserved media")

    assert _run(NoNetwork(), store, state, prepared, audio_root)["counts"] == {
        "BLOCKED_MEASUREMENT_INTEGRITY": 1
    }
    upsert_human_clip_examples(
        store, [trusted_example("new-learning-conflict", stream_id="stream-evaluation")],
    )
    assert _run(NoNetwork(), store, state, prepared, audio_root)["counts"] == {
        "BLOCKED_SPLIT_CONFLICT": 1
    }


def test_rights_restriction_after_prior_measurement_quarantines(tmp_path):
    provider, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    denied = ClipPolicyRule(
        id="later-revoked-range", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=45, end=48,
        reason="range revoked after prior visual evidence",
    )
    result = _run(provider, store, state, prepared, audio_root,
                  policy_rules=[denied], compare=lambda *args, **kwargs: None)
    assert result["counts"] == {"QUARANTINED_POLICY": 1}


def test_unverified_side_clock_stays_unknown_instead_of_decoding(tmp_path):
    provider, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    path = prepared / "source_clocks" / "holdout-source-clock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    clock = next(iter(data["records"].values()))["section_clocks"][0]
    clock["status"] = "UNKNOWN_INCONSISTENT_SOURCE_CLOCK"
    clock["origin_seconds"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    def no_video(*args, **kwargs):
        return {"status": "UNKNOWN_SECTION_CLOCK", "verified_edit_cut": False}

    result = _run(provider, store, state, prepared, audio_root, compare=no_video)
    assert result["counts"] == {"MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE": 1}
    output = json.loads(
        (prepared / "cut_sides" / "holdout-cut-side-evidence.json").read_text(encoding="utf-8")
    )
    evidence = next(iter(output["records"].values()))["transitions"][0]["candidate"]
    assert evidence["status"] == "UNKNOWN_SECTION_CLOCK"
