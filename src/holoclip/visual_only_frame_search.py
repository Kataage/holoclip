"""Audio-offset-independent, bounded image-sequence search in pinned source sections.

The source sections are still reserved by the upstream policy/audio pipeline.
Inside those sections this search uses ONLY image correspondence; no local
audio offset or section AV clock is needed to choose source frame candidates.
A sparse visual link cannot establish an editor cut, accurate native PTS,
complete media coverage, or an original-stream source event identity.
"""
from __future__ import annotations

import math

from holoclip.visual_edit_evidence import _compare_features, _orb_features
from holoclip.visual_only_survey import VERSION as SURVEY_VERSION

VERSION = "bounded-visual-only-frame-search-v1"
MAX_PEAKS = 3
MAX_GROUPS = 5
MAX_SOURCE_FRAMES_PER_GROUP = 20
MAX_FINISHED_FRAMES_PER_SIDE = 2
MAX_COMPARISONS = 2400

_NO_GOLD = {
    "verified_edit_cut": False,
    "source_scene_change_ruled_out": False,
    "frame_accurate_cuts_verified": False,
    "original_video_absolute_pts_verified": False,
    "dense_output_source_map_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
}


def _real(x):
    return type(x) in (int, float) and math.isfinite(x)


def _sample(frames: list, count: int) -> list:
    if len(frames) <= count:
        return frames
    return [frames[(i * (len(frames) - 1)) // (count - 1)]
            for i in range(count)]


def _side_frames(frames: list, interval: list[float], side: str) -> list:
    if side == "left":
        candidates = sorted(
            (f for t, f in frames if interval[0] - 4 <= t <= interval[0] - .10),
            key=lambda f: f[0], reverse=True,
        )
    else:
        candidates = sorted(
            (f for t, f in frames if interval[1] + .10 <= t <= interval[1] + 4),
            key=lambda f: f[0],
        )
    chosen = []
    for t, frame in candidates:
        if not chosen or abs(t - chosen[0][0]) >= .45:
            chosen.append((t, frame))
        if len(chosen) == MAX_FINISHED_FRAMES_PER_SIDE:
            break
    return sorted(chosen, key=lambda x: x[0])


def _match_side(side_frames: list, originals: list, source_features: dict,
                finished_features: dict, compare) -> dict:
    matches = []
    for finished_time, finished in side_frames:
        scored = []
        for group, source in originals:
            geometry = compare(source_features[source.id],
                               finished_features[finished.id])
            if geometry is not None and geometry.get("score", 0) >= .25:
                scored.append((geometry["score"], group, source))
        scored.sort(key=lambda r: (-r[0], r[1], r[2].pts_seconds, r[2].id))
        if not scored:
            return {"status": "UNKNOWN_NO_VISUAL_IMAGE_MATCH"}
        score, group, source = scored[0]
        distant = [row[0] for row in scored[1:]
                   if row[1] != group or
                   abs(row[2].pts_seconds - source.pts_seconds) > 1.]
        if distant and score - max(distant) < .09:
            return {"status": "UNKNOWN_REPEATED_OR_AMBIGUOUS_SOURCE"}
        matches.append((finished_time, group, source.pts_seconds, score,
                        source.checksum, finished.checksum))
    # Two timestamps showing the same pixel content are not independent
    # evidence of a changing temporal sequence (static slides/game HUDs).
    if (len(matches) != 2
            or matches[0][4] == matches[1][4]
            or matches[0][5] == matches[1][5]
            or matches[0][1] != matches[1][1]
            or matches[1][0] - matches[0][0] < .45
            or matches[1][2] - matches[0][2] < .4
            or abs((matches[1][2] - matches[0][2])
                   - (matches[1][0] - matches[0][0])) > .9):
        return {"status": "UNKNOWN_NO_UNIQUE_ADVANCING_SEQUENCE"}
    return {
        "status": "SPARSE_UNREVIEWED_IMAGE_SEQUENCE",
        "group": matches[0][1],
        "source_local_samples_seconds": [round(x[2], 3) for x in matches],
        "finished_samples_seconds": [round(x[0], 3) for x in matches],
        "sampled_visual_scores": [round(x[3], 5) for x in matches],
    }


def search_source_visual_sequences(
    survey: dict, sections: list[dict], *,
    load_window, compare=_compare_features, extract=_orb_features,
) -> dict:
    """Use already-verified source/finished frame manifests with no new download.

    The caller must recheck rights, source URLs, split, SHA256 and measured
    manifests before invoking. The loaded windows have local, *not global*
    source-video timestamps and limited visual coverage.
    """
    result = {
        "version": VERSION, "status": "UNKNOWN_NO_MEASURED_OUTPUT_SURVEY",
        "peak_observations": [], "tested_image_comparisons": 0,
        "tested_source_frame_samples": 0, "tested_finished_frame_samples": 0,
        "visual_source_skip_hypotheses": 0,
        "visual_source_continuity_hypotheses": 0,
        "visual_source_group_switch_hypotheses": 0,
        "unknown_visual_correspondences": 0, **_NO_GOLD,
    }
    if (not isinstance(survey, dict) or survey.get("version") != SURVEY_VERSION
            or survey.get("status") != "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS"
            or any(survey.get(k) is not False for k in (
                "verified_edit_cut", "reviewed_gold_available",
                "frame_accurate_cuts_verified", "qualification_allowed",
            ))):
        return result
    peaks = survey.get("observed_peaks")
    if (not isinstance(sections, list) or len(sections) > MAX_GROUPS
            or not isinstance(peaks, list) or len(peaks) > 18):
        raise ValueError("Unsafe image-sequence source search inputs")
    selected = []
    for point in peaks:
        if (not isinstance(point, dict)
                or point.get("verified_edit_cut") is not False
                or point.get("source_scene_change_ruled_out") is not False
                or point.get("relation") not in {
                    "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
                    "OVERLAPS_ACOUSTIC_CANDIDATE",
                }):
            raise ValueError("Promoted or malformed output-only visual change")
        interval = point.get("time_bracket_seconds")
        if (not isinstance(interval, list) or len(interval) != 2
                or any(not _real(v) for v in interval)
                or not 0 <= interval[0] < interval[1] <= 86400):
            raise ValueError("Invalid bounded visual interval")
        if point["relation"] == "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME":
            selected.append(interval)
    if len(selected) > MAX_PEAKS:
        result["status"] = "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET"
        result["unknown_visual_correspondences"] = len(selected)
        return result
    if not selected:
        result["status"] = "NO_UNMATCHED_OUTPUT_VISUAL_PEAKS"
        return result
    if not sections:
        result["status"] = "UNKNOWN_NO_PINNED_SOURCE_SECTIONS"
        result["unknown_visual_correspondences"] = len(selected)
        return result
    source_frames = []
    finished_frames = {}
    for group, section in enumerate(sections):
        if section.get("group") != group:
            raise ValueError("Source group order changed")
        src = load_window(section["source_measurement"])
        dst = load_window(section["finished_measurement"])
        source_frames.extend(
            (group, f) for f in _sample(
                sorted(src.frames, key=lambda x: (x.pts_seconds, x.id)),
                MAX_SOURCE_FRAMES_PER_GROUP,
            )
        )
        for f in dst.frames:
            if f.id not in finished_frames:
                finished_frames[f.id] = (
                    dst.source_start + f.pts_seconds, f
                )
    originals = sorted(source_frames, key=lambda p: (p[0], p[1].pts_seconds))
    destinations = sorted(finished_frames.values(), key=lambda p: (p[0], p[1].id))
    if len(originals) > MAX_GROUPS * MAX_SOURCE_FRAMES_PER_GROUP:
        raise ValueError("Source sample budget exceeded")
    result["tested_source_frame_samples"] = len(originals)
    if not originals or not destinations:
        result["status"] = "UNKNOWN_VISUAL_FRAME_COVERAGE"
        result["unknown_visual_correspondences"] = len(selected)
        return result
    pairs_by_peak = []
    for interval in selected:
        left = _side_frames(destinations, interval, "left")
        right = _side_frames(destinations, interval, "right")
        pairs_by_peak.append((left, right))
    total_finished = sum(len(a) + len(b) for a, b in pairs_by_peak)
    result["tested_finished_frame_samples"] = total_finished
    comparisons = len(originals) * total_finished
    if comparisons > MAX_COMPARISONS:
        result["status"] = "UNKNOWN_VISUAL_SEARCH_COMPARISON_BUDGET"
        result["unknown_visual_correspondences"] = len(selected)
        return result

    # Load each validated image once. Missing samples remain UNKNOWN for all
    # candidates, rather than partially promoting earlier convenient peaks.
    try:
        src_features = {frame.id: extract(frame.locator) for _, frame in originals}
        sampled_destinations = {
            frame.id: frame for left, right in pairs_by_peak
            for _, frame in left + right
        }
        dst_features = {key: extract(frame.locator)
                        for key, frame in sampled_destinations.items()}
    except (OSError, ValueError):
        result["status"] = "UNKNOWN_VISUAL_IMAGE_DECODE"
        result["unknown_visual_correspondences"] = len(selected)
        return result
    result["status"] = "MEASURED_UNREVIEWED_VISUAL_SEQUENCE_SEARCH"
    result["tested_image_comparisons"] = comparisons
    for interval, (left, right) in zip(selected, pairs_by_peak):
        if len(left) < 2 or len(right) < 2:
            verdict = "UNKNOWN_NO_TWO_SIDED_FINISHED_FRAMES"
            matched = []
        else:
            a = _match_side(left, originals, src_features, dst_features, compare)
            b = _match_side(right, originals, src_features, dst_features, compare)
            matched = [a, b]
            if (a["status"] != "SPARSE_UNREVIEWED_IMAGE_SEQUENCE"
                    or b["status"] != "SPARSE_UNREVIEWED_IMAGE_SEQUENCE"):
                verdict = "UNKNOWN_VISUAL_MATCH_NOT_UNIQUE"
            elif a["group"] != b["group"]:
                verdict = "VISUAL_ONLY_GROUP_SWITCH_HYPOTHESIS"
            else:
                delta = (b["source_local_samples_seconds"][0]
                         - a["source_local_samples_seconds"][-1])
                observed = (b["finished_samples_seconds"][0]
                            - a["finished_samples_seconds"][-1])
                if delta < 0:
                    verdict = "UNKNOWN_VISUAL_SOURCE_REORDERING"
                elif delta - observed > 1.:
                    verdict = "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS"
                else:
                    verdict = "VISUAL_ONLY_CONTINUITY_COMPATIBLE"
        result["peak_observations"].append({
            "output_interval": interval,
            "status": verdict,
            "sides": matched,
            "editorial_gold": False, "verified_edit_cut": False,
        })
        result["visual_source_skip_hypotheses"] += int(
            verdict == "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS"
        )
        result["visual_source_continuity_hypotheses"] += int(
            verdict == "VISUAL_ONLY_CONTINUITY_COMPATIBLE"
        )
        result["visual_source_group_switch_hypotheses"] += int(
            verdict == "VISUAL_ONLY_GROUP_SWITCH_HYPOTHESIS"
        )
        result["unknown_visual_correspondences"] += int(verdict.startswith("UNKNOWN_"))
    return result
