"""Conservative, CPU-only visual frame correspondence for human editing evidence.

Geometry is image-measured, not authored by an LLM. Sparse frame comparisons
cannot establish exact cut boundaries or semantic editing intent.
"""
from __future__ import annotations

import math

VERSION = "visual-orb-affine-v1"


def _read_image(path: str, *, max_side: int = 640):
    import cv2

    raw = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if raw is None or min(raw.shape[:2]) < 64:
        raise ValueError("Image cannot be decoded or is too small")
    height, width = raw.shape
    factor = min(1.0, max_side / max(height, width))
    if factor < 1:
        raw = cv2.resize(raw, (round(width * factor), round(height * factor)),
                         interpolation=cv2.INTER_AREA)
    return raw


def _orb_features(path: str):
    import cv2

    image = _read_image(path)
    keypoints, descriptors = cv2.ORB_create(
        nfeatures=1200, fastThreshold=15, edgeThreshold=20,
    ).detectAndCompute(image, None)
    return image.shape, keypoints, descriptors


def _compare_features(source, finished) -> dict | None:
    """Measure scale/translation from spatially distributed matched pixels."""
    import cv2
    import numpy as np

    source_shape, source_points, source_desc = source
    finish_shape, finished_points, finished_desc = finished
    if (source_desc is None or finished_desc is None
            or len(source_points) < 16 or len(finished_points) < 16):
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(
        source_desc, finished_desc
    )
    matches = [m for m in matches if m.distance <= 65]
    if len(matches) < 16:
        return None
    matches.sort(key=lambda item: (item.distance, item.queryIdx, item.trainIdx))
    src = np.float32([source_points[m.queryIdx].pt for m in matches])
    dst = np.float32([finished_points[m.trainIdx].pt for m in matches])
    affine, mask = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.5,
        maxIters=2500, confidence=0.99,
    )
    if affine is None or mask is None:
        return None
    accepted = mask.reshape(-1).astype(bool)
    n = int(accepted.sum())
    ratio = n / len(matches)
    if n < 12 or ratio < .42:
        return None
    src_good = src[accepted]
    dst_good = dst[accepted]
    src_h, src_w = source_shape
    out_h, out_w = finish_shape
    hull_src = float(cv2.contourArea(cv2.convexHull(src_good))) / (src_w * src_h)
    hull_dst = float(cv2.contourArea(cv2.convexHull(dst_good))) / (out_w * out_h)
    if hull_src < .045 or hull_dst < .045:
        return None
    x, y = float(affine[0, 0]), float(affine[1, 0])
    scale_pixels = math.hypot(x, y)
    angle = math.degrees(math.atan2(y, x))
    if (not .45 <= scale_pixels <= 3.25 or abs(angle) > 6
            or not math.isfinite(scale_pixels)):
        return None
    estimate = (affine[:, :2] @ src_good.T).T + affine[:, 2]
    residual = float(np.sqrt(np.mean(np.sum((estimate - dst_good) ** 2, axis=1))))
    if residual > 3.5:
        return None
    zoom_x = scale_pixels * src_w / out_w
    zoom_y = scale_pixels * src_h / out_h
    if not (.5 <= zoom_x <= 3.0 and .5 <= zoom_y <= 3.0
            and abs(zoom_x - zoom_y) <= .15):
        # Different output aspect ratio or nonuniform stretch needs separate
        # validation, not an invented uniform zoom factor.
        return None
    center = affine @ np.array([src_w / 2, src_h / 2, 1.], dtype=np.float64)
    center_x, center_y = float(center[0] / out_w), float(center[1] / out_h)
    if not (math.isfinite(center_x) and math.isfinite(center_y)):
        return None
    zoom = (zoom_x + zoom_y) / 2
    displacement = math.hypot(center_x - .5, center_y - .5)
    if abs(zoom - 1) <= .055 and displacement <= .055:
        kind = "identity_like"
    elif zoom > 1.07 and displacement <= .075:
        kind = "zoom_candidate"
    elif abs(zoom - 1) <= .055 and displacement > .075:
        kind = "reframe_candidate"
    elif zoom > 1.07 and displacement > .075:
        kind = "zoom_reframe_candidate"
    else:
        kind = "other_affine_candidate"
    # This score ranks measured candidates; it is *not* a calibrated
    # probability of an editorial transform.
    score = ratio * min(1., n / 35.) * min(1., hull_src / .16)
    return {
        "kind": kind, "score": round(float(score), 5),
        "orb_inliers": n, "candidate_matches": len(matches),
        "inlier_ratio": round(ratio, 5),
        "source_coverage": round(hull_src, 5),
        "finished_coverage": round(hull_dst, 5),
        "reprojection_rmse_px": round(residual, 4),
        "zoom_factor_candidate": round(zoom, 4),
        "source_center_in_finished_normalized": [round(center_x, 4), round(center_y, 4)],
        "rotation_degrees": round(angle, 3),
    }


