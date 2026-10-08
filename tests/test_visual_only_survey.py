"""Independent finished-video change survey: bounded, source-unverified, no gold."""
from pathlib import Path
import shutil
import subprocess

import pytest

from holoclip.visual_only_survey import (
    MAX_WINDOWS, VERSION, survey_finished_visual_changes,
)


def test_independent_visual_peak_without_acoustic_candidates():
    measured_windows = []

    def synthetic(media, interval):
        measured_windows.append(interval)
        if interval[0] < 6 < interval[1]:
            return {
                "status": "VISUAL_JUMP_CANDIDATES",
                "peaks": [{
                    "time_bracket_seconds": [5.95, 6.05],
                    "excess_change": .9,
                }],
            }
        return {"status": "UNKNOWN_NO_ISOLATED_JUMP", "peaks": []}

    result = survey_finished_visual_changes(Path("unused.mp4"), 12., [],
                                            measure=synthetic)
    assert result["version"] == VERSION
    assert result["status"] == "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
    assert len(measured_windows) == MAX_WINDOWS
    assert result["observed_visual_peaks"] == 1  # deduplicated overlap
    assert result["without_acoustic_candidate"] == 1
    assert result["sampled_coverage_seconds"] == 12.
    assert result["unsampled_duration_seconds"] == 0.
    assert result["observed_peaks"][0]["relation"] == (
        "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME"
    )
    assert result["verified_edit_cut"] is False
    assert result["frame_accurate_cuts_verified"] is False


def test_same_visual_change_with_acoustic_candidate_remains_reference_free():
    def synthetic(media, interval):
        return {
            "status": "VISUAL_JUMP_CANDIDATES",
            "peaks": [{"time_bracket_seconds": [5.95, 6.05]}],
        } if interval[0] < 6 < interval[1] else {
            "status": "UNKNOWN_NO_ISOLATED_JUMP", "peaks": [],
        }

    result = survey_finished_visual_changes(
        Path("not_needed"), 12.,
        [{"status": "CUT_INTERVAL_CANDIDATE", "interval": [5.5, 6.5]}],
        measure=synthetic,
    )
    assert result["observed_visual_peaks"] == 1
    assert result["without_acoustic_candidate"] == 0
    assert result["observed_peaks"][0]["relation"] == "OVERLAPS_ACOUSTIC_CANDIDATE"
    assert result["reviewed_gold_available"] is False


def test_long_clip_has_real_unsampled_duration_and_fixed_budget():
    observed = []

    def no_jump(media, bracket):
        observed.append(bracket)
        return {"status": "UNKNOWN_NO_ISOLATED_JUMP", "peaks": []}

    result = survey_finished_visual_changes(
        Path("not_needed"), 1800., [], measure=no_jump,
    )
    assert len(observed) == MAX_WINDOWS
    assert result["sampled_coverage_seconds"] <= 36.
    assert result["unsampled_duration_seconds"] >= 1764.
    assert result["observed_visual_peaks"] == 0
    assert result["qualification_allowed"] is False


def test_decode_error_is_unknown_not_clean_video_or_empty_gold():
    def failed(media, bracket):
        raise RuntimeError("invalid FFmpeg media")

    result = survey_finished_visual_changes(
        Path("unreadable"), 45., [], measure=failed,
    )
    assert result["status"] == "UNKNOWN_BOUNDED_VISUAL_DECODE"
    assert result["observed_visual_peaks"] is None
    assert result["without_acoustic_candidate"] is None


def test_excessive_visual_peaks_fail_closed():
    def fake(media, bracket):
        start = bracket[0]
        return {"status": "VISUAL_JUMP_CANDIDATES", "peaks": [
            {"time_bracket_seconds": [start + i*.25, start + i*.25 + .1]}
            for i in range(10)
            if start + i*.25 + .1 < bracket[1]
        ]}

    result = survey_finished_visual_changes(
        Path("unused"), 15., [], measure=fake,
    )
    assert result["status"] == "UNKNOWN_VISUAL_PEAK_BUDGET"
    assert result["observed_visual_peaks"] is None


@pytest.mark.parametrize("bad", [
    [{"status": "CUT_INTERVAL_CANDIDATE", "interval": [-1, 3]}],
    [{"status": "CUT_INTERVAL_CANDIDATE", "interval": [float("nan"), 3]}],
    [{"status": "CUT_INTERVAL_CANDIDATE", "interval": [9, 6]}],
])
def test_invalid_acoustic_candidates_rejected_not_reinterpreted(bad):
    with pytest.raises(ValueError):
        survey_finished_visual_changes(Path("no_file"), 12., bad, measure=lambda *_: None)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_actual_ffmpeg_black_to_white_cut_without_acoustic_signal(tmp_path):
    video = tmp_path / "visual_edit.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=256x144:r=8:d=6",
        "-f", "lavfi", "-i", "color=c=white:s=256x144:r=8:d=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(video),
    ], check=True, timeout=90)
    report = survey_finished_visual_changes(video, 12., [])
    assert report["status"] == "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
    assert report["without_acoustic_candidate"] >= 1
    assert any(5.5 <= (x["time_bracket_seconds"][0] +
                       x["time_bracket_seconds"][1]) / 2 <= 6.5
               for x in report["observed_peaks"])
    assert report["verified_edit_cut"] is False
