"""Measure the audio time origin of bounded original-video sections.

All times below have a known ~0.25s fingerprint resolution and are candidate
stream-absolute acoustic offsets, NOT native absolute video-frame timestamps.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from holoclip.audio_anchor_alignment import HOP, VERSION as AUDIO_VERSION, fingerprint_media
from holoclip.video_measurement import frame_pts, probe_video

VERSION = "bounded-source-audio-clock-v1"


def locate_section_audio_origin(
    section_features, source_features, *, anchor_seconds: float = 4.,
    max_anchors: int = 6, min_score: float = .79,
    min_margin: float = .065, max_spread: float = .25,
) -> dict:
    """Match >=3 independently unique fragments to the full source audio clock.

    Unlike coarse edited-clip alignment, shorter sections get dense anchors,
    but only a *single*, stable section start-time hypothesis can pass.
    """
    import numpy as np

    section = np.asarray(section_features, dtype=np.float32)
    source = np.asarray(source_features, dtype=np.float32)
    if (section.ndim != 2 or source.ndim != 2
            or section.shape[1:] != (12,) or source.shape[1:] != (12,)
            or not np.isfinite(section).all() or not np.isfinite(source).all()):
        raise ValueError("Expected finite versioned 12-band acoustic measurements")
    if not 3 <= max_anchors <= 8 or not 3 <= anchor_seconds <= 6:
        raise ValueError("Invalid source section anchor budget")
    width = round(anchor_seconds / HOP)
    # Do not treat overlapping fragments of the same source moment as
    # independent corroboration.
    if len(section) < 3 * width or len(source) < len(section):
        return {"version": VERSION, "status": "UNKNOWN_SHORT_OR_MISSING_AUDIO",
                "origin_seconds": None, "anchors": [],
                "reason": "Fewer than three independent anchored spans"}
    starts = np.unique(np.rint(np.linspace(
        0, len(section) - width, max_anchors,
    )).astype(int))
    starts = [int(v) for v in starts]
    n = len(source)
    size = 1 << (n + width - 2).bit_length()
    src_fft = np.fft.rfft(source, n=size, axis=0)
    accepted = []
    for start in starts:
        fragment = section[start:start + width]
        if (np.linalg.norm(fragment, axis=1) > .5).mean() < .75:
            continue
        query_fft = np.fft.rfft(fragment[::-1], n=size, axis=0)
        scores = np.fft.irfft((src_fft * query_fft).sum(axis=1), n=size)[width - 1:n] / width
        best = int(scores.argmax())
        corr = float(scores[best])
        alternatives = scores.copy()
        exclusion = width * 2
        alternatives[max(0, best - exclusion):min(len(scores), best + exclusion + 1)] = -1
        competitor = float(alternatives.max())
        margin = corr - competitor
        if corr < min_score or margin < min_margin:
            continue
        accepted.append({
            "section_start": round(start * HOP, 3),
            "source_start": round(best * HOP, 3),
            "origin_seconds": round((best - start) * HOP, 3),
            "correlation": round(corr, 5),
            "uniqueness_margin": round(margin, 5),
        })
    if len(accepted) < 3:
        status = "UNKNOWN_NONUNIQUE_SOURCE_AUDIO"
        origin = None
    else:
        offsets = [a["origin_seconds"] for a in accepted]
        origin = float(np.median(offsets))
        # Prevent a wrong source-section clock if the downloaded section
        # contains discontinuities, duplicates or speed edits.
        separated = [a for a in accepted if abs(a["origin_seconds"] - origin) <= max_spread]
        independent = []
        for a in separated:
            if not independent or a["section_start"] >= independent[-1]["section_start"] + anchor_seconds:
                independent.append(a)
        if len(independent) < 3 or max(offsets) - min(offsets) > max_spread:
            origin = None
            status = "UNKNOWN_INCONSISTENT_SOURCE_CLOCK"
        else:
            status = "PARTIAL_SECTION_AUDIO_CLOCK"
    return {
        "version": VERSION, "status": status,
        "origin_seconds": round(origin, 3) if origin is not None else None,
        "resolution_seconds": HOP, "anchors": accepted,
        "attempted_anchors": len(starts),
        "unknowns": [
            "Decoded audio sample zero and video frame PTS have different time bases",
            "Streaming container audio priming and A/V sync still require validation",
            "Original livestream absolute frame time not independently certified",
        ],
    }


def _first_audio_pts(video: Path) -> float:
    import json

    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required to inspect section media clock")
    completed = subprocess.run(
        [executable, "-v", "error", "-select_streams", "a:0", "-show_packets",
         "-read_intervals", "%+#1", "-show_entries", "packet=pts_time",
         "-of", "json", str(video)],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    packets = json.loads(completed.stdout).get("packets", [])
    if not packets:
        raise ValueError("No decoded source section audio clock")
    return float(packets[0]["pts_time"])


def calibrate_section_clock(
    section_video: Path, original_audio: Path, cache_root: Path,
    *, fingerprint=fingerprint_media, locate=locate_section_audio_origin,
    probe=probe_video, pts=frame_pts, audio_pts=_first_audio_pts,
) -> dict:
    """Link audio correspondence to native section frame PTS without overclaiming."""
    meta = probe(section_video)
    duration = float(meta["format"]["duration"])
    if (not math.isfinite(duration) or not 10 <= duration <= 123
            or not any(s.get("codec_type") == "video" for s in meta["streams"])
            or not any(s.get("codec_type") == "audio" for s in meta["streams"])):
        return {"version": VERSION, "status": "UNKNOWN_SECTION_NO_BOUNDED_AV",
                "origin_seconds": None, "frame_pts_verified": False}
    timeline = pts(section_video)
    if (not 3 <= len(timeline) <= 15000
            or not all(math.isfinite(t) for t in timeline)
            or timeline != sorted(timeline)):
        raise ValueError("Invalid or excessive bounded source native frame PTS")
    packet_start = audio_pts(section_video)
    if not math.isfinite(packet_start):
        raise ValueError("No finite audio packet PTS")
    section = fingerprint(section_video, cache_root)
    stream = fingerprint(original_audio, cache_root)
    result = locate(section, stream)
    origin = result.get("origin_seconds")
    if result["status"] != "PARTIAL_SECTION_AUDIO_CLOCK" or origin is None:
        return {
            **result, "frame_pts_verified": False,
            "source_native_video_pts_range": [
                round(timeline[0], 6), round(timeline[-1], 6),
            ],
            "audio_first_packet_pts": round(packet_start, 6),
            "mapped_source_video_frame_times": [],
        }
    # FFprobe native video PTS and first audio packet PTS are measured in
    # section container units. Conversion to original clock is approximate:
    # encoded audio may have priming delay, and source AV sync is unknown.
    mapped_start = origin + timeline[0] - packet_start
    mapped_end = origin + timeline[-1] - packet_start
    if not (0 <= mapped_start <= mapped_end):
        return {**result, "status": "UNKNOWN_AV_CLOCK_DISAGREEMENT",
                "origin_seconds": None, "frame_pts_verified": False,
                "mapped_source_video_frame_times": []}
    return {
        **result, "frame_pts_verified": True,
        "original_video_absolute_pts_verified": False,
        "source_video_time_basis": "acoustic_origin_plus_native_section_pts_minus_audio_packet_pts",
        "first_audio_packet_pts": round(packet_start, 6),
        "source_native_video_pts_range": [round(timeline[0], 6), round(timeline[-1], 6)],
        "approx_original_video_range": [round(mapped_start, 3), round(mapped_end, 3)],
        "max_claimed_precision_seconds": .25,
        "unresolved": [
            "Source native PTS within the bounded downloaded section is measured",
            "Conversion to original stream absolute time remains audio-relative and approximate",
            "No source/output visual frame matching or edited-cut truth is established",
        ],
    }
