"""Visual jump detector: synthetic frames, bounded FFmpeg decoder, cut-stage integration."""
from pathlib import Path

import numpy as np
import pytest

from holoclip.visual_cut_brackets import (
    HEIGHT, WIDTH, VERSION, analyze_visual_jump, extract_bounded_frames,
    measure_visual_cut_bracket,
)
from test_acoustic_cut_brackets import _run, _setup


def _samples(*, jump=False, continuous=False):
    rng = np.random.default_rng(124)
    first = rng.integers(0, 255, (HEIGHT, WIDTH), dtype=np.uint8)
    second = rng.integers(0, 255, (HEIGHT, WIDTH), dtype=np.uint8)
    if continuous:
        return np.stack([first.copy() for _ in range(64)])
    frames = [first.copy() for _ in range(64)]
    if jump:
        frames[32:] = [second.copy() for _ in range(32)]
    return np.stack(frames)


def _times(n=64):
    return [i / 8 for i in range(n)]


def test_isolated_visual_change_brackets_a_cut_not_exact_frame():
    observed = analyze_visual_jump(_samples(jump=True), _times(), [3.5, 4.5])
    assert observed["version"] == VERSION
    assert observed["status"] == "VISUAL_JUMP_CANDIDATES"
    assert len(observed["peaks"]) == 1
    assert observed["peaks"][0]["time_bracket_seconds"] == [3.875, 4.0]
    assert observed["peaks"][0]["changed_pixel_fraction"] >= .46
    assert "unverified_frame_pts" in observed["peaks"][0]["time_basis"]
    assert observed["unknowns"]


def test_stable_scene_and_natural_motion_are_not_cut_proof():
    stable = analyze_visual_jump(_samples(continuous=True), _times(), [3, 5])
    assert stable["status"] == "UNKNOWN_NO_ISOLATED_JUMP"
    assert stable["peaks"] == []
    # Uniformly high frame-to-frame changes are not isolated edit evidence.
    moving = np.random.default_rng(77).integers(
        0, 255, (64, HEIGHT, WIDTH), dtype=np.uint8,
    )
    motion = analyze_visual_jump(moving, _times(), [3, 5])
    assert motion["status"] == "UNKNOWN_NO_ISOLATED_JUMP"


def test_only_changes_within_audio_bracket_are_reported():
    sampled = analyze_visual_jump(_samples(jump=True), _times(), [1, 2])
    assert sampled["status"] == "UNKNOWN_NO_ISOLATED_JUMP"


def test_bounded_decoder_invariants_and_unknown_wide_audio():
    with pytest.raises(ValueError):
        extract_bounded_frames(Path("does-not-exist"), 2, 20)
    with pytest.raises(ValueError):
        extract_bounded_frames(Path("does-not-exist"), 0, 10, fps=96)
    unknown = measure_visual_cut_bracket(Path("does-not-exist"), [1, 25])
    assert unknown["status"] == "UNKNOWN_BRACKET_TOO_WIDE"
    assert unknown["peaks"] == []


def test_visual_frame_budget_and_clock_reject_corrupt_evidence():
    with pytest.raises(ValueError):
        analyze_visual_jump(_samples()[:2], [2, 2], [1, 3])
    with pytest.raises(ValueError):
        analyze_visual_jump(_samples(), _times(63), [3, 4])
    with pytest.raises(ValueError):
        analyze_visual_jump(_samples(), _times(), [20, 30])


def test_pipeline_correlates_output_jump_without_promoting_gold(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    observed = []

    def fake_refiner(clip, source, ranges, *, max_points):
        return {
            "status": "AUDIO_SUPPORTED_INTERVALS",
            "transitions": [{
                "status": "CUT_INTERVAL_CANDIDATE", "interval": [9., 11.],
                "offset_change_seconds": 27.0,
            }],
            "points": [],
        }

    def fake_jump(path, bracket):
        observed.append((path, bracket))
        return {"version": VERSION, "status": "VISUAL_JUMP_CANDIDATES",
                "peaks": [{"time_bracket_seconds": [9.75, 9.875]}],
                "unknowns": ["Source natural transition unverified"]}

    result = _run(
        provider, store, state, prepared, audio_root,
        fingerprint=lambda media, root: np.zeros((120, 12), dtype=np.float32),
        refine=fake_refiner, measure_jump=fake_jump,
    )
    assert result["counts"] == {"MEASURED_UNREVIEWED_CUT_BRACKETS": 1}
    assert len(observed) == 1
    assert observed[0][1] == [9., 11.]
    import json
    path = prepared / "cut_brackets" / "holdout-cut-brackets.json"
    record = next(iter(json.loads(path.read_text(encoding="utf-8"))["records"].values()))
    evidence = record["output_visual_cut_evidence"][0]
    assert evidence["relation"] == "CORROBORATING_OUTPUT_CHANGE"
    assert evidence["verified_edit_cut"] is False
    assert evidence["source_visual_transition_ruled_out"] is False
    assert evidence["native_frame_pts_verified"] is False
    assert record["source_absolute_seek_verified"] is False
    assert record["visual_cut_boundary_confirmed"] is False


def test_pipeline_keeps_audio_cut_unresolved_without_visual_jump(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)

    def fake_refiner(clip, source, ranges, *, max_points):
        return {"status": "AUDIO_SUPPORTED_INTERVALS", "points": [],
                "transitions": [{"status": "CUT_INTERVAL_CANDIDATE",
                                 "interval": [9., 11.], "offset_change_seconds": 21.}]}

    result = _run(
        provider, store, state, prepared, audio_root,
        fingerprint=lambda media, root: np.zeros((120, 12), dtype=np.float32),
        refine=fake_refiner,
        measure_jump=lambda *args: {"status": "UNKNOWN_NO_ISOLATED_JUMP",
                                    "peaks": [], "unknowns": ["Unresolved"]},
    )
    assert result["counts"] == {"MEASURED_UNREVIEWED_CUT_BRACKETS": 1}
    import json
    report = json.loads(
        (prepared / "cut_brackets" / "holdout-cut-brackets.json").read_text(encoding="utf-8")
    )
    evidence = next(iter(report["records"].values()))["output_visual_cut_evidence"][0]
    assert evidence["relation"] == "UNRESOLVED_VISUAL_EVIDENCE"
    assert evidence["visual_result"]["peaks"] == []


def test_pipeline_never_truncates_unbounded_visual_candidate_work(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)

    def excessive_refinement(clip, source, ranges, *, max_points):
        return {"status": "AUDIO_SUPPORTED_INTERVALS", "points": [],
                "transitions": [{"status": "CUT_INTERVAL_CANDIDATE",
                                 "interval": [i, i + 1], "offset_change_seconds": 14.}
                                for i in range(10)]}

    def must_not_decode(*args, **kwargs):
        raise AssertionError("Budget exhaustion should block visual decoding")

    result = _run(
        provider, store, state, prepared, audio_root,
        fingerprint=lambda media, root: np.zeros((120, 12), dtype=np.float32),
        refine=excessive_refinement, measure_jump=must_not_decode,
    )
    assert result["counts"] == {"MEASURED_UNREVIEWED_CUT_BRACKETS": 1}
    import json
    report = json.loads(
        (prepared / "cut_brackets" / "holdout-cut-brackets.json").read_text(encoding="utf-8")
    )
    evidence = next(iter(report["records"].values()))["output_visual_cut_evidence"][0]
    assert evidence["status"] == "UNKNOWN_VISUAL_BRACKET_BUDGET"
    assert evidence["unresolved_candidates"] == 10
