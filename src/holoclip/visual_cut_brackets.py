"""Bounded CPU frame-discontinuity diagnostics for acoustically proposed edit cuts.

No visual jump can prove an edit: natural camera/game transitions are confounders.
Frame times are uniform FFmpeg sampling-clock estimates, not decoded source PTS.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

VERSION = "bounded-output-visual-jumps-v1"
FPS = 8
WIDTH = 256
HEIGHT = 144
MAX_SECONDS = 10.0
MAX_FRAMES = 96


def extract_bounded_frames(
    media: Path, start: float, end: float, *, fps: int = FPS
):
    """Decode only a short, capped output bracket; never a whole source stream."""
    import numpy as np

    if (not math.isfinite(start) or not math.isfinite(end)
            or not 0 <= start < end or end - start > MAX_SECONDS
            or fps not in {4, 8, 12}):
        raise ValueError("Only finite bounded output brackets can be sampled")
    if not media.is_file():
        raise FileNotFoundError("Finished video is missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to measure cut brackets")
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
        "-i", str(media), "-t", f"{end - start:.3f}",
        "-an", "-sn", "-dn",
        "-vf", f"fps={fps},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    # Bounded output bytes and timeout. Never retain arbitrary decoded video.
    expected_limit = MAX_FRAMES * WIDTH * HEIGHT
    completed = subprocess.run(command, capture_output=True, timeout=90, check=True)
    data = completed.stdout
    if len(data) > expected_limit or len(data) % (WIDTH * HEIGHT):
        raise ValueError("Decoded cut-bracket frame budget or shape mismatch")
    frames = np.frombuffer(data, dtype=np.uint8).reshape((-1, HEIGHT, WIDTH))
    if len(frames) > MAX_FRAMES:
        raise ValueError("Too many decoded bracket frames")
    # Seek and fps sampling produce approximate times only; no PTS assertion.
    clock = [round(start + i / fps, 3) for i in range(len(frames))]
    return frames, clock


def analyze_visual_jump(frames, sampled_times: list[float], bracket: list[float]) -> dict:
    """Detect isolated spatial change peaks; rank evidence without exact cut claims."""
    import cv2
    import numpy as np

    arr = np.asarray(frames, dtype=np.uint8)
    if (arr.ndim != 3 or not 1 <= len(arr) <= MAX_FRAMES
            or arr.shape[1:] != (HEIGHT, WIDTH)
            or len(sampled_times) != len(arr)
            or len(bracket) != 2
            or not all(math.isfinite(float(x)) for x in [*sampled_times, *bracket])
            or not 0 <= bracket[0] < bracket[1] <= sampled_times[-1] + 1):
        raise ValueError("Invalid timestamped bounded frame samples")
    if any(b <= a for a, b in zip(sampled_times, sampled_times[1:])):
        raise ValueError("Non-monotonic sampled frame clock")
    if len(arr) < 3:
        return {"version": VERSION, "status": "UNKNOWN_INSUFFICIENT_FRAMES",
                "peaks": [], "frame_count": len(arr), "unknowns": ["Insufficient visual samples"]}
    delta = []
    for left, right in zip(arr[:-1], arr[1:]):
        left_blur = cv2.GaussianBlur(left, (5, 5), 0).astype(np.float32)
        right_blur = cv2.GaussianBlur(right, (5, 5), 0).astype(np.float32)
        # Compression noise is suppressed by blur and the 24/255 change floor.
        significant = np.abs(right_blur - left_blur) > 24.0
        changed = float(significant.mean())
        delta.append(changed)
    candidates = []
    for i, score in enumerate(delta):
        middle = (sampled_times[i] + sampled_times[i + 1]) / 2.
        if not bracket[0] <= middle <= bracket[1]:
            continue
        # Absolute discontinuity, not an automatically calibrated cut score.
        if score < .46:
            continue
        neighborhood = delta[max(0, i - 5):min(len(delta), i + 6)]
        outside = [v for j, v in enumerate(neighborhood)
                   if max(0, i - 5) + j != i]
        background = float(np.median(outside)) if outside else 0.
        if score - background < .22:
            continue
        candidates.append({
            "time_bracket_seconds": [sampled_times[i], sampled_times[i + 1]],
            "changed_pixel_fraction": round(score, 5),
            "local_background_change": round(background, 5),
            "excess_change": round(score - background, 5),
            "time_basis": "uniform_ffmpeg_sampled_clock_unverified_frame_pts",
        })
    # Do not allow two adjoining detected transitions to imply two exact cuts.
    peaks = sorted(candidates, key=lambda x: -x["excess_change"])[:3]
    status = "VISUAL_JUMP_CANDIDATES" if peaks else "UNKNOWN_NO_ISOLATED_JUMP"
    return {
        "version": VERSION, "status": status, "peaks": peaks,
        "frame_count": len(arr), "sampling_fps": (
            round(1. / (sampled_times[1] - sampled_times[0]), 3)
        ),
        "unknowns": [
            "A visual jump can be an original camera/game transition rather than an edit",
            "No dense frame-level source-to-output time correspondence established",
            "FFmpeg fps sampling clock is not a verified native frame PTS",
            "Zooms, overlays, fades, motion or compressed footage can mask real cuts",
        ],
    }


def measure_visual_cut_bracket(media: Path, bracket: list[float], *,
                               max_seconds: float = MAX_SECONDS,
                               extractor=extract_bounded_frames) -> dict:
    if (len(bracket) != 2 or not all(math.isfinite(float(x)) for x in bracket)
            or bracket[0] < 0 or bracket[1] <= bracket[0]
            or bracket[1] - bracket[0] > max_seconds):
        return {"version": VERSION, "status": "UNKNOWN_BRACKET_TOO_WIDE",
                "peaks": [], "unknowns": ["Acoustic bracket is not safely bounded"]}
    start = max(0., bracket[0] - .5)
    end = bracket[1] + .5
    if end - start > MAX_SECONDS:
        return {"version": VERSION, "status": "UNKNOWN_BRACKET_TOO_WIDE",
                "peaks": [], "unknowns": ["Padded bracket exceeds decoding limit"]}
    frames, times = extractor(media, start, end)
    return analyze_visual_jump(frames, times, bracket)
