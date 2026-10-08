"""Bounded original/finished visual checks cannot turn a scene into gold."""
import shutil
import subprocess

import pytest

from holoclip.visual_source_crosscheck import (
    VERSION, inspect_visual_only_source_changes,
)
from holoclip.visual_cut_brackets import VERSION as VISUAL_VERSION

PEAK = [6.4, 6.6]


def survey(peaks=None):
    p = [PEAK] if peaks is None else peaks
    return {
        "version": "bounded-output-only-visual-survey-v1",
        "status": "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS",
        "observed_peaks": [{
            "relation": "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
            "time_bracket_seconds": interval,
            "verified_edit_cut": False,
            "source_scene_change_ruled_out": False,
        } for interval in p],
        "verified_edit_cut": False,
        "frame_accurate_cuts_verified": False,
        "reviewed_gold_available": False,
        "qualification_allowed": False,
    }


def evidence(path="source.mkv", *, verified=True, offset=0):
    segments = [{
        "source_video_path": path,
        "source_start": 50., "source_end": 64.,
        "source_measured_duration_seconds": 14.,
    }]
    clocks = [{
        "status": "PARTIAL_SECTION_AUDIO_CLOCK" if verified else "UNKNOWN_SECTION_CLOCK",
        "frame_pts_verified": True,
        "original_video_absolute_pts_verified": False,
        "origin_seconds": 50.,
    }]
    refinement = {
        "points": [
            {"group": 0, "clip_start": 4., "clip_end": 6.,
             "source_start": 54., "source_end": 56.},
            {"group": 0, "clip_start": 7., "clip_end": 9.,
             "source_start": 57.+offset, "source_end": 59.+offset},
        ],
    }
    return segments, clocks, refinement


def test_same_source_change_is_observed_but_not_proven_to_be_natural_edit():
    sections, clocks, refinement = evidence()
    calls = []

    def measure(media, bracket):
        calls.append((str(media), bracket))
        return {"version": VISUAL_VERSION, "status": "VISUAL_JUMP_CANDIDATES",
                "peaks": [{"time_bracket_seconds": bracket}]}

    result = inspect_visual_only_source_changes(
        survey(), sections, clocks, refinement, measure=measure,
    )
    assert result["version"] == VERSION
    assert result["status"] == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS"
    assert result["checked_visual_peaks"] == 1
    assert result["source_change_also_observed"] == 1
    assert result["source_no_isolated_change_observed"] == 0
    assert result["unknown_source_correspondence"] == 0
    assert result["peak_observations"][0]["status"] == (
        "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED"
    )
    assert calls == [("source.mkv", [6.025, 6.975])]
    assert result["verified_edit_cut"] is False
    assert result["source_scene_change_ruled_out"] is False


def test_no_source_jump_is_not_proof_of_editor_causation():
    segments, clocks, refinement = evidence()
    result = inspect_visual_only_source_changes(
        survey(), segments, clocks, refinement,
        measure=lambda *_: {"version": VISUAL_VERSION,
                            "status": "UNKNOWN_NO_ISOLATED_JUMP",
                            "peaks": []},
    )
    assert result["source_no_isolated_change_observed"] == 1
    assert result["peak_observations"][0]["status"] == (
        "SOURCE_NO_ISOLATED_CHANGE_OBSERVED"
    )
    assert result["verified_edit_cut"] is False


@pytest.mark.parametrize("case", ["weak_clock", "offset_jump", "missing_second_anchor"])
def test_uncertain_audio_or_source_clock_never_decodes(case):
    sections, clocks, refinement = evidence()
    if case == "weak_clock":
        clocks[0]["status"] = "UNKNOWN_INCONSISTENT_SOURCE_CLOCK"
    elif case == "offset_jump":
        refinement["points"][1]["source_start"] += 4
        refinement["points"][1]["source_end"] += 4
    else:
        refinement["points"].pop()

    def forbidden(*args):
        raise AssertionError("Ungrounded timestamp cannot initiate source decode")
    report = inspect_visual_only_source_changes(
        survey(), sections, clocks, refinement, measure=forbidden,
    )
    assert report["peak_observations"][0]["status"] == (
        "UNKNOWN_NO_TWO_SIDED_AUDIO_ANCHORS"
    )
    assert report["unknown_source_correspondence"] == 1


def test_bounded_probe_budget_does_not_decode_partial_prefix():
    sections, clocks, refinement = evidence()
    report = inspect_visual_only_source_changes(
        survey([[float(i), float(i)+.1] for i in range(7)]),
        sections, clocks, refinement,
        measure=lambda *_: (_ for _ in ()).throw(AssertionError("decoded")),
    )
    assert report["status"] == "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET"
    assert report["checked_visual_peaks"] == 0
    assert report["unknown_source_correspondence"] == 7


def test_malformed_or_promoted_surveys_fail_closed():
    sections, clocks, refinement = evidence()
    promoted = survey()
    promoted["observed_peaks"][0]["verified_edit_cut"] = True
    with pytest.raises(ValueError):
        inspect_visual_only_source_changes(promoted, sections, clocks, refinement)
    bad = survey()
    bad["observed_peaks"][0]["time_bracket_seconds"] = [float("nan"), 7.]
    with pytest.raises(ValueError):
        inspect_visual_only_source_changes(bad, sections, clocks, refinement)



def test_source_decoder_error_is_unknown_and_does_not_promote():
    sections, clocks, refinement = evidence()
    def failed(*args):
        raise subprocess.CalledProcessError(1, ["ffmpeg"])
    measured = inspect_visual_only_source_changes(
        survey(), sections, clocks, refinement, measure=failed,
    )
    assert measured["status"] == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS"
    assert measured["unknown_source_correspondence"] == 1
    assert measured["peak_observations"][0]["status"] == (
        "UNKNOWN_SOURCE_VISUAL_DECODE"
    )
    assert measured["verified_edit_cut"] is False


def test_missing_output_survey_is_unknown_not_zero_or_gold():
    sections, clocks, refinement = evidence()
    outcome = inspect_visual_only_source_changes(
        {"status": "UNKNOWN_BOUNDED_VISUAL_DECODE"},
        sections, clocks, refinement,
    )
    assert outcome["status"] == "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY"
    assert outcome["source_change_also_observed"] == 0
    assert outcome["verified_edit_cut"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_actual_bounded_original_video_natural_transition_vs_unchanged(tmp_path):
    black_white = tmp_path / "source_transition.mp4"
    still = tmp_path / "continuous_black.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=256x144:r=8:d=6.5",
        "-f", "lavfi", "-i", "color=c=white:s=256x144:r=8:d=7.5",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(black_white),
    ], check=True, timeout=90)
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=256x144:r=8:d=14",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(still),
    ], check=True, timeout=90)
    for path, expected in [
        (black_white, "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED"),
        (still, "SOURCE_NO_ISOLATED_CHANGE_OBSERVED"),
    ]:
        sections, clocks, refinement = evidence(str(path))
        result = inspect_visual_only_source_changes(
            survey(), sections, clocks, refinement,
        )
        assert result["status"] == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS"
        assert result["peak_observations"][0]["status"] == expected
        assert result["verified_edit_cut"] is False
