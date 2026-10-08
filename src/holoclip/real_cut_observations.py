"""Reference-free, offline observations of real human-edited cut candidates.

The output is a *description of measured signals*, not editorial accuracy.
No stage, model, or production route can be qualified by these statistics.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter

from holoclip.visual_only_survey import VERSION as VISUAL_SURVEY_VERSION
from holoclip.visual_source_crosscheck import VERSION as SOURCE_VISUAL_VERSION
from holoclip.visual_only_frame_search import VERSION as VISUAL_SEARCH_VERSION

VERSION = "unreviewed-real-cut-observations-v1"
ACOUSTIC_VERSION = "bounded-acoustic-cut-brackets-v2"
MAX_TRANSITIONS = 256
_UNKNOWN = {"UNKNOWN_WIDE_GAP", "UNKNOWN_CONTRADICTORY_AUDIO"}
_MEASURED = "MEASURED_UNREVIEWED_CUT_BRACKETS"
_SIDE_MEASURED = "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE"
_NO_GOLD = {
    "ready": False, "production_ready": False, "qualification_allowed": False,
    "reviewed_gold_available": False, "benchmark_held_out": False,
    "event_split_verified": False, "frame_accurate_cuts_verified": False,
    "dense_output_source_map_verified": False, "editor_cut_precision": None,
    "editor_cut_recall": None,
}


def _interval(value: object) -> tuple[float, float]:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(t) not in (int, float) or not math.isfinite(t)
                   for t in value)):
        raise ValueError("Malformed acoustic cut interval")
    a, b = (float(t) for t in value)
    if not 0 <= a <= b <= 3600:
        raise ValueError("Out-of-range acoustic cut interval")
    return a, b


def _safe_unreviewed(row: dict) -> bool:
    return (
        isinstance(row, dict)
        and all(row.get(flag) is False for flag in (
            "qualification_allowed", "reviewed_gold_available",
            "event_split_verified", "frame_accurate_cuts_verified",
        ))
    )



def _describe_independent_visual_survey(row: dict) -> dict:
    """Check separate output-only visual evidence, without editor-cut claims."""
    result = {
        "output_only_visual_status": "NOT_MEASURED_OR_UNVERIFIED",
        "output_only_visual_change_count": None,
        "output_only_visual_without_acoustic_count": None,
        "output_only_visual_coverage_seconds": None,
    }
    survey = row.get("independent_output_visual_survey")
    if survey is None:
        return result
    if (not isinstance(survey, dict)
            or row.get("visual_survey_method_version") != VISUAL_SURVEY_VERSION
            or survey.get("version") != VISUAL_SURVEY_VERSION
            or survey.get("source") != "FINISHED_OUTPUT_ONLY_UNREVIEWED"
            or any(survey.get(k) is not False for k in (
                "verified_edit_cut", "reviewed_gold_available",
                "frame_accurate_cuts_verified", "qualification_allowed",
            ))):
        raise ValueError("Untrusted independent visual survey")
    status = survey.get("status")
    valid = {
        "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS",
        "UNKNOWN_BOUNDED_VISUAL_DECODE",
        "UNKNOWN_INSUFFICIENT_VISUAL_SAMPLING",
        "UNKNOWN_VISUAL_PEAK_BUDGET",
    }
    duration = survey.get("duration_seconds")
    coverage = survey.get("sampled_coverage_seconds")
    unsampled = survey.get("unsampled_duration_seconds")
    windows = survey.get("sampled_windows")
    if (status not in valid
            or any(type(x) not in (int, float) or not math.isfinite(x)
                   for x in (duration, coverage, unsampled))
            or not 0 < duration <= 86400
            or not 0 <= coverage <= duration + .05
            or not 0 <= unsampled <= duration + .05
            or abs(coverage + unsampled - duration) > .1
            or not isinstance(windows, list) or not 0 <= len(windows) <= 6
            or (status == "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
                and not windows)
            or any(not isinstance(w, list) or len(w) != 2
                   or any(type(t) not in (int, float) or not math.isfinite(t)
                          for t in w)
                   or not 0 <= w[0] < w[1] <= duration + .05 for w in windows)):
        raise ValueError("Invalid independent visual survey coverage")
    # The independently recorded coverage must agree with the windows
    # actually decoded; planned/unopened windows cannot imply clean video.
    measured_coverage = 0.
    prior_right = 0.
    prior_start = -1.
    for left, right in windows:
        if left < prior_start:
            raise ValueError("Unsorted output-only sampled windows")
        measured_coverage += max(0., right - max(left, prior_right))
        prior_right = max(prior_right, right)
        prior_start = left
    if abs(measured_coverage - coverage) > .06:
        raise ValueError("Visual survey coverage exceeds actually scanned windows")
    result["output_only_visual_status"] = status
    result["output_only_visual_coverage_seconds"] = coverage
    count = survey.get("observed_visual_peaks")
    unmatched = survey.get("without_acoustic_candidate")
    peaks = survey.get("observed_peaks")
    if status == "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS":
        if (type(count) is not int or type(unmatched) is not int
                or not 0 <= unmatched <= count <= 18
                or not isinstance(peaks, list) or len(peaks) != count
                or sum(p.get("relation") == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME"
                       for p in peaks if isinstance(p, dict)) != unmatched
                or any(not isinstance(p, dict)
                       or p.get("verified_edit_cut") is not False
                       or p.get("source_scene_change_ruled_out") is not False
                       or p.get("relation") not in {
                           "OVERLAPS_ACOUSTIC_CANDIDATE",
                           "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
                       }
                       or not isinstance(p.get("time_bracket_seconds"), list)
                       or len(p["time_bracket_seconds"]) != 2
                       or any(type(t) not in (int, float) or not math.isfinite(t)
                              for t in p["time_bracket_seconds"])
                       or not any(w[0] - .05 <= p["time_bracket_seconds"][0]
                                  < p["time_bracket_seconds"][1] <= w[1] + .05
                                  for w in windows)
                       for p in peaks)):
            raise ValueError("Forged or inconsistent output-only visual counts")
        result["output_only_visual_change_count"] = count
        result["output_only_visual_without_acoustic_count"] = unmatched
    elif count is not None or unmatched is not None:
        raise ValueError("Unknown visual survey cannot assert zero measured peaks")
    return result



def _describe_source_visual_checks(cut_row: dict, side_row: dict) -> dict:
    """Only same-identity source-side evidence, never verified cut causation."""
    result = {
        "visual_only_search_status": "NOT_MEASURED_OR_UNVERIFIED",
        "visual_only_source_skip_hypotheses": None,
        "visual_only_source_continuity_hypotheses": None,
        "visual_only_source_group_switch_hypotheses": None,
        "visual_only_unknown_correspondences": None,
        "source_visual_check_status": "NOT_MEASURED_OR_UNVERIFIED",
        "source_visual_change_also_observed": None,
        "source_visual_no_isolated_change_observed": None,
        "source_visual_unknown_correspondence": None,
    }
    source = side_row.get("independent_visual_source_checks")
    if source is None:
        return result
    if (not isinstance(source, dict)
            or side_row.get("source_visual_method_version") != SOURCE_VISUAL_VERSION
            or source.get("version") != SOURCE_VISUAL_VERSION
            or any(source.get(flag) is not False for flag in (
                "verified_edit_cut", "source_scene_change_ruled_out",
                "original_video_absolute_pts_verified",
                "reviewed_gold_available", "qualification_allowed",
            ))):
        raise ValueError("Unsupported or promoted source visual observations")
    status = source.get("status")
    checked = source.get("checked_visual_peaks")
    seen = source.get("source_change_also_observed")
    absent = source.get("source_no_isolated_change_observed")
    unknown = source.get("unknown_source_correspondence")
    observations = source.get("peak_observations")
    if (status not in {
            "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY",
            "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET",
            "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS",
        } or any(type(v) is not int or not 0 <= v <= 18
                 for v in (checked, seen, absent, unknown))
            or not isinstance(observations, list)
            or len(observations) != checked
            or seen + absent + unknown != (
                checked if status == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS"
                else unknown
            )):
        raise ValueError("Contradictory original/finished visual statistics")
    survey = cut_row.get("independent_output_visual_survey", {})
    output_peaks = survey.get("observed_peaks", []) if isinstance(survey, dict) else []
    unmatched = [
        p["time_bracket_seconds"] for p in output_peaks
        if isinstance(p, dict)
        and p.get("relation") == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME"
    ]
    if status == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS":
        if checked != len(unmatched):
            raise ValueError("Source visual checks do not cover output-only peaks")
        values = {
            "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED",
            "SOURCE_NO_ISOLATED_CHANGE_OBSERVED",
            "UNKNOWN_NO_TWO_SIDED_AUDIO_ANCHORS",
            "UNKNOWN_SOURCE_VISUAL_DECODE",
        }
        if any(
            not isinstance(record, dict)
            or record.get("output_interval") != output
            or record.get("status") not in values
            or record.get("verified_edit_cut") is not False
            or record.get("source_scene_change_ruled_out") is not False
            for record, output in zip(observations, unmatched)
        ) or (
            seen != sum(p["status"] == "SOURCE_VISUAL_CHANGE_ALSO_OBSERVED"
                        for p in observations)
            or absent != sum(
                p["status"] == "SOURCE_NO_ISOLATED_CHANGE_OBSERVED"
                for p in observations
            )
        ):
            raise ValueError("Inconsistent linked output/source peak identities")
        result.update({
            "source_visual_change_also_observed": seen,
            "source_visual_no_isolated_change_observed": absent,
            "source_visual_unknown_correspondence": unknown,
        })
    elif (checked or seen or absent or observations
          or (status == "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY" and unknown)
          or (status == "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET"
              and unknown != len(unmatched))):
        raise ValueError("Unknown original visual survey was promoted")
    result["source_visual_check_status"] = status
    return result



def _describe_visual_only_frame_search(cut_row: dict, side_row: dict) -> dict:
    """Strictly descriptive cross-video image-sequence hypotheses."""
    result = {
        "visual_only_search_status": "NOT_MEASURED_OR_UNVERIFIED",
        "visual_only_source_skip_hypotheses": None,
        "visual_only_source_continuity_hypotheses": None,
        "visual_only_source_group_switch_hypotheses": None,
        "visual_only_unknown_correspondences": None,
    }
    raw = side_row.get("visual_only_frame_search")
    if raw is None:
        return result
    if (not isinstance(raw, dict)
            or side_row.get("visual_only_frame_search_version") != VISUAL_SEARCH_VERSION
            or raw.get("version") != VISUAL_SEARCH_VERSION
            or any(raw.get(flag) is not False for flag in (
                "verified_edit_cut", "source_scene_change_ruled_out",
                "frame_accurate_cuts_verified", "original_video_absolute_pts_verified",
                "dense_output_source_map_verified",
                "reviewed_gold_available", "qualification_allowed",
            ))):
        raise ValueError("Promoted visual-only sequence correspondence")
    status = raw.get("status")
    measured = "MEASURED_UNREVIEWED_VISUAL_SEQUENCE_SEARCH"
    valid = {
        measured, "UNKNOWN_NO_MEASURED_OUTPUT_SURVEY",
        "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET",
        "NO_UNMATCHED_OUTPUT_VISUAL_PEAKS",
        "UNKNOWN_NO_PINNED_SOURCE_SECTIONS",
        "UNKNOWN_VISUAL_FRAME_COVERAGE",
        "UNKNOWN_VISUAL_SEARCH_COMPARISON_BUDGET",
        "UNKNOWN_VISUAL_IMAGE_DECODE",
    }
    observations = raw.get("peak_observations")
    counts = [
        raw.get(key) for key in (
            "visual_source_skip_hypotheses",
            "visual_source_continuity_hypotheses",
            "visual_source_group_switch_hypotheses",
            "unknown_visual_correspondences",
        )
    ]
    if (status not in valid or not isinstance(observations, list)
            or len(observations) > 3
            or any(type(n) is not int or not 0 <= n <= 18 for n in counts)
            or any(type(raw.get(k)) is not int or not 0 <= raw[k] <= limit
                   for k, limit in (
                       ("tested_image_comparisons", 2400),
                       ("tested_source_frame_samples", 100),
                       ("tested_finished_frame_samples", 12),
                   ))):
        raise ValueError("Impossible bounded visual-only correspondence counts")
    survey = cut_row.get("independent_output_visual_survey")
    unmatched = [
        p["time_bracket_seconds"] for p in survey.get("observed_peaks", [])
        if p.get("relation") == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME"
    ] if isinstance(survey, dict) else []
    if status == measured:
        if (sum(counts) != len(observations)
                or len(observations) != len(unmatched)
                or len(observations) > 3):
            raise ValueError("Search observations disagree with output visual peaks")
        statuses = {
            "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS",
            "VISUAL_ONLY_CONTINUITY_COMPATIBLE",
            "VISUAL_ONLY_GROUP_SWITCH_HYPOTHESIS",
            "UNKNOWN_NO_TWO_SIDED_FINISHED_FRAMES",
            "UNKNOWN_VISUAL_MATCH_NOT_UNIQUE",
            "UNKNOWN_VISUAL_SOURCE_REORDERING",
        }
        if (any(not isinstance(entry, dict)
                or entry.get("output_interval") != interval
                or entry.get("status") not in statuses
                or entry.get("editorial_gold") is not False
                or entry.get("verified_edit_cut") is not False
                for entry, interval in zip(observations, unmatched))
                or counts[:3] != [
                    sum(p["status"] == name for p in observations)
                    for name in (
                        "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS",
                        "VISUAL_ONLY_CONTINUITY_COMPATIBLE",
                        "VISUAL_ONLY_GROUP_SWITCH_HYPOTHESIS",
                    )
                ]
                or counts[3] != sum(
                    p["status"].startswith("UNKNOWN_") for p in observations
                )):
            raise ValueError("Forged or misattributed visual-only hypothesis")
        result.update({
            "visual_only_source_skip_hypotheses": counts[0],
            "visual_only_source_continuity_hypotheses": counts[1],
            "visual_only_source_group_switch_hypotheses": counts[2],
            "visual_only_unknown_correspondences": counts[3],
        })
    elif (observations or any(counts[:3])
          or (status == "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET"
              and counts[3] != len(unmatched))
          or (status != "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET" and counts[3]
              not in (0, len(unmatched)))):
        raise ValueError("Unknown visual-only search fabricated completed links")
    result["visual_only_search_status"] = status
    return result


def describe_real_cut_observations(
    cut_row: dict, side_row: dict | None = None, *,
    maximum_bracket_seconds: float = 8.,
) -> dict:
    """Summarize a single stage-confirmed clip; never turn absence into gold.

    Unknown audio changes and unconfirmed source-side evidence are reported
    separately. Provenance mismatches make side agreement unavailable.
    """
    if (type(maximum_bracket_seconds) not in (int, float)
            or not math.isfinite(maximum_bracket_seconds)
            or not 0 < maximum_bracket_seconds <= 12):
        raise ValueError("Invalid descriptive bracket threshold")
    result = {
        "version": VERSION, "status": "UNVERIFIED_CUT_MEASUREMENT",
        "reference_kind": "NO_INDEPENDENT_EDITOR_REFERENCE",
        **_NO_GOLD,
        "cut_candidate_count": None, "unknown_audio_transition_count": None,
        "unknown_audio_transition_statuses": None,
        "overbroad_candidate_count": None,
        "candidate_median_width_seconds": None,
        "candidate_p90_width_seconds": None,
        "output_visual_corroborating_candidates": None,
        "two_sided_multimodal_candidates": None,
        "two_sided_unresolved_candidates": None,
        "two_sided_status": "NOT_MEASURED_OR_UNVERIFIED",
        "source_visual_check_status": "NOT_MEASURED_OR_UNVERIFIED",
        "source_visual_change_also_observed": None,
        "source_visual_no_isolated_change_observed": None,
        "source_visual_unknown_correspondence": None,
        "output_only_visual_status": "NOT_MEASURED_OR_UNVERIFIED",
        "output_only_visual_change_count": None,
        "output_only_visual_without_acoustic_count": None,
        "output_only_visual_coverage_seconds": None,
    }
    if not _safe_unreviewed(cut_row) or cut_row.get("status") != _MEASURED:
        return result
    refinement = cut_row.get("refinement")
    if (not isinstance(refinement, dict)
            or refinement.get("version") != ACOUSTIC_VERSION
            or refinement.get("status") not in {
                "AUDIO_SUPPORTED_INTERVALS", "UNKNOWN_NO_UNIQUE_POINTS",
                "UNKNOWN_SAMPLING_BUDGET", "UNKNOWN_NO_SUPPORTED_RANGES",
            }):
        return result
    # Output-only survey is independently measured even if acoustic refinement
    # could not produce uniquely grounded cut candidate intervals.
    result.update(_describe_independent_visual_survey(cut_row))
    # Exhausted budget / missing unique anchors is *not* an observed
    # zero-cut clip. Preserve unknown counts as null until audio was supported.
    if refinement["status"] != "AUDIO_SUPPORTED_INTERVALS":
        result["status"] = "UNKNOWN_ACOUSTIC_SUPPORT"
        result["measurement_status"] = refinement["status"]
        result["clip_id"] = cut_row.get("clip_id")
        return result
    changes = refinement.get("transitions")
    if not isinstance(changes, list) or len(changes) > MAX_TRANSITIONS:
        raise ValueError("Malformed or excessive acoustic transitions")
    candidates = []
    unknowns = Counter()
    for transition in changes:
        if not isinstance(transition, dict):
            raise ValueError("Malformed acoustic transition")
        lo, hi = _interval(transition.get("interval"))
        status = transition.get("status")
        if status == "CUT_INTERVAL_CANDIDATE":
            candidates.append((transition, lo, hi))
        elif status in _UNKNOWN:
            unknowns[status] += 1
        else:
            raise ValueError("Unexpected acoustic transition state")
    widths = sorted(round(hi - lo, 4) for _, lo, hi in candidates)
    result.update({
        "status": "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS",
        "clip_id": cut_row.get("clip_id"),
        "measurement_status": refinement["status"],
        "cut_candidate_count": len(candidates),
        "unknown_audio_transition_count": sum(unknowns.values()),
        "unknown_audio_transition_statuses": dict(sorted(unknowns.items())),
        "overbroad_candidate_count": sum(
            width > maximum_bracket_seconds for width in widths
        ),
        "candidate_median_width_seconds": (
            round(statistics.median(widths), 4) if widths else None
        ),
        "candidate_p90_width_seconds": (
            widths[math.ceil(.9 * len(widths)) - 1] if widths else None
        ),
        "maximum_bracket_seconds": float(maximum_bracket_seconds),
    })

    visual = cut_row.get("output_visual_cut_evidence")
    if isinstance(visual, list):
        if not candidates and not visual:
            result["output_visual_corroborating_candidates"] = 0
        elif len(visual) == len(candidates) and all(
            isinstance(item, dict)
            and item.get("acoustic_interval") == transition["interval"]
            and type(transition.get("offset_change_seconds")) in (int, float)
            and math.isfinite(transition["offset_change_seconds"])
            and item.get("acoustic_offset_change_seconds")
                == transition["offset_change_seconds"]
            and item.get("verified_edit_cut") is False
            and item.get("source_visual_transition_ruled_out") is False
            and item.get("native_frame_pts_verified") is False
            and isinstance(item.get("visual_result"), dict)
            and item["visual_result"].get("version")
                == "bounded-output-visual-jumps-v1"
            and item["visual_result"].get("status") in {
                "VISUAL_JUMP_CANDIDATES", "UNKNOWN_NO_ISOLATED_JUMP",
                "UNKNOWN_INSUFFICIENT_FRAMES", "UNKNOWN_BRACKET_TOO_WIDE",
            }
            and item.get("relation") == (
                "CORROBORATING_OUTPUT_CHANGE"
                if item["visual_result"]["status"] == "VISUAL_JUMP_CANDIDATES"
                else "UNRESOLVED_VISUAL_EVIDENCE"
            )
            for item, (transition, _, _) in zip(visual, candidates)
        ):
            result["output_visual_corroborating_candidates"] = sum(
                item["relation"] == "CORROBORATING_OUTPUT_CHANGE"
                for item in visual
            )

    if side_row is None:
        return result
    if (not _safe_unreviewed(side_row)
            or side_row.get("status") != _SIDE_MEASURED
            or not isinstance(side_row.get("clip_id"), str)
            or side_row["clip_id"] != cut_row.get("clip_id")
            or not all(
                isinstance(cut_row.get(key), str) and cut_row[key]
                and side_row.get(key) == cut_row[key]
                for key in ("source_stream_id", "audio_source_sha256",
                            "finished_media_sha256")
            )):
        result["two_sided_status"] = "UNVERIFIED_CROSS_STAGE_PROVENANCE"
        return result

    result.update(_describe_source_visual_checks(cut_row, side_row))
    result.update(_describe_visual_only_frame_search(cut_row, side_row))
    sides = side_row.get("transitions")
    if not isinstance(sides, list):
        result["two_sided_status"] = "INVALID_SIDE_SCHEMA"
    elif (len(sides) == 1 and isinstance(sides[0], dict)
          and sides[0].get("status") == "UNKNOWN_TRANSITION_BUDGET"):
        result["two_sided_status"] = "UNKNOWN_TRANSITION_BUDGET"
    elif len(sides) != len(candidates) or any(
        not isinstance(side, dict)
        or side.get("acoustic_interval") != transition["interval"]
        or side.get("editorial_gold") is not False
        or not isinstance(side.get("candidate"), dict)
        or side["candidate"].get("verified_edit_cut") is not False
        or not isinstance(side["candidate"].get("status"), str)
        or side["candidate"]["status"] not in {
            "MULTIMODAL_EDIT_JUMP_CANDIDATE",
            "UNKNOWN_INDEPENDENT_VISUAL_SUPPORT",
            "UNKNOWN_UNSUPPORTED_ACOUSTIC_INTERVAL",
            "UNKNOWN_OUTPUT_VISUAL_JUMP", "UNKNOWN_SOURCE_GROUP",
            "UNKNOWN_SECTION_CLOCK", "UNKNOWN_MISSING_SIDE_AUDIO_ANCHOR",
            "UNKNOWN_SOURCE_SEEK_OUTSIDE_SECTION",
            "UNKNOWN_SOURCE_CONTINUITY", "UNKNOWN_REORDERED_SOURCE",
            "UNKNOWN_BOUNDED_FRAME_DECODE",
            "UNKNOWN_ONE_OR_BOTH_SIDES_UNVERIFIED",
        }
        for side, (transition, _, _) in zip(sides, candidates)
    ):
        result["two_sided_status"] = "UNVERIFIED_SIDE_INTERVAL_LINKS"
    else:
        supported = sum(
            side["candidate"]["status"] == "MULTIMODAL_EDIT_JUMP_CANDIDATE"
            for side in sides
        )
        result["two_sided_status"] = "MEASURED_UNREVIEWED"
        result["two_sided_multimodal_candidates"] = supported
        result["two_sided_unresolved_candidates"] = len(sides) - supported
    return result


def aggregate_real_cut_observations(rows: dict[str, dict]) -> dict:
    """Aggregate only schema-checked observations; null is never zero."""
    measured = [
        row for row in rows.values()
        if row.get("status") == "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS"
    ]
    side = [
        row for row in measured if row["two_sided_status"] == "MEASURED_UNREVIEWED"
    ]
    return {
        "version": VERSION, **_NO_GOLD,
        "described_clips": len(measured),
        "unverified_clips": len(rows) - len(measured),
        "cut_candidates": sum(x["cut_candidate_count"] for x in measured),
        "unknown_audio_transitions": sum(
            x["unknown_audio_transition_count"] for x in measured
        ),
        "overbroad_candidates": sum(x["overbroad_candidate_count"] for x in measured),
        "clips_with_verified_side_report_provenance": len(side),
        "two_sided_multimodal_candidates": (
            sum(x["two_sided_multimodal_candidates"] for x in side)
            if len(side) == len(measured) and measured else None
        ),
        "clips_with_measured_output_only_visual_survey": sum(
            x.get("output_only_visual_change_count") is not None
            for x in rows.values()
        ),
        "output_only_visual_without_acoustic_candidates": sum(
            x.get("output_only_visual_without_acoustic_count") or 0
            for x in rows.values()
        ),
        "clips_with_measured_source_visual_crosscheck": sum(
            x.get("source_visual_change_also_observed") is not None
            for x in rows.values()
        ),
        "source_visual_changes_also_observed": sum(
            x.get("source_visual_change_also_observed") or 0
            for x in rows.values()
        ),
        "source_no_isolated_change_observed": sum(
            x.get("source_visual_no_isolated_change_observed") or 0
            for x in rows.values()
        ),
        "clips_with_measured_visual_only_frame_search": sum(
            x.get("visual_only_source_skip_hypotheses") is not None
            for x in rows.values()
        ),
        "visual_only_source_skip_hypotheses": sum(
            x.get("visual_only_source_skip_hypotheses") or 0
            for x in rows.values()
        ),
        "visual_only_source_group_switch_hypotheses": sum(
            x.get("visual_only_source_group_switch_hypotheses") or 0
            for x in rows.values()
        ),
        "human_editor_accuracy_available": False,
    }
