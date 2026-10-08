"""Compare real source/finished sampled frames against independent audio timing.

These are *sampled-frame links*, not original-stream native PTS, exact cuts,
a complete time map, editorial intent, or independently reviewed gold.
"""
from __future__ import annotations

import math

from holoclip.visual_edit_evidence import _compare_features, _orb_features

VERSION = "audio-conditioned-frame-links-v1"


def link_measured_frames(
    source_window, finished_window, clock: dict, *,
    audio_offset_seconds: float, max_frames: int = 64,
    max_comparisons: int = 4096, time_tolerance_seconds: float = 2.0,
    min_score: float = .25, ambiguity_margin: float = .09,
) -> dict:
    """Return auditable sparse frame links or explicit unknown states.

    The source window was re-encoded, so its local frame PTS must be interpreted
    relative to the *audio-calibrated section origin*, with unknown codec skew.
    Every source frame is examined for visual duplicates, even those outside
    the acoustic time neighborhood.
    """
    if (not math.isfinite(audio_offset_seconds)
            or not 1 <= max_frames <= 64 or not 1 <= max_comparisons <= 4096
            or not .5 <= time_tolerance_seconds <= 5.0):
        raise ValueError("Invalid bounded frame-link parameters")
    result = {
        "version": VERSION, "status": "UNKNOWN_CLOCK",
        "frame_links": [], "candidate_links": [], "rejected": {},
        "original_video_absolute_pts_verified": False,
        "finished_native_frame_pts_verified": False,
        "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "unknowns": [
            "Original source section is re-encoded before sampled frame measurement",
            "Source video/audio priming and drift are not independently calibrated",
            "Finished image sampling does not establish exact edited frame timing",
            "Repeated scenes, overlays, camera/game transitions and edits can collide",
        ],
    }
    if (clock.get("status") != "PARTIAL_SECTION_AUDIO_CLOCK"
            or clock.get("frame_pts_verified") is not True
            or clock.get("original_video_absolute_pts_verified") is not False
            or not isinstance(clock.get("origin_seconds"), (int, float))
            or not math.isfinite(clock["origin_seconds"])
            or clock["origin_seconds"] < 0):
        return result
    sources = sorted(source_window.frames, key=lambda f: f.pts_seconds)
    finished = sorted(finished_window.frames, key=lambda f: f.pts_seconds)
    if not sources or not finished:
        result["status"] = "UNKNOWN_MISSING_FRAMES"
        return result
    if (len(sources) > max_frames or len(finished) > max_frames
            or len(sources) * len(finished) > max_comparisons):
        result["status"] = "UNKNOWN_FRAME_PAIR_BUDGET"
        return result

    src_features = {f.id: _orb_features(f.locator) for f in sources}
    dst_features = {f.id: _orb_features(f.locator) for f in finished}
    rejected = {"no_visual_match": 0, "audio_visual_disagreement": 0,
                "repeated_visual_content": 0, "low_geometric_score": 0}

    candidates = []
    for frame in finished:
        clip_seconds = finished_window.source_start + frame.pts_seconds
        if not math.isfinite(clip_seconds):
            raise ValueError("Non-finite finished-video frame timestamp")
        expected_source_seconds = clip_seconds + audio_offset_seconds
        ranked = []
        for other in sources:
            # Re-encoded source *window* frame clock plus the measured
            # section audio origin is only an approximate original clock.
            estimated_source_seconds = (
                clock["origin_seconds"] + source_window.source_start + other.pts_seconds
            )
            geom = _compare_features(src_features[other.id], dst_features[frame.id])
            if geom is None or geom["score"] < min_score:
                continue
            ranked.append((geom["score"], other, estimated_source_seconds, geom))
        ranked.sort(key=lambda row: (-row[0], row[1].id))
        if not ranked:
            rejected["no_visual_match"] += 1
            continue
        within = [row for row in ranked
                  if abs(row[2] - expected_source_seconds) <= time_tolerance_seconds]
        if not within:
            rejected["audio_visual_disagreement"] += 1
            continue
        # A substantially stronger geometric match at a different original
        # source time contradicts the audio prediction. Do not cherry-pick the
        # weaker image that happens to fall inside the expected time window.
        if (ranked[0][1].id != within[0][1].id
                and ranked[0][0] - within[0][0] >= ambiguity_margin):
            rejected["audio_visual_disagreement"] += 1
            continue
        score, best, source_seconds, geometry = within[0]
        distant_competitors = [
            row[0] for row in ranked
            if row[1].id != best.id
            and abs(row[2] - source_seconds) > 1.0
        ]
        margin = (score - max(distant_competitors)) if distant_competitors else None
        if margin is not None and margin < ambiguity_margin:
            rejected["repeated_visual_content"] += 1
            continue
        if score < min_score:
            rejected["low_geometric_score"] += 1
            continue
        candidates.append({
            "source_frame_id": best.id, "finished_frame_id": frame.id,
            "source_shot_id": best.shot_id, "finished_shot_id": frame.shot_id,
            "source_window_pts_seconds": best.pts_seconds,
            "finished_window_pts_seconds": frame.pts_seconds,
            "finished_clip_time_estimate_seconds": round(clip_seconds, 3),
            "source_audio_clock_estimate_seconds": round(source_seconds, 3),
            "expected_source_from_audio_seconds": round(expected_source_seconds, 3),
            "audio_visual_residual_seconds": round(source_seconds - expected_source_seconds, 3),
            "visual_uniqueness_margin": round(margin, 5) if margin is not None else None,
            "distant_visual_alternatives_tested": bool(distant_competitors),
            **geometry,
        })

    # A single plausible image, or many images mapped to the same still frame,
    # cannot certify even a partial *temporal* trajectory.
    distinct = []
    for item in sorted(candidates, key=lambda x: x["finished_clip_time_estimate_seconds"]):
        if not distinct or (
            item["finished_clip_time_estimate_seconds"]
            - distinct[-1]["finished_clip_time_estimate_seconds"] >= .45
            and item["source_audio_clock_estimate_seconds"]
            - distinct[-1]["source_audio_clock_estimate_seconds"] >= .45
        ):
            distinct.append(item)
    status = "UNKNOWN_TOO_SPARSE"
    links = []
    if len(distinct) >= 2:
        offsets = [x["source_audio_clock_estimate_seconds"]
                   - x["finished_clip_time_estimate_seconds"] for x in distinct]
        if max(offsets) - min(offsets) <= 1.5 and all(
            next_item["source_audio_clock_estimate_seconds"] >
            curr["source_audio_clock_estimate_seconds"]
            for curr, next_item in zip(distinct, distinct[1:])
        ):
            links = distinct
            status = "PARTIAL_AUDIO_VISUAL_FRAME_LINKS"
        else:
            status = "UNKNOWN_TEMPORAL_CONTRADICTION"
    elif not candidates:
        status = "UNKNOWN_NO_COMPATIBLE_VISUAL_LINKS"
    result.update({
        "status": status,
        "frame_links": links,
        # A mismatch diagnostic is not a verified output/source time map.
        "candidate_links": candidates if status != "PARTIAL_AUDIO_VISUAL_FRAME_LINKS" else [],
        "rejected": rejected,
        "examined_source_frames": len(sources),
        "examined_finished_frames": len(finished),
        "examined_frame_comparisons": len(sources) * len(finished),
        "audio_offset_seconds": audio_offset_seconds,
        "time_tolerance_seconds": time_tolerance_seconds,
        "unknowns": result["unknowns"] + [
            "Link existence requires at least two separated, increasing frame matches",
            "No interpolation across unobserved edit gaps is authorized",
            "An aligned input frame can still be unedited original content",
        ],
    })
    return result
