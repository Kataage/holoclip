"""Multi-pattern *constructed* matcher controls on actual bounded source-video frames.

The transformations have known local correspondence labels, NOT labels about
real human editorial decisions or whether a candidate is a Hololive stream.
No video fetching, LLM inference, GPU, or training/held-out data changes.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from holoclip.source_visual_real_controls import (
    MAX_CONTROL_FRAMES, MAX_FRAME_BYTES, MAX_WINDOW_BYTES,
)
from holoclip.video_measurement import checksum_file

VERSION = "source-measured-video-pattern-controls-v1"
APPROVED = "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
PATTERNS = {
    "jpeg_overlay": "constructed_positive",
    "brightness_shift": "constructed_positive",
    "center_zoom_108": "constructed_positive",
    "reversed_time": "constructed_temporal_decoy",
    "cyclic_time_shift": "constructed_temporal_decoy",
}
_NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "used_in_learning_or_holdout": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
}


def _report(status: str) -> dict:
    return {
        "version": VERSION,
        "status": status,
        "reference_type": "CONSTRUCTED_FROM_ACTUAL_MEASURED_SOURCE_FRAMES",
        "real_human_editor_labels": False,
        "official_hololive_source_verified": False,
        "source_video_sha256": None,
        "measured_frame_sha256s": [],
        "measured_frame_count": 0,
        "patterns": {},
        **_NO_GOLD,
    }


def measure_video_pattern_controls(window, *, root: Path, matcher=None) -> dict:
    """Try five varied controls only on pinned, bounded measured video bytes.

    A negative control is artificial video reordering, *not* a different
    original stream. Every pattern is run through the existing end-to-end
    ORB + independent DCT verifier. Errors are UNKNOWN, never measured fails.
    """
    from holoclip.source_candidate_visual import compare_sparse_visual_sequences

    if matcher is None:
        matcher = compare_sparse_visual_sequences
    result = _report("UNKNOWN_NO_MEASURED_PATTERN_VIDEO")
    try:
        import cv2

        media = Path(window.video_path)
        if (not media.is_file() or media.is_symlink()
                or not 0 < media.stat().st_size <= MAX_WINDOW_BYTES
                or checksum_file(media) != window.media_sha256):
            result["status"] = "UNKNOWN_MEASURED_VIDEO_HASH_OR_BUDGET"
            return result
        frames = sorted(window.frames, key=lambda f: (f.pts_seconds, f.id))
        if len(frames) < 3:
            result["status"] = "UNKNOWN_TOO_FEW_SOURCE_VIDEO_FRAMES"
            return result
        if len(frames) > MAX_CONTROL_FRAMES:
            last = len(frames) - 1
            frames = [frames[i*last//(MAX_CONTROL_FRAMES-1)]
                      for i in range(MAX_CONTROL_FRAMES)]
        images = []
        shas = []
        for frame in frames:
            path = Path(frame.locator)
            if (not path.is_file() or path.is_symlink()
                    or not 0 < path.stat().st_size <= MAX_FRAME_BYTES
                    or checksum_file(path) != frame.checksum):
                result["status"] = "UNKNOWN_MEASURED_FRAME_HASH_OR_BUDGET"
                return result
            pixels = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if pixels is None or min(pixels.shape[:2]) < 64:
                result["status"] = "UNKNOWN_SOURCE_FRAME_DECODE"
                return result
            images.append(pixels)
            shas.append(frame.checksum)
        result["source_video_sha256"] = window.media_sha256
        result["measured_frame_sha256s"] = shas
        result["measured_frame_count"] = len(frames)
        if len(set(shas)) < 3:
            result["status"] = "UNKNOWN_REPEATED_SOURCE_FRAMES"
            return result
        # Content-derived output, never an untrusted title or stream name.
        folder = root / f"patterns-{window.media_sha256[:20]}-{shas[0][:12]}"
        folder.mkdir(parents=True, exist_ok=True)
        source = [
            SimpleNamespace(
                id=f"source-{i}", locator=str(frame.locator),
                checksum=frame.checksum, pts_seconds=float(i*2),
            ) for i, frame in enumerate(frames)
        ]
        variants = {}
        for pattern in ("jpeg_overlay", "brightness_shift", "center_zoom_108"):
            rows = []
            for i, image in enumerate(images):
                altered = image.copy()
                if pattern == "jpeg_overlay":
                    cv2.rectangle(altered, (6, 6), (36, 26), (200, 200, 200), -1)
                elif pattern == "brightness_shift":
                    altered = cv2.convertScaleAbs(altered, alpha=1.07, beta=8)
                else:
                    h, w = image.shape[:2]
                    dx, dy = max(1, int(w*.037)), max(1, int(h*.037))
                    altered = cv2.resize(
                        image[dy:h-dy, dx:w-dx], (w, h),
                        interpolation=cv2.INTER_LINEAR,
                    )
                path = folder / f"{pattern}-{i}.jpg"
                if not cv2.imwrite(
                    str(path), altered, [cv2.IMWRITE_JPEG_QUALITY, 88],
                ):
                    raise ValueError("Cannot persist bounded generated control")
                rows.append(SimpleNamespace(
                    id=f"{pattern}-{i}", locator=str(path),
                    checksum=checksum_file(path), pts_seconds=float(i*2),
                ))
            variants[pattern] = rows
        # Reuse the same known-correspondence output pixels as the positive
        # JPEG overlay control; only the temporal ordering changes.
        ordinary = variants["jpeg_overlay"]
        variants["reversed_time"] = [
            SimpleNamespace(
                id=f"rev-{i}", locator=f.locator, checksum=f.checksum,
                pts_seconds=float(i*2),
            ) for i, f in enumerate(reversed(ordinary))
        ]
        variants["cyclic_time_shift"] = [
            SimpleNamespace(
                id=f"cyc-{i}", locator=f.locator, checksum=f.checksum,
                pts_seconds=float(i*2),
            ) for i, f in enumerate(ordinary[1:] + ordinary[:1])
        ]
        cases = {}
        for name, expected in PATTERNS.items():
            try:
                response = matcher(
                    SimpleNamespace(frames=variants[name]),
                    [SimpleNamespace(frames=source)],
                )
                if not isinstance(response, dict):
                    raise ValueError("Malformed end-to-end matcher response")
                if any(response.get(flag) is not False for flag in (
                    "source_identity_verified", "reviewed_gold_available",
                    "qualification_allowed", "used_in_learning_or_holdout",
                    "original_video_absolute_pts_verified",
                )):
                    raise ValueError("A constructed matcher result claimed gold")
                actual = response.get("status")
                if not isinstance(actual, str) or len(actual) > 128:
                    raise ValueError("Unsupported video matcher status")
                confirmed = actual == APPROVED
                correct = (confirmed if expected == "constructed_positive"
                           else not confirmed)
                cases[name] = {
                    "expected": expected,
                    "observed_status": actual,
                    "behaved_as_constructed": correct,
                }
            except Exception as exc:
                cases[name] = {
                    "expected": expected,
                    "observed_status": "UNKNOWN_CONTROL_EXECUTION_FAILURE",
                    "behaved_as_constructed": None,
                    "error_class": type(exc).__name__,
                }
        result["patterns"] = cases
        result["status"] = "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE"
        return result
    except Exception as exc:
        result["status"] = "UNKNOWN_CONSTRUCTED_PATTERN_VIDEO_FAILURE"
        result["error_class"] = type(exc).__name__
        return result
