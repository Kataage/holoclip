"""Second, pixel-domain check for sparse ORB source/finished video sequences.

The ORB affine matcher chooses candidate frame pairs. This separate check
uses sampled image intensities, low-frequency DCT fingerprints and temporal
uniqueness -- it does not reuse ORB descriptors, keypoint counts, or geometry.
This is a conservative confirmation of *observed* images, not editor/source
identity gold, an exact seek timestamp, or a calibrated probability.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

VERSION = "independent-dct-temporal-video-check-v1"
MAX_PAIRS = 6
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PAIR_DISTANCE_BITS = 12
MIN_OFF_DIAGONAL_MARGIN_BITS = 3
MIN_MOTION_BITS = 5


def _checked_image(frame):
    import cv2

    path = Path(frame.locator)
    if (not path.is_file() or path.is_symlink()
            or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES):
        raise ValueError("Measured frame path unsafe, absent or exceeds budget")
    if not isinstance(frame.checksum, str) or len(frame.checksum) != 64:
        raise ValueError("Measured frame checksum is missing")
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != frame.checksum:
        raise ValueError("Measured image bytes no longer match saved checksum")
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or min(image.shape[:2]) < 64:
        raise ValueError("Frame cannot be decoded for independent check")
    return image


def _phash(image, scale: float, offset: float):
    import cv2
    import numpy as np

    height, width = image.shape
    h, w = max(32, round(height*scale)), max(32, round(width*scale))
    cx = min(width - w//2, max(w//2, round(width * (.5 + offset))))
    cy = height//2
    left = max(0, min(width-w, cx - w//2))
    top = max(0, min(height-h, cy - h//2))
    crop = image[top:top+h, left:left+w]
    reduced = cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA)
    spectrum = cv2.dct(np.float32(reduced))
    low = spectrum[:8, :8].copy()
    # Independent of global image brightness.
    low[0, 0] = 0
    median = float(np.median(low.flatten()[1:]))
    bits = (low.flatten()[1:] > median).astype("uint8")
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def _fingerprints(image):
    return tuple(_phash(image, scale, offset) for scale, offset in (
        (1., 0.), (.82, 0.), (.82, -.10), (.82, .10),
        (.66, 0.), (.66, -.12), (.66, .12),
    ))


def _distance(a, b):
    return min((x ^ y).bit_count() for x in a for y in b)


def confirm_pixel_temporal_pairs(pairs: list[tuple]) -> dict:
    """Confirm 3-6 measured ORB pairs independently, fail-closed on uncertainty.

    Pair order must advance on both sides; images must be independently
    checksum verified. These 63-bit DCT hashes cannot establish a stream
    identity or exclude all false positives; their purpose is to reject
    sparse deceptive geometry and repeated/stationary image content.
    """
    base = {
        "version": VERSION, "status": "UNKNOWN_NOT_CONFIRMED",
        "checked_pairs": 0, "matched_dct_distances_bits": [],
        "min_temporal_diversity_bits": None,
        "distinct_off_diagonal_margin_bits": None,
        "source_identity_verified": False,
        "reviewed_gold_available": False,
        "qualification_allowed": False,
    }
    if not isinstance(pairs, list) or not 3 <= len(pairs) <= MAX_PAIRS:
        base["status"] = "UNKNOWN_INDEPENDENT_PAIR_BUDGET"
        return base
    if any(not (
        math.isfinite(a.pts_seconds) and math.isfinite(b.pts_seconds)
    ) for a, b in pairs):
        base["status"] = "UNKNOWN_INVALID_LOCAL_FRAME_TIME"
        return base
    for left, right in zip(pairs, pairs[1:]):
        if (right[0].pts_seconds-left[0].pts_seconds < .4
                or right[1].pts_seconds-left[1].pts_seconds < .4):
            base["status"] = "UNKNOWN_NONADVANCING_IMAGES"
            return base
    try:
        source = [_fingerprints(_checked_image(src)) for src, _ in pairs]
        finished = [_fingerprints(_checked_image(dst)) for _, dst in pairs]
    except (OSError, ValueError):
        base["status"] = "UNKNOWN_IMAGE_CHECKSUM_OR_DECODE"
        return base
    n = len(pairs)
    diagonal = [_distance(source[i], finished[i]) for i in range(n)]
    motion_src = [_distance(source[i], source[i+1]) for i in range(n-1)]
    motion_dst = [_distance(finished[i], finished[i+1]) for i in range(n-1)]
    diversity = min(motion_src + motion_dst)
    margins = []
    for i in range(n):
        competitors = [
            _distance(source[j], finished[i])
            for j in range(n) if j != i
        ]
        margins.append(min(competitors) - diagonal[i])
    base.update({
        "checked_pairs": n,
        "matched_dct_distances_bits": diagonal,
        "min_temporal_diversity_bits": diversity,
        "distinct_off_diagonal_margin_bits": min(margins),
    })
    if any(value > MAX_PAIR_DISTANCE_BITS for value in diagonal):
        base["status"] = "UNKNOWN_PERCEPTUAL_FRAME_MISMATCH"
    elif diversity < MIN_MOTION_BITS:
        base["status"] = "UNKNOWN_STATIC_OR_REPEATED_VIDEO"
    elif min(margins) < MIN_OFF_DIAGONAL_MARGIN_BITS:
        base["status"] = "UNKNOWN_PERCEPTUAL_TEMPORAL_AMBIGUITY"
    else:
        base["status"] = "PIXEL_TEMPORAL_SEQUENCE_CORROBORATED_NOT_GOLD"
    return base