def inspect_visual_pair(source_window, finished_window, *,
                        max_frames: int = 64, max_time_delta: float = 4.0,
                        min_uniqueness_margin: float = .09) -> dict:
    """Return only reproducible, auditable sparse visual correspondences."""
    if (max_frames < 1 or max_frames > 64 or not 0 < max_time_delta <= 10):
        raise ValueError("Invalid bounded visual comparison parameters")
    if len(source_window.frames) > max_frames or len(finished_window.frames) > max_frames:
        return {"status": "UNKNOWN_FRAME_BUDGET", "observations": [],
                "unknowns": ["Input frames exceed configured budget; no silent truncation"]}
    frames_source = sorted(source_window.frames, key=lambda f: f.pts_seconds)
    frames_finished = sorted(finished_window.frames, key=lambda f: f.pts_seconds)
    if not frames_source or not frames_finished:
        return {"status": "UNKNOWN_NO_FRAMES", "observations": [],
                "unknowns": ["Measured frames missing"]}
    # Absolute yt-dlp seek position is *not* verified, so this uses only
    # within-section PTS as a soft candidate filter. It cannot establish a cut.
    cache = {f.id: _orb_features(f.locator) for f in frames_source + frames_finished}
    observations = []
    unmatched = 0
    ambiguous = 0
    for dst in frames_finished:
        ranked = []
        for src in frames_source:
            if abs(src.pts_seconds - dst.pts_seconds) > max_time_delta:
                continue
            geom = _compare_features(cache[src.id], cache[dst.id])
            if geom is not None:
                ranked.append((geom["score"], src, geom))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        if not ranked or ranked[0][0] < .20:
            unmatched += 1
            continue
        winner_score, best, geom = ranked[0]
        # Duplicated content at separated times is not a unique match.
        competitors = [
            row[0] for row in ranked[1:]
            if (abs(row[1].pts_seconds - best.pts_seconds) > 1.5
                or row[1].shot_id != best.shot_id)
        ]
        margin = winner_score - max(competitors) if competitors else None
        if margin is not None and margin < min_uniqueness_margin:
            ambiguous += 1
            continue
        observations.append({
            "source_frame_id": best.id, "finished_frame_id": dst.id,
            "source_shot_id": best.shot_id, "finished_shot_id": dst.shot_id,
            "source_pts_seconds": best.pts_seconds,
            "finished_pts_seconds": dst.pts_seconds,
            "uniqueness_margin": round(margin, 5) if margin is not None else None,
            "temporal_uniqueness_tested": margin is not None,
            **geom,
        })
    status = "SPARSE_VISUAL_CORRESPONDENCE" if observations else "UNKNOWN_VISUAL_MATCH"
    return {
        "version": VERSION, "status": status, "observations": observations,
        "unmatched_finished_frames": unmatched,
        "ambiguous_finished_frames": ambiguous,
        "unknowns": [
            "Frame PTS are local to independently decoded video windows",
            "Requested absolute source seek start and exact original frame time are not verified",
            "Sparse sampled frames cannot establish exact cut boundaries or visual absence",
            "RANSAC zoom/reframe geometry is a candidate, not semantic editing intent",
            "OCR, captions, overlays, BGM, multi-POV event identity and subjective quality unverified",
        ],
    }
