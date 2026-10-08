"""Conservative local audio refinement for candidate editorial cut *intervals*.

A transition between two separated corroborated audio pieces is a time bracket,
never a measured exact frame cut. Do not infer cuts across unsupported gaps.
"""
from __future__ import annotations

import math

from holoclip.audio_anchor_alignment import HOP

VERSION = "bounded-acoustic-cut-brackets-v2"


def refine_acoustic_cut_brackets(
    clip_features,
    source_features,
    ranges: list[dict],
    *,
    sample_step_seconds: float = 1.,
    anchor_seconds: float = 2.,
    search_radius_seconds: float = 3.,
    max_points: int = 480,
    min_correlation: float = .88,
    min_margin: float = .045,
) -> dict:
    """Locate locally unique short audio fragments near previously supported offsets.

    Never searches unrelated source regions, invents absent audio, or extrapolates
    exact cut times from distant acoustic anchors.
    """
    import numpy as np

    clip = np.asarray(clip_features, dtype=np.float32)
    source = np.asarray(source_features, dtype=np.float32)
    if (clip.ndim != 2 or source.ndim != 2
            or clip.shape[1:] != (12,) or source.shape[1:] != (12,)
            or not np.isfinite(clip).all() or not np.isfinite(source).all()):
        raise ValueError("Need finite, version-matched 12-band fingerprints")
    if (not .5 <= sample_step_seconds <= 2. or not 1.5 <= anchor_seconds <= 4.
            or not 1. <= search_radius_seconds <= 5.
            or not 1 <= max_points <= 1000):
        raise ValueError("Unsafe dense correspondence sampling budget")
    if not ranges:
        return {"version": VERSION, "status": "UNKNOWN_NO_SUPPORTED_RANGES",
                "points": [], "transitions": [], "unknowns": ["No coarse audio support"]}
    width = round(anchor_seconds / HOP)
    step = round(sample_step_seconds / HOP)
    radius = round(search_radius_seconds / HOP)
    if width < 6 or step < 1 or radius < width:
        raise ValueError("Unsafe feature matching window")
    if not 0 < min_correlation <= 1 or not 0 <= min_margin < 1:
        raise ValueError("Invalid unique acoustic match threshold")
    points = []
    ignored = 0
    budgets = 0
    previous_range_end = -1.
    validated = []
    # Preflight every coarse audio section before sampling any expensive
    # correlation. An earlier group must never consume the available budget
    # and thereby hide a later, potentially edited group.
    for group, section in enumerate(ranges):
        try:
            clip_start = float(section["clip_start"])
            clip_end = float(section["clip_end"])
            source_start = float(section["source_start"])
            source_end = float(section["source_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid supported audio region") from exc
        if (not all(math.isfinite(v) for v in (
            clip_start, clip_end, source_start, source_end,
        )) or not 0 <= clip_start < clip_end <= len(clip) * HOP + .05
                or not 0 <= source_start < source_end <= len(source) * HOP + .05
                or clip_start < previous_range_end - .5
                or abs((clip_end - clip_start) - (source_end - source_start)) > 1.):
            raise ValueError("Coarse range is not physically supported")
        previous_range_end = clip_end
        expected_offset = source_start - clip_start
        a = max(0, math.ceil(clip_start / HOP))
        b = min(len(clip) - width, math.floor((clip_end - width * HOP) / HOP))
        validated.append((group, a, b, expected_offset))

    def sample_count(first: int, last: int, stride: int) -> int:
        if last < first:
            return 0
        regular = (last - first) // stride + 1
        # End of every anchored section is always sampled, not silently
        # excluded by a stride remainder at the edit seam.
        return regular + int(first + (regular - 1) * stride != last)

    # The sampler may increase spacing, but never beyond 2s or the user's
    # explicitly requested minimum spacing. Candidate boundaries remain
    # interval-valued; sparse sampling does not establish accurate edit PTS.
    available_steps = sorted({
        candidate for candidate in (step, round(1.5 / HOP), round(2. / HOP))
        if candidate >= step
    })
    selected_step = next((
        candidate for candidate in available_steps
        if sum(sample_count(a, b, candidate) for _, a, b, _ in validated)
        <= max_points
    ), None)
    if selected_step is None:
        return {
            "version": VERSION, "status": "UNKNOWN_SAMPLING_BUDGET",
            "points": [], "transitions": [], "tested_points": 0,
            "sampling_strategy": "FULL_COVERAGE_OR_UNKNOWN",
            "maximum_sample_spacing_seconds": 2.,
            "unknowns": [
                "Even 2s spacing cannot cover all anchored ranges within budget",
                "No earlier partial range is promoted to a cut observation",
            ],
        }

    planned_points = sum(
        sample_count(a, b, selected_step) for _, a, b, _ in validated
    )
    for group, a, b, expected_offset in validated:
        if b < a:
            ignored += 1
            continue
        start_positions = list(range(a, b + 1, selected_step))
        if start_positions[-1] != b:
            start_positions.append(b)
        for start in start_positions:
            budgets += 1
            fragment = clip[start:start + width]
            if (np.linalg.norm(fragment, axis=1) > .5).mean() < .75:
                ignored += 1
                continue
            guess = start + int(round(expected_offset / HOP))
            lower = max(0, guess - radius)
            upper = min(len(source) - width, guess + radius)
            if lower > upper:
                ignored += 1
                continue
            scores = np.array([
                float(np.einsum("ij,ij->", fragment, source[j:j + width]) / width)
                for j in range(lower, upper + 1)
            ])
            location = int(scores.argmax())
            best = lower + location
            best_score = float(scores[location])
            # Nearby shifted matches are the same event and not an independent
            # alternative source occurrence. Wider separated peaks must compete.
            competing = np.abs(np.arange(lower, upper + 1) - best) > width
            other = float(scores[competing].max()) if competing.any() else -1.
            margin = best_score - other
            if best_score < min_correlation or margin < min_margin:
                ignored += 1
                continue
            points.append({
                "group": group,
                "clip_start": round(start * HOP, 3),
                "clip_end": round((start + width) * HOP, 3),
                "source_start": round(best * HOP, 3),
                "source_end": round((best + width) * HOP, 3),
                "offset_seconds": round((best - start) * HOP, 3),
                "correlation": round(best_score, 5),
                "uniqueness_margin": round(margin, 5),
            })
    transitions = []
    points.sort(key=lambda p: (p["clip_start"], p["group"]))
    for left, right in zip(points, points[1:]):
        difference = right["offset_seconds"] - left["offset_seconds"]
        if abs(difference) <= .75:
            continue
        gap = right["clip_start"] - left["clip_end"]
        if gap < -.5:
            # Conflicting overlapping windows are incompatible observations.
            transitions.append({
                "status": "UNKNOWN_CONTRADICTORY_AUDIO",
                "left_group": left["group"], "right_group": right["group"],
                "interval": [left["clip_start"], right["clip_end"]],
                "offset_change_seconds": round(difference, 3),
            })
            continue
        # Even a tightly bracketed change stays uncertain at HOP resolution
        # and under re-encoding, source audio masking or A/V drift.
        left_edge = max(0., left["clip_end"] - HOP)
        right_edge = right["clip_start"] + HOP
        transitions.append({
            "status": ("CUT_INTERVAL_CANDIDATE" if gap <= 6
                       else "UNKNOWN_WIDE_GAP"),
            "left_group": left["group"], "right_group": right["group"],
            "interval": [round(left_edge, 3), round(right_edge, 3)],
            "offset_change_seconds": round(difference, 3),
            "unresolved_gap_seconds": round(max(0., gap), 3),
            "basis": "nonidentical_unique_acoustic_offsets",
        })
    status = ("AUDIO_SUPPORTED_INTERVALS" if points else "UNKNOWN_NO_UNIQUE_POINTS")
    return {
        "version": VERSION, "status": status,
        "points": points, "transitions": transitions,
        "tested_points": budgets, "rejected_points": ignored,
        "planned_points": planned_points,
        "sample_spacing_seconds": round(selected_step * HOP, 3),
        "sampling_strategy": "FULL_COVERAGE_BUDGET_ADAPTED",
        "resolution_seconds": HOP,
        "unknowns": [
            "Cut boundaries are intervals rather than exact output frame positions",
            "Audio edits, ducking, compression and silence may hide transitions",
            "The local search is conditional on previously unique coarse anchors",
            "No speed/freeze classification, subtitle timing, or visual edit intent",
            "Independent same-event/multi-POV leakage and reviewed gold unavailable",
        ],
    }
