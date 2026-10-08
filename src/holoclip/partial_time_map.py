"""Non-authoritative evidence-constrained partial OutputSourceTimeMap diagnostics.

The entries are explicitly *local interpolation candidates* between two
measured source↔output frames, corroborated by one independently unique
audio anchor. They are never frame-accurate, complete, or training gold.
"""
from __future__ import annotations

import math

VERSION = "partial-audio-visual-output-source-map-v1"


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def construct_partial_time_map(
    group_observations: list[dict], audio_anchors: list[dict],
    acoustic_transitions: list[dict], *, finished_duration: float,
    max_pair_gap: float = 3., maximum_offset_drift: float = .5,
    max_time_slope_delta: float = .15, max_pairs: int = 160,
) -> dict:
    """Propose only short *supported* local source/output time intervals.

    No extrapolation beyond observed frames and no interpolation across any
    measured edit-cut/unknown-acoustic transition. A source time range is
    audio-relative and approximate, not verified native original frame PTS.
    """
    if (not _finite(finished_duration) or finished_duration <= 0
            or not 1 <= max_pair_gap <= 4. or not 0 < maximum_offset_drift <= 1.
            or not 0 < max_time_slope_delta <= .25 or not 1 <= max_pairs <= 320
            or len(group_observations) > 5 or len(audio_anchors) > 300
            or len(acoustic_transitions) > 200):
        raise ValueError("Invalid time-map evidence or work budget")
    result = {
        "version": VERSION, "status": "UNKNOWN_NO_SUPPORTED_LOCAL_INTERVALS",
        "segments": [], "unknown_intervals": [],
        "qualification_allowed": False, "frame_accurate_cuts_verified": False,
        "source_absolute_video_pts_verified": False,
        "dense_output_source_map_verified": False, "reviewed_gold_available": False,
        "interpolation_verified": False,
        "assumptions": [
            "Only short intervals BETWEEN two sampled source/finished frame matches are proposed",
            "Video section was re-encoded; source timing derives from an approximate audio clock",
            "Unique acoustic anchor confirms two image timings, not unseen intermediate frames",
            "This is NOT a frame-accurate or deployable OutputSourceTimeMap",
        ],
    }
    transitions = []
    for transition in acoustic_transitions:
        interval = transition.get("interval")
        if not isinstance(interval, list) or len(interval) != 2 or not all(
            _finite(v) for v in interval
        ) or not 0 <= interval[0] < interval[1] <= finished_duration + .5:
            raise ValueError("Acoustic transition interval has invalid timing")
        transitions.append(interval)
    anchors = []
    for anchor in audio_anchors:
        try:
            cs, ce = anchor["clip_start"], anchor["clip_end"]
            ss, se = anchor["source_start"], anchor["source_end"]
            cor, mar = anchor["correlation"], anchor["uniqueness_margin"]
        except (KeyError, TypeError) as error:
            raise ValueError("Missing audio-anchor provenance") from error
        if (not all(_finite(v) for v in (cs, ce, ss, se, cor, mar))
                or not 0 <= cs < ce <= finished_duration + .25
                or not 0 <= ss < se
                or abs((ce - cs) - (se - ss)) > .6
                or not .79 <= cor <= 1.01 or not .065 <= mar <= 1.01):
            raise ValueError("Anchor is not independently unique and bounded")
        anchors.append(anchor)
    rejected = {
        "unreviewed_visual_status": 0, "invalid_sample": 0, "no_audio_support": 0,
        "wide_or_unobserved_gap": 0, "intersects_cut_or_unknown": 0,
        "time_or_speed_disagreement": 0, "duplicate_source_frames": 0,
        "overlapping_group_links": 0,
    }
    proposed = []
    count = 0
    for observation in group_observations:
        if observation.get("status") != "PARTIAL_AUDIO_VISUAL_FRAME_LINKS":
            rejected["unreviewed_visual_status"] += 1
            continue
        if (observation.get("original_absolute_frame_pts_verified") is not False
                or observation.get("frame_accurate_cuts_verified") is not False
                or observation.get("dense_output_source_map_verified") is not False):
            rejected["unreviewed_visual_status"] += 1
            continue
        points = observation.get("frame_links")
        group = observation.get("group")
        if not isinstance(group, int) or not isinstance(points, list) or len(points) > 64:
            raise ValueError("Invalid group/frame provenance")
        links = []
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("Invalid sampled frame link")
            try:
                x = point["finished_clip_time_estimate_seconds"]
                y = point["source_audio_clock_estimate_seconds"]
                expected = point["expected_source_from_audio_seconds"]
                resid = point["audio_visual_residual_seconds"]
                visual = point["score"]
                n = point["orb_inliers"]
                src = point["source_frame_id"]
                dst = point["finished_frame_id"]
            except KeyError as error:
                raise ValueError("Sampled frame link missing measured IDs") from error
            if (not all(_finite(v) for v in (x, y, expected, resid, visual))
                    or not 0 <= x <= finished_duration or y < 0
                    or abs(y - expected - resid) > .03
                    or abs(resid) > 2.01 or not .25 <= visual <= 1.01
                    or not isinstance(n, int) or n < 12
                    or not isinstance(src, str) or not src
                    or not isinstance(dst, str) or not dst):
                rejected["invalid_sample"] += 1
                # One malformed observation invalidates its complete group.
                links = []
                break
            links.append(point)
        if not links:
            continue
        links.sort(key=lambda p: p["finished_clip_time_estimate_seconds"])
        for a, b in zip(links, links[1:]):
            count += 1
            if count > max_pairs:
                return {**result, "status": "UNKNOWN_PAIR_BUDGET",
                        "unknown_intervals": [{"start": 0., "end": finished_duration,
                                               "reason": "PAIR_BUDGET_EXHAUSTED"}],
                        "rejected": rejected, "tested_pairs": count}
            t0, t1 = a["finished_clip_time_estimate_seconds"], b["finished_clip_time_estimate_seconds"]
            s0, s1 = a["source_audio_clock_estimate_seconds"], b["source_audio_clock_estimate_seconds"]
            dt, ds = t1 - t0, s1 - s0
            if dt < .45 or dt > max_pair_gap or ds <= 0:
                rejected["wide_or_unobserved_gap"] += 1
                continue
            if a["source_frame_id"] == b["source_frame_id"]:
                rejected["duplicate_source_frames"] += 1
                continue
            if any(t0 < upper and lower < t1 for lower, upper in transitions):
                rejected["intersects_cut_or_unknown"] += 1
                continue
            offset0, offset1 = s0 - t0, s1 - t1
            if (abs(offset1 - offset0) > maximum_offset_drift
                    or abs(ds / dt - 1.) > max_time_slope_delta):
                rejected["time_or_speed_disagreement"] += 1
                continue
            supported = [
                anchor for anchor in anchors
                if anchor["clip_start"] - .05 <= t0 <= t1 <= anchor["clip_end"] + .05
                and abs((anchor["source_start"] - anchor["clip_start"]) - offset0) <= 1.
                and abs((anchor["source_start"] - anchor["clip_start"]) - offset1) <= 1.
            ]
            if not supported:
                rejected["no_audio_support"] += 1
                continue
            proposed.append({
                "group": group, "status": "LOCAL_INTERPOLATION_CANDIDATE",
                "output_start": round(t0, 3), "output_end": round(t1, 3),
                "approx_source_start": round(s0, 3), "approx_source_end": round(s1, 3),
                "approx_source_rate": round(ds / dt, 5),
                "source_frame_ids": [a["source_frame_id"], b["source_frame_id"]],
                "finished_frame_ids": [a["finished_frame_id"], b["finished_frame_id"]],
                "audio_anchor_support": len(supported),
                "max_audio_visual_residual_seconds": round(max(
                    abs(a["audio_visual_residual_seconds"]),
                    abs(b["audio_visual_residual_seconds"]),
                ), 3),
                "unobserved_intermediate_frames": True,
                "valid_for_edit_execution": False,
                "valid_for_training_gold": False,
            })
    proposed.sort(key=lambda r: (r["output_start"], r["group"], r["output_end"]))
    accepted = []
    for item in proposed:
        if accepted and item["output_start"] < accepted[-1]["output_end"] - .001:
            # Overlapping claims from distinct windows cannot both be trusted.
            if item["group"] != accepted[-1]["group"]:
                accepted.pop()
                rejected["overlapping_group_links"] += 2
            else:
                rejected["overlapping_group_links"] += 1
            continue
        accepted.append(item)
    current = 0.
    unknown = []
    for segment in accepted:
        if segment["output_start"] > current + .001:
            unknown.append({
                "start": round(current, 3), "end": segment["output_start"],
                "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT",
            })
        current = max(current, segment["output_end"])
    if current < finished_duration - .001:
        unknown.append({
            "start": round(current, 3), "end": round(finished_duration, 3),
            "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT",
        })
    # Unknown spans can contain candidate cuts; never imply continuity through
    # missing measurements. This diagnostic may be 100% unknown.
    duration = sum(x["output_end"] - x["output_start"] for x in accepted)
    result.update({
        "status": ("PARTIAL_LOCAL_INTERVAL_CANDIDATES" if accepted
                   else "UNKNOWN_NO_SUPPORTED_LOCAL_INTERVALS"),
        "segments": accepted, "unknown_intervals": unknown,
        "candidate_coverage_seconds": round(duration, 3),
        "candidate_coverage_fraction": round(duration / finished_duration, 5),
        "tested_pairs": count, "rejected": rejected,
    })
    return result
