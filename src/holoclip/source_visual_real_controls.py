"""Self-supervised controls using already measured bounded real source-video frames.

A positive control is a locally JPEG-recompressed version of saved source
frames. A negative control reverses that same sequence while preserving
ascending clock labels. These are constructed counterfactuals, *not*
human-reviewed source matches, negatives from independent streams, or a
measurement of real editor precision/recall.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from holoclip.video_measurement import checksum_file

VERSION = "measured-public-video-self-controls-v1"
MAX_CONTROL_FRAMES = 4
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_WINDOW_BYTES = 128 * 1024 * 1024

_NO_GOLD = {
    "reviewed_gold_available": False,
    "source_identity_verified": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_source_match_false_positive_rate": None,
    "qualification_allowed": False,
    "used_in_learning_or_holdout": False,
    "source_native_absolute_pts_verified": False,
}


def _base():
    return {
        "version": VERSION,
        "status": "UNKNOWN_NO_VALID_MEASURED_CONTROL",
        "reference_type": "CONSTRUCTED_FROM_MEASURED_SOURCE_FRAMES",
        "controls_are_real_human_editor_labels": False,
        "source_video_is_official_hololive_verified": False,
        "source_window_sha256": None,
        "observed_frame_sha256s": [],
        "positive_control_result": "NOT_MEASURED",
        "reversed_time_control_result": "NOT_MEASURED",
        "constructed_positive_detected": None,
        "constructed_reversal_rejected": None,
        "observed_control_pairs": 0,
        **_NO_GOLD,
    }


def measure_source_video_controls(window, *, root: Path, matcher=None) -> dict:
    """Use up to four real, SHA-pinned decoded video frames; no network calls.

    Deliberately conservative: lack of distinct frames or codec support is
    UNKNOWN. No automatic production qualification or source-ID promotion.
    """
    from holoclip.source_candidate_visual import compare_sparse_visual_sequences

    result = _base()
    if matcher is None:
        matcher = compare_sparse_visual_sequences
    try:
        import cv2

        video = Path(window.video_path)
        if (not video.is_file() or video.is_symlink()
                or not 0 < video.stat().st_size <= MAX_WINDOW_BYTES
                or checksum_file(video) != window.media_sha256):
            result["status"] = "UNKNOWN_SOURCE_VIDEO_HASH_OR_BUDGET"
            return result
        source_frames = sorted(
            window.frames, key=lambda f: (f.pts_seconds, f.id)
        )
        if len(source_frames) < 3:
            result["status"] = "UNKNOWN_INSUFFICIENT_MEASURED_SOURCE_FRAMES"
            return result
        if len(source_frames) > MAX_CONTROL_FRAMES:
            last = len(source_frames) - 1
            source_frames = [
                source_frames[(i * last) // (MAX_CONTROL_FRAMES - 1)]
                for i in range(MAX_CONTROL_FRAMES)
            ]
        pixels = []
        hashes = []
        for frame in source_frames:
            path = Path(frame.locator)
            if (not path.is_file() or path.is_symlink()
                    or not 0 < path.stat().st_size <= MAX_FRAME_BYTES
                    or checksum_file(path) != frame.checksum):
                result["status"] = "UNKNOWN_MEASURED_FRAME_HASH_OR_BUDGET"
                return result
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None or min(image.shape[:2]) < 64:
                result["status"] = "UNKNOWN_MEASURED_FRAME_DECODE"
                return result
            pixels.append(image)
            hashes.append(frame.checksum)
        result["source_window_sha256"] = window.media_sha256
        result["observed_frame_sha256s"] = hashes
        if len(set(hashes)) < 3:
            result["status"] = "UNKNOWN_REPEATED_SOURCE_IMAGES"
            return result
        # Always use a content-derived directory name, never untrusted
        # title/channel strings or source IDs in filesystem paths.
        run_dir = root / (
            "control-" + window.media_sha256[:16]
            + "-" + hashes[0][:8]
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        src = []
        reencoded = []
        for i, (frame, image) in enumerate(zip(source_frames, pixels)):
            data = image.copy()
            # A tiny synthetic corner overlay plus lossy JPEG compression
            # provides a known-correspondence controlled perturbation.
            cv2.rectangle(data, (5, 5), (28, 23), (200, 200, 200), -1)
            jpeg = run_dir / f"control-{i:02d}.jpg"
            if not cv2.imwrite(
                str(jpeg), data, [cv2.IMWRITE_JPEG_QUALITY, 90]
            ):
                raise ValueError("Cannot encode independent control JPEG")
            ts = float(i * 2)
            src.append(SimpleNamespace(
                id=f"src-{i}", locator=str(frame.locator),
                checksum=frame.checksum, pts_seconds=ts,
            ))
            reencoded.append(SimpleNamespace(
                id=f"reencoded-{i}", locator=str(jpeg),
                checksum=checksum_file(jpeg), pts_seconds=ts,
            ))
        negative = [SimpleNamespace(
            id=f"reversed-{i}", locator=row.locator,
            checksum=row.checksum, pts_seconds=float(i * 2),
        ) for i, row in enumerate(reversed(reencoded))]
        good = matcher(
            SimpleNamespace(frames=reencoded), [
                SimpleNamespace(frames=src),
            ],
        )
        bad = matcher(
            SimpleNamespace(frames=negative), [
                SimpleNamespace(frames=src),
            ],
        )
        approved = "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
        result.update({
            "status": "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED",
            "observed_control_pairs": len(src),
            "positive_control_result": good["status"],
            "reversed_time_control_result": bad["status"],
            "constructed_positive_detected": good["status"] == approved,
            "constructed_reversal_rejected": bad["status"] != approved,
        })
        return result
    except Exception as exc:
        result["status"] = "UNKNOWN_CONSTRUCTED_VIDEO_CONTROL_FAILURE"
        result["error_class"] = type(exc).__name__
        return result
