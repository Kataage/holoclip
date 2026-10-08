"""Independent image-domain temporal validation on real measured PNG/JPEG frames."""
from types import SimpleNamespace

import cv2
import numpy as np

from holoclip.source_candidate_visual import (
    _NO_GOLD, compare_sparse_visual_sequences,
)
from holoclip.source_visual_pixel_check import (
    VERSION, confirm_pixel_temporal_pairs,
)
from holoclip.video_measurement import checksum_file


def _frames(tmp_path, *, kind, seed, count=4, still=False,
            jpeg=False, overlay=False):
    frames = []
    for index in range(count):
        rng = np.random.default_rng(seed if still else seed+index)
        pixels = rng.integers(0, 255, (260, 416), dtype=np.uint8)
        if overlay:
            cv2.rectangle(pixels, (10, 10), (38, 25), 150, -1)
        suffix = ".jpg" if jpeg else ".png"
        file = tmp_path/f"{kind}-{index}{suffix}"
        options = [cv2.IMWRITE_JPEG_QUALITY, 95] if jpeg else []
        assert cv2.imwrite(str(file), pixels, options)
        frames.append(SimpleNamespace(
            id=f"{kind}-{index}",
            locator=str(file), checksum=checksum_file(file),
            pts_seconds=float(index*2),
        ))
    return frames


def _pairs(tmp_path, *, still=False, source_seed=1100,
           finished_seed=1100, jpeg=True, overlay=True):
    source = _frames(
        tmp_path, kind="original", seed=source_seed, still=still,
    )
    finished = _frames(
        tmp_path, kind="clip", seed=finished_seed,
        still=still, jpeg=jpeg, overlay=overlay,
    )
    return list(zip(source, finished))


def test_true_sampled_video_png_to_jpeg_overlay_matches_without_orb_reuse(tmp_path):
    evidence = confirm_pixel_temporal_pairs(_pairs(tmp_path))
    assert evidence["version"] == VERSION
    assert evidence["status"] == "PIXEL_TEMPORAL_SEQUENCE_CORROBORATED_NOT_GOLD"
    assert evidence["checked_pairs"] == 4
    assert all(d <= 12 for d in evidence["matched_dct_distances_bits"])
    assert evidence["min_temporal_diversity_bits"] >= 5
    assert evidence["distinct_off_diagonal_margin_bits"] >= 3
    assert evidence["source_identity_verified"] is False


def test_original_other_video_false_geometry_positive_is_rejected(tmp_path):
    evidence = confirm_pixel_temporal_pairs(_pairs(
        tmp_path, source_seed=2110, finished_seed=8400,
        jpeg=False, overlay=False,
    ))
    assert evidence["status"] in {
        "UNKNOWN_PERCEPTUAL_FRAME_MISMATCH",
        "UNKNOWN_PERCEPTUAL_TEMPORAL_AMBIGUITY",
    }
    assert evidence["source_identity_verified"] is False


def test_stationary_stream_cannot_confirm_three_orb_appearances(tmp_path):
    evidence = confirm_pixel_temporal_pairs(_pairs(
        tmp_path, still=True, jpeg=False, overlay=False,
    ))
    assert evidence["status"] == "UNKNOWN_STATIC_OR_REPEATED_VIDEO"
    assert evidence["min_temporal_diversity_bits"] == 0


def test_changed_frame_hash_blocks_even_matching_image_bytes(tmp_path):
    pairs = _pairs(tmp_path)
    pairs[0][1].checksum = "0"*64
    evidence = confirm_pixel_temporal_pairs(pairs)
    assert evidence["status"] == "UNKNOWN_IMAGE_CHECKSUM_OR_DECODE"
    assert evidence["source_identity_verified"] is False


def test_pixel_verifier_rejects_more_than_six_frames_before_image_reads():
    f = SimpleNamespace(
        locator="/path/that/must/not/be/read", checksum="x"*64,
        pts_seconds=0.,
    )
    result = confirm_pixel_temporal_pairs([(f, f)] * 7)
    assert result["status"] == "UNKNOWN_INDEPENDENT_PAIR_BUDGET"
    assert result["checked_pairs"] == 0


def test_mock_orb_match_to_different_real_images_does_not_record_positive(tmp_path):
    src = SimpleNamespace(frames=_frames(
        tmp_path, kind="src", seed=1010, count=4,
    ))
    out = SimpleNamespace(frames=_frames(
        tmp_path, kind="dst", seed=1414, count=4,
    ))

    def fake_geometry(source, target):
        if source.rsplit("-", 1)[-1].split(".")[0] == (
            target.rsplit("-", 1)[-1].split(".")[0]
        ):
            return {"score": .85}
        return None

    result = compare_sparse_visual_sequences(
        out, [src],
        extract=lambda locator: locator,
        compare=fake_geometry,
    )
    assert result["status"] == "UNKNOWN_INDEPENDENT_PIXEL_NOT_CORROBORATED"
    assert result["matched_frames"] == 0
    assert result["pixel_independent_check"]["status"] != (
        "PIXEL_TEMPORAL_SEQUENCE_CORROBORATED_NOT_GOLD"
    )
    for key in _NO_GOLD:
        assert result[key] is False


def test_real_orb_and_independent_pixel_checker_both_required(tmp_path):
    pairs = _pairs(tmp_path, jpeg=False, overlay=False)
    original = SimpleNamespace(frames=[src for src, _ in pairs])
    finished = SimpleNamespace(frames=[dst for _, dst in pairs])
    result = compare_sparse_visual_sequences(finished, [original])
    assert result["status"] == "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
    assert result["pixel_independent_check"]["status"] == (
        "PIXEL_TEMPORAL_SEQUENCE_CORROBORATED_NOT_GOLD"
    )
    assert result["matched_frames"] >= 3
    assert result["source_identity_verified"] is False
