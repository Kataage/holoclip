"""Self-supervised controls from actually encoded, decoded and SHA-pinned video."""
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from holoclip.source_visual_real_controls import (
    VERSION, measure_source_video_controls,
)
from holoclip.video_measurement import checksum_file


def _window(tmp_path: Path, *, still=False, n=4):
    video = tmp_path / "measured.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"mp4v"),
        1., (416, 260),
    )
    if not writer.isOpened():
        pytest.skip("MP4 VideoWriter is not available")
    try:
        for index in range(n):
            pixels = np.random.default_rng(
                5500 if still else 5500 + index
            ).integers(0, 255, (260, 416, 3), dtype=np.uint8)
            writer.write(pixels)
    finally:
        writer.release()
    reader = cv2.VideoCapture(str(video))
    assert reader.isOpened()
    frames = []
    try:
        for index in range(n):
            ok, pixels = reader.read()
            assert ok
            output = tmp_path / f"actual-{index}.png"
            assert cv2.imwrite(str(output), pixels)
            frames.append(SimpleNamespace(
                id=f"f-{index}", locator=str(output),
                checksum=checksum_file(output),
                pts_seconds=float(index),
            ))
    finally:
        reader.release()
    return SimpleNamespace(
        video_path=str(video), media_sha256=checksum_file(video),
        frames=frames,
    )


def test_real_encoded_decoded_source_frames_yield_known_positive_and_decoy(tmp_path):
    window = _window(tmp_path)
    result = measure_source_video_controls(
        window, root=tmp_path / "controls",
    )
    assert result["version"] == VERSION
    assert result["status"] == "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED"
    assert result["reference_type"] == "CONSTRUCTED_FROM_MEASURED_SOURCE_FRAMES"
    assert result["source_window_sha256"] == window.media_sha256
    assert result["observed_control_pairs"] == 4
    assert result["positive_control_result"] == (
        "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
    )
    assert result["reversed_time_control_result"] != (
        "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
    )
    assert result["constructed_positive_detected"] is True
    assert result["constructed_reversal_rejected"] is True
    assert result["controls_are_real_human_editor_labels"] is False
    assert result["source_video_is_official_hololive_verified"] is False
    assert result["editor_cut_precision"] is None
    assert result["editor_cut_recall"] is None
    assert result["real_source_match_false_positive_rate"] is None
    assert result["qualification_allowed"] is False


def test_still_measured_video_is_unknown_not_failed_real_precision(tmp_path):
    window = _window(tmp_path, still=True)
    # Codec inter-frame artifacts can change PNG SHA even when a repeated
    # still is visually identical. Pin an actual decoded measured still at
    # every sampled timestamp so this is deterministically the same image.
    for row in window.frames[1:]:
        row.locator = window.frames[0].locator
        row.checksum = window.frames[0].checksum
    result = measure_source_video_controls(
        window, root=tmp_path / "controls",
    )
    assert result["status"] == "UNKNOWN_REPEATED_SOURCE_IMAGES"
    assert result["constructed_positive_detected"] is None
    assert result["real_source_match_false_positive_rate"] is None


def test_corrupt_measured_video_sha_blocks_control_before_reading_frames(tmp_path):
    window = _window(tmp_path)
    window.media_sha256 = "0"*64
    result = measure_source_video_controls(window, root=tmp_path/"controls")
    assert result["status"] == "UNKNOWN_SOURCE_VIDEO_HASH_OR_BUDGET"
    assert result["observed_control_pairs"] == 0
    assert not (tmp_path/"controls").exists()


def test_corrupt_measured_frame_sha_blocks_real_control(tmp_path):
    window = _window(tmp_path)
    window.frames[1].checksum = "f"*64
    result = measure_source_video_controls(window, root=tmp_path/"controls")
    assert result["status"] == "UNKNOWN_MEASURED_FRAME_HASH_OR_BUDGET"
    assert result["observed_control_pairs"] == 0
    assert not (tmp_path/"controls").exists()


def test_deleted_video_or_insufficient_frames_does_not_claim_measurement(tmp_path):
    window = _window(tmp_path, n=2)
    result = measure_source_video_controls(window, root=tmp_path/"controls")
    assert result["status"] == "UNKNOWN_INSUFFICIENT_MEASURED_SOURCE_FRAMES"
    assert result["qualification_allowed"] is False
    Path(window.video_path).unlink()
    result = measure_source_video_controls(window, root=tmp_path/"controls")
    assert result["status"] == "UNKNOWN_SOURCE_VIDEO_HASH_OR_BUDGET"


def test_synthetic_matcher_cannot_force_source_gold(tmp_path):
    window = _window(tmp_path)
    def claims_gold(*_args, **_kwargs):
        return {"status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"}
    result = measure_source_video_controls(
        window, root=tmp_path/"controls", matcher=claims_gold,
    )
    assert result["status"] == "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED"
    assert result["constructed_positive_detected"] is True
    assert result["constructed_reversal_rejected"] is False
    assert result["source_identity_verified"] is False
    assert result["reviewed_gold_available"] is False
    assert result["editor_cut_precision"] is None
