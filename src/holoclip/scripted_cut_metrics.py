"""Offline cut-candidate scoring against ONLY explicitly scripted synthetic edits.

Scripted construction markers are not independently reviewed human editorial
gold. This module never changes provider qualification, routing, or training.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from holoclip.provider_benchmark import atomic_json

REFERENCE_VERSION = "scripted-ffmpeg-cut-reference-v1"
SCORE_VERSION = "scripted-cut-candidate-score-v1"
EXPECTED_SOURCE = "SCRIPTED_FFMPEG_ONLY"
CUT_VERSION = "reserved-acoustic-cut-brackets-v1"
REFINEMENT_VERSION = "bounded-acoustic-cut-brackets-v2"
MAX_CUTS = 64
MAX_TRANSITIONS = 256
_FALSE_CLAIMS = (
    "production_ready", "ready", "qualification_allowed",
    "reviewed_gold_available", "benchmark_held_out", "event_split_verified",
    "frame_accurate_cuts_verified", "dense_output_source_map_verified",
)


def _second(value: object, *, duration: float) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= duration):
        raise ValueError("Non-finite or out-of-duration scripted time")
    return float(value)


def score_scripted_cut_intervals(
    *, scripted_cuts: list[float], transitions: list[dict],
    duration_seconds: float, tolerance_seconds: float = 0.,
    max_bracket_seconds: float = 8.,
) -> dict:
    """One-to-one interval recall and unmatched candidate counts.

    A wide acoustic bracket cannot score a hit even if it contains the
    boundary. UNKNOWN evidence is never interpreted as a detected cut.
    Intervals are NOT measurements of exact editor frame timing.
    """
    if (type(duration_seconds) not in (float, int)
            or not math.isfinite(duration_seconds)
            or not 0 < duration_seconds <= 3600):
        raise ValueError("Invalid finite synthetic edited media duration")
    if (type(tolerance_seconds) not in (float, int)
            or not 0 <= tolerance_seconds <= 1.):
        raise ValueError("Unsafe tolerance; maximum 1 second")
    if (type(max_bracket_seconds) not in (float, int)
            or not 0 < max_bracket_seconds <= 12.):
        raise ValueError("Invalid maximum eligible bracket width")
    if (not isinstance(scripted_cuts, list) or len(scripted_cuts) > MAX_CUTS
            or not isinstance(transitions, list)
            or len(transitions) > MAX_TRANSITIONS):
        raise ValueError("Too many or malformed scripted cuts/candidates")

    duration = float(duration_seconds)
    truth = [_second(t, duration=duration) for t in scripted_cuts]
    if any(not 0 < t < duration for t in truth):
        raise ValueError("Cuts must be strictly inside the completed clip")
    if truth != sorted(set(truth)):
        raise ValueError("Scripted cut markers must be strictly ordered and unique")

    candidates = []
    unknown = 0
    overbroad = 0
    for item in transitions:
        if not isinstance(item, dict) or item.get("status") not in {
            "CUT_INTERVAL_CANDIDATE", "UNKNOWN_WIDE_GAP",
            "UNKNOWN_CONTRADICTORY_AUDIO",
        }:
            raise ValueError("Unrecognized acoustic transition")
        interval = item.get("interval")
        if (not isinstance(interval, list) or len(interval) != 2):
            raise ValueError("Invalid cut interval")
        lo, hi = (_second(v, duration=duration) for v in interval)
        if lo > hi:
            raise ValueError("Reversed cut interval")
        if item["status"] != "CUT_INTERVAL_CANDIDATE":
            unknown += 1
            continue
        width = hi - lo
        if width > max_bracket_seconds:
            overbroad += 1
        candidates.append({
            "index": len(candidates), "interval": [lo, hi],
            "width_seconds": round(width, 4),
            "eligible": width <= max_bracket_seconds,
        })

    # Maximum-cardinality bipartite matching rather than each bracket
    # independently counting every true cut it happens to contain.
    adjacency = {
        t: sorted(
            [k for k, c in enumerate(candidates)
             if c["eligible"] and
             c["interval"][0] - tolerance_seconds <= truth[t] <=
             c["interval"][1] + tolerance_seconds],
            key=lambda k: (
                abs(sum(candidates[k]["interval"]) / 2 - truth[t]),
                candidates[k]["width_seconds"], k,
            ),
        )
        for t in range(len(truth))
    }
    selected_by_candidate: dict[int, int] = {}

    def assign(t: int, visited: set[int]) -> bool:
        for k in adjacency[t]:
            if k in visited:
                continue
            visited.add(k)
            if k not in selected_by_candidate or assign(selected_by_candidate[k], visited):
                selected_by_candidate[k] = t
                return True
        return False

    for t in sorted(range(len(truth)), key=lambda index: (len(adjacency[index]), index)):
        assign(t, set())

    matched = sorted(
        ({"scripted_cut_seconds": truth[t], "candidate_index": k,
          "candidate_interval": candidates[k]["interval"],
          "candidate_width_seconds": candidates[k]["width_seconds"],
          "midpoint_absolute_error_seconds": round(abs(
              sum(candidates[k]["interval"]) / 2 - truth[t]
          ), 4)}
         for k, t in selected_by_candidate.items()),
        key=lambda row: row["scripted_cut_seconds"],
    )
    tp = len(matched)
    fp = len(candidates) - tp
    fn = len(truth) - tp
    precision = tp / len(candidates) if candidates else None
    recall = tp / len(truth) if truth else None
    f1 = (
        2 * tp / (2 * tp + fp + fn)
        if (2 * tp + fp + fn) else None
    )
    result = {
        "version": SCORE_VERSION,
        "reference_kind": EXPECTED_SOURCE,
        "status": "SCRIPTED_CONSTRUCTION_METRICS_ONLY",
        "production_ready": False, "ready": False,
        "qualification_allowed": False, "reviewed_gold_available": False,
        "benchmark_held_out": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "editor_cut_precision": None, "editor_cut_recall": None,
        "native_timestamp_error_seconds": None,
        "scripted_duration_seconds": duration,
        "scripted_cut_count": len(truth),
        "cut_candidate_count": len(candidates),
        "true_positive_candidates": tp,
        "false_positive_candidates": fp,
        "missed_scripted_cuts": fn,
        "unknown_audio_transitions": unknown,
        "overbroad_candidates_counted_as_false_positives": overbroad,
        "scripted_interval_precision": round(precision, 6) if precision is not None else None,
        "scripted_interval_recall": round(recall, 6) if recall is not None else None,
        "scripted_interval_f1": round(f1, 6) if f1 is not None else None,
        "matched": matched,
        "unmatched_scripted_cut_seconds": [
            t for i, t in enumerate(truth)
            if i not in selected_by_candidate.values()
        ],
        "unmatched_candidate_indices": [
            i for i in range(len(candidates))
            if i not in selected_by_candidate
        ],
        "matched_midpoint_median_absolute_error_seconds": (
            round(statistics.median(m["midpoint_absolute_error_seconds"]
                                     for m in matched), 4)
            if matched else None
        ),
        "matched_bracket_median_width_seconds": (
            round(statistics.median(m["candidate_width_seconds"] for m in matched), 4)
            if matched else None
        ),
        "tolerance_seconds": float(tolerance_seconds),
        "maximum_eligible_bracket_seconds": float(max_bracket_seconds),
        "limitations": [
            "Scripted FFmpeg construction boundaries are NOT independent human editor gold",
            "Precision and recall count eligible acoustic candidate intervals, NOT exact frame cuts",
            "Unknown acoustic transitions are separate and not assumed to be absent real edits",
            "Source originality, rights and cross-video editorial relevance are NOT scored",
        ],
    }
    return result


def score_scripted_cut_report(
    reference: dict, report: dict, *, tolerance_seconds: float = 0.,
    max_bracket_seconds: float = 8.,
) -> dict:
    """Fail-closed evaluation of a single scripted clip against saved cut evidence."""
    if (not isinstance(reference, dict) or not isinstance(report, dict)
            or reference.get("version") != REFERENCE_VERSION
            or reference.get("reference_kind") != EXPECTED_SOURCE):
        raise ValueError("Expected explicitly synthetic FFmpeg construction reference")
    clip_id = reference.get("clip_id")
    if not isinstance(clip_id, str) or not clip_id or len(clip_id) > 128:
        raise ValueError("Missing synthetic reference clip identity")
    if (report.get("version") != CUT_VERSION or not isinstance(report.get("records"), dict)
            or any(report.get(f) is not False for f in (
                "qualification_allowed", "reviewed_gold_available",
                "benchmark_held_out", "event_split_verified", "frame_accurate_cuts_verified",
            ))):
        raise ValueError("Only unpromoted acoustic cut evidence may be compared")
    row = report["records"].get(clip_id)
    if (not isinstance(row, dict)
            or row.get("status") != "MEASURED_UNREVIEWED_CUT_BRACKETS"
            or row.get("clip_id") != clip_id
            or row.get("frame_accurate_cuts_verified") is not False):
        raise ValueError("No matching measured unreviewed synthetic clip row")
    acoustic = row.get("refinement")
    if (not isinstance(acoustic, dict)
            or acoustic.get("version") != REFINEMENT_VERSION
            or acoustic.get("status") not in {
                "AUDIO_SUPPORTED_INTERVALS", "UNKNOWN_NO_UNIQUE_POINTS",
                "UNKNOWN_SAMPLING_BUDGET", "UNKNOWN_NO_SUPPORTED_RANGES",
            }):
        raise ValueError("Unsupported acoustic method or measurement state")
    result = score_scripted_cut_intervals(
        scripted_cuts=reference.get("cut_seconds"),
        transitions=acoustic.get("transitions"),
        duration_seconds=reference.get("duration_seconds"),
        tolerance_seconds=tolerance_seconds,
        max_bracket_seconds=max_bracket_seconds,
    )
    result["clip_id"] = clip_id
    result["measurement_status"] = acoustic["status"]
    return result


def score_scripted_cut_files(
    *, reference_path: Path, report_path: Path, output_path: Path,
    tolerance_seconds: float = 0., max_bracket_seconds: float = 8.,
) -> dict:
    """Write an isolated fixture-only score, never overwrite source evidence."""
    if (output_path.resolve() in {reference_path.resolve(), report_path.resolve()}
            or not reference_path.is_file() or not report_path.is_file()):
        raise ValueError("Input files must exist and output cannot overwrite them")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = score_scripted_cut_report(
        reference, report, tolerance_seconds=tolerance_seconds,
        max_bracket_seconds=max_bracket_seconds,
    )
    atomic_json(output_path, result)
    return result
