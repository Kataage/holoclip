"""Conservative source-video checks for independently observed output changes.

Requires two same-group, unique audio anchors across each visual peak, a
partially calibrated section clock and already pinned source media. A visual
change in the source is *plausible alternative causation*, not proof that the
finished edit reused the same natural transition. Absence is never gold.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from holoclip.visual_cut_brackets import (
    VERSION as VISUAL_VERSION, measure_visual_cut_bracket,
)
from holoclip.visual_only_survey import VERSION as SURVEY_VERSION

VERSION = "bounded-output-source-visual-crosscheck-v1"
MAX_PROBES = 6
_ALLOWED = {
    "VISUAL_JUMP_CANDIDATES", "UNKNOWN_NO_ISOLATED_JUMP",
    "UNKNOWN_INSUFFICIENT_FRAMES", "UNKNOWN_BRACKET_TOO_WIDE",
}


def _real(x: object) -> bool:
    return type(x) in (int, float) and math.isfinite(x)


def _anchor_point(point: object, group: int) -> bool:
    if not isinstance(point, dict) or point.get("group") != group:
        return False
    v = [point.get(k) for k in (
        "clip_start", "clip_end", "source_start", "source_end",
    )]
    return (all(_real(t) for t in v) and 0 <= v[0] < v[1]
            and 0 <= v[2] < v[3]
            and abs((v[1] - v[0]) - (v[3] - v[2])) <= .25)


def _mapped_window(peak: list[float], sections: list[dict],
                   clocks: list[dict], points: list[dict]) -> tuple[Path, list[float]] | None:
    """One-to-one local map only between adjacent strong continuous anchors."""
    middle = sum(peak) / 2
    possible = []
    for group, (section, clock) in enumerate(zip(sections, clocks)):
        if (clock.get("status") != "PARTIAL_SECTION_AUDIO_CLOCK"
                or clock.get("frame_pts_verified") is not True
                or clock.get("original_video_absolute_pts_verified") is not False
                or not _real(clock.get("origin_seconds"))
                or not _real(section.get("source_measured_duration_seconds"))
                or not _real(section.get("source_start"))
                or not _real(section.get("source_end"))):
            continue
        group_points = sorted(
            (p for p in points if _anchor_point(p, group)),
            key=lambda p: p["clip_start"],
        )
        for left, right in zip(group_points, group_points[1:]):
            # Only adjacent independent audio anchors with a short, continuous
            # bracket may justify source-clock interpolation near this peak.
            if (not left["clip_end"] <= peak[0]
                    or not peak[1] <= right["clip_start"]
                    or right["clip_start"] - left["clip_end"] > 4
                    or right["clip_start"] < left["clip_end"]):
                continue
            left_offset = left["source_end"] - left["clip_end"]
            right_offset = right["source_start"] - right["clip_start"]
            if abs(left_offset - right_offset) > .25:
                continue
            offset = (left_offset + right_offset) / 2
            origin = clock["origin_seconds"]
            local = [round(t + offset - origin, 3) for t in peak]
            duration = section["source_measured_duration_seconds"]
            if (not 1 <= local[0] < local[1] < duration - 1
                    or not section["source_start"] - 5 <= middle + offset
                    <= section["source_end"] + 5):
                continue
            possible.append((Path(section["source_video_path"]), local))
    # Disjoint, duplicate or ambiguous sections cannot create causal evidence.
    return possible[0] if len(possible) == 1 else None


def inspect_visual_only_source_changes(
    survey: dict, sections: list[dict], clocks: list[dict],
    refinement: dict, *, measure=measure_visual_cut_bracket,
) -> dict:
    """Return bounded source-change hypotheses; all edit/gold flags stay false."""
    result = {
        "version": VERSION, "status": "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY",
        "checked_visual_peaks": 0, "source_change_also_observed": 0,
        "source_no_isolated_change_observed": 0,
        "unknown_source_correspondence": 0,
        "peak_observations": [], "verified_edit_cut": False,
        "source_scene_change_ruled_out": False,
        "original_video_absolute_pts_verified": False,
        "reviewed_gold_available": False, "qualification_allowed": False,
    }
    if (not isinstance(survey, dict)
            or survey.get("version") != SURVEY_VERSION
            or survey.get("status") != "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
            or any(survey.get(k) is not False for k in (
                "verified_edit_cut", "reviewed_gold_available",
                "frame_accurate_cuts_verified", "qualification_allowed",
            ))):
        return result
    peaks = survey.get("observed_peaks")
    points = refinement.get("points") if isinstance(refinement, dict) else None
    if (not isinstance(peaks, list) or len(peaks) > 18
            or not isinstance(points, list) or len(points) > 1000
            or not isinstance(sections, list) or not isinstance(clocks, list)
            or len(sections) != len(clocks) or len(sections) > 5):
        raise ValueError("Malformed bounded output/source visual evidence")
    output_only = []
    for point in peaks:
        if (not isinstance(point, dict)
                or point.get("verified_edit_cut") is not False
                or point.get("source_scene_change_ruled_out") is not False
                or point.get("relation") not in (
                    "OVERLAPS_ACOUSTIC_CANDIDATE",
                    "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
                )):
            raise ValueError("Unreviewed visual peak was promoted")
        interval = point.get("time_bracket_seconds")
        if (not isinstance(interval, list) or len(interval) != 2
                or not all(_real(x) for x in interval)
                or not 0 <= interval[0] < interval[1] <= 86400):
            raise ValueError("Malformed sampled visual interval")
        if point["relation"] == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME":
            output_only.append(interval)
    if len(output_only) > MAX_PROBES:
        result["status"] = "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET"
        result["unknown_source_correspondence"] = len(output_only)
        return result

    result["status"] = "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS"
    for interval in output_only:
        linked = _mapped_window(interval, sections, clocks, points)
        status = "UNKNOWN_NO_TWO_SIDED_AUDIO_ANCHORS"
        if linked is not None:
            media, local_bracket = linked
            try:
                observed = measure(media, local_bracket)
                if not isinstance(observed, dict) or observed.get("status") not in _ALLOWED:
                    raise ValueError("Unsupported bounded source visual response")
                if observed.get("version") != VISUAL_VERSION:
                    raise ValueError("Wrong source visual detector version")
                status = {
                    "VISUAL_JUMP_CANDIDATES": "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED",
                    "UNKNOWN_NO_ISOLATED_JUMP": "SOURCE_NO_ISOLATED_CHANGE_OBSERVED",
                }.get(observed["status"], "UNKNOWN_SOURCE_VISUAL_DECODE")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                status = "UNKNOWN_SOURCE_VISUAL_DECODE"
        result["peak_observations"].append({
            "output_interval": interval,
            "status": status,
            "verified_edit_cut": False,
            "source_scene_change_ruled_out": False,
        })
        result["checked_visual_peaks"] += 1
        result["source_change_also_observed"] += int(
            status == "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED"
        )
        result["source_no_isolated_change_observed"] += int(
            status == "SOURCE_NO_ISOLATED_CHANGE_OBSERVED"
        )
        result["unknown_source_correspondence"] += int(status.startswith("UNKNOWN_"))
    return result
