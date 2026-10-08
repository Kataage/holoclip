"""Independent, bounded finished-video visual change survey.

Visual discontinuities can be original camera/game changes. Never infer exact
human edit cuts or claim exhaustiveness from incomplete output sampling.
"""
from __future__ import annotations

import math
from pathlib import Path

from holoclip.visual_cut_brackets import measure_visual_cut_bracket

VERSION = "bounded-output-only-visual-survey-v1"
MAX_WINDOWS = 6
WINDOW_SECONDS = 6.
MAX_PEAKS = 18
MAX_SECONDS = 3600.


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _windows(duration: float, *, window_seconds: float, max_windows: int) -> list[list[float]]:
    width = min(window_seconds, duration)
    if width <= 0:
        return []
    # Never introduce gaps while a complete survey fits the bounded budget.
    count = min(max_windows, max(1, math.ceil(duration / width)))
    starts = ([0.] if count == 1 else [
        i * (duration - width) / (count - 1) for i in range(count)
    ])
    return [[round(a, 3), round(min(duration, a + width), 3)] for a in starts]


def _covered_seconds(windows: list[list[float]]) -> float:
    total = 0.
    right = 0.
    for start, end in windows:
        total += max(0., end - max(start, right))
        right = max(right, end)
    return total


def survey_finished_visual_changes(
    media: Path, duration_seconds: float, acoustic_transitions: list[dict],
    *, max_windows: int = MAX_WINDOWS, window_seconds: float = WINDOW_SECONDS,
    measure=measure_visual_cut_bracket,
) -> dict:
    """Scan independent output windows and mark acoustic overlap, *not gold*.

    A decode failure is explicit UNKNOWN, not evidence of no edits. Media
    checksums, rights and source provenance are the caller's responsibility.
    """
    if (not _finite(duration_seconds) or not 0 < duration_seconds <= MAX_SECONDS
            or not _finite(window_seconds) or not 1 <= window_seconds <= 8
            or type(max_windows) is not int or not 1 <= max_windows <= MAX_WINDOWS
            or not isinstance(acoustic_transitions, list)
            or len(acoustic_transitions) > 256):
        raise ValueError("Unsafe visual survey budget or duration")
    candidates = []
    for change in acoustic_transitions:
        if not isinstance(change, dict):
            raise ValueError("Invalid acoustic transition")
        if change.get("status") != "CUT_INTERVAL_CANDIDATE":
            continue
        interval = change.get("interval")
        if (not isinstance(interval, list) or len(interval) != 2
                or not all(_finite(t) for t in interval)
                or not 0 <= interval[0] <= interval[1] <= duration_seconds + .05):
            raise ValueError("Malformed acoustic interval for output survey")
        candidates.append(interval)
    windows = _windows(float(duration_seconds), window_seconds=window_seconds,
                       max_windows=max_windows)
    coverage = round(min(float(duration_seconds), _covered_seconds(windows)), 3)
    result = {
        "version": VERSION, "status": "UNKNOWN_VISUAL_NOT_MEASURED",
        "source": "FINISHED_OUTPUT_ONLY_UNREVIEWED",
        "duration_seconds": round(float(duration_seconds), 3),
        "sampled_windows": windows,
        "sampled_coverage_seconds": coverage,
        "unsampled_duration_seconds": round(max(0., duration_seconds - coverage), 3),
        "sampling_clock": "uniform_ffmpeg_approximation_not_native_pts",
        "observed_peaks": [],
        "observed_visual_peaks": None,
        "without_acoustic_candidate": None,
        "verified_edit_cut": False,
        "reviewed_gold_available": False,
        "frame_accurate_cuts_verified": False,
        "qualification_allowed": False,
    }
    seen = set()
    peaks = []
    try:
        for start, end in windows:
            measurement = measure(media, [start, end])
            if (not isinstance(measurement, dict)
                    or measurement.get("status") not in {
                        "VISUAL_JUMP_CANDIDATES", "UNKNOWN_NO_ISOLATED_JUMP",
                        "UNKNOWN_INSUFFICIENT_FRAMES", "UNKNOWN_BRACKET_TOO_WIDE",
                    }):
                raise ValueError("Unsupported visual survey detector result")
            if measurement["status"] not in {
                "VISUAL_JUMP_CANDIDATES", "UNKNOWN_NO_ISOLATED_JUMP",
            }:
                result["status"] = "UNKNOWN_INSUFFICIENT_VISUAL_SAMPLING"
                return result
            for point in measurement.get("peaks", []):
                if not isinstance(point, dict):
                    raise ValueError("Malformed output-only visual peak")
                span = point.get("time_bracket_seconds")
                if (not isinstance(span, list) or len(span) != 2
                        or not all(_finite(t) for t in span)
                        or not start - .05 <= span[0] < span[1] <= end + .05):
                    raise ValueError("Unbounded visual peak time interval")
                # Deduplicate overlap caused by neighbouring survey windows
                # using approximate temporal intervals, not native frame PTS.
                key = (round(span[0], 2), round(span[1], 2))
                if key in seen:
                    continue
                seen.add(key)
                if len(peaks) >= MAX_PEAKS:
                    result["status"] = "UNKNOWN_VISUAL_PEAK_BUDGET"
                    return result
                overlap = any(span[0] <= acoustic[1] and acoustic[0] <= span[1]
                              for acoustic in candidates)
                peaks.append({
                    "time_bracket_seconds": span,
                    "relation": ("OVERLAPS_ACOUSTIC_CANDIDATE" if overlap
                                 else "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME"),
                    "source_scene_change_ruled_out": False,
                    "verified_edit_cut": False,
                    "time_basis": "uniform_ffmpeg_sampled_clock_unverified_frame_pts",
                })
    except (OSError, RuntimeError, ValueError):
        result["status"] = "UNKNOWN_BOUNDED_VISUAL_DECODE"
        return result
    result["observed_peaks"] = sorted(
        peaks, key=lambda point: point["time_bracket_seconds"]
    )
    result["observed_visual_peaks"] = len(peaks)
    result["without_acoustic_candidate"] = sum(
        p["relation"] == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME" for p in peaks
    )
    result["status"] = "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
    return result
