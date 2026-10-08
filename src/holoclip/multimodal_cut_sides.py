"""Independently sample both sides of candidate cuts in source and finished video.

Acoustic offsets select which *bounded* original section to inspect. The actual
pixels are compared independently; no LLM estimates a timestamp or a cut.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from holoclip.visual_edit_evidence import _compare_features

VERSION = "bounded-cross-video-cut-sides-v1"
FPS = 8
HEIGHT = 360
WIDTH = 640
MAX_FRAMES_PER_WINDOW = 16
MAX_TRANSITIONS = 8


def decode_short_frames(media: Path, center: float, duration: float):
    """Return at most 16 640x360 frames in one bounded 1.5-second seek."""
    import numpy as np

    if (not math.isfinite(center) or not math.isfinite(duration)
            or not 1 <= center < duration - 1):
        raise ValueError("Sampling center must have at least 1s source margin")
    if not media.is_file():
        raise FileNotFoundError("Bounded media missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found")
    start = center - .75
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-ss", f"{start:.4f}",
        "-i", str(media), "-t", "1.5", "-map", "0:v:0",
        "-an", "-sn", "-dn",
        "-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT},format=gray",
        "-frames:v", str(MAX_FRAMES_PER_WINDOW),
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    output = subprocess.run(command, check=True, capture_output=True, timeout=90).stdout
    size = WIDTH * HEIGHT
    if len(output) > MAX_FRAMES_PER_WINDOW * size or len(output) % size:
        raise ValueError("Decode exceeds short interval frame byte budget")
    frames = np.frombuffer(output, dtype=np.uint8).reshape((-1, HEIGHT, WIDTH))
    return frames, [round(start + i / FPS, 4) for i in range(len(frames))]


def _features(frames):
    import cv2

    orb = cv2.ORB_create(nfeatures=1200, fastThreshold=15, edgeThreshold=20)
    return [
        (frame.shape, *orb.detectAndCompute(frame, None)) for frame in frames
    ]


def _side_check(source_images, finished_images, competing_source_images) -> dict:
    """Require 2 separated frame observations and no cross-side visual aliases."""
    if len(source_images) < 3 or len(finished_images) < 3:
        return {"status": "UNKNOWN_INSUFFICIENT_SAMPLED_FRAMES", "matches": []}
    source_features = _features(source_images)
    finished_features = _features(finished_images)
    opposite = _features(competing_source_images)
    candidates = []
    for i, finished in enumerate(finished_features):
        correct = [
            (_compare_features(src, finished), j)
            for j, src in enumerate(source_features)
        ]
        correct = sorted(
            [(score["score"], j, score) for score, j in correct if score is not None],
            key=lambda entry: (-entry[0], entry[1]),
        )
        if not correct or correct[0][0] < .25:
            continue
        decoys = [
            item["score"] for src in opposite
            if (item := _compare_features(src, finished)) is not None
        ]
        top = correct[0]
        if decoys and top[0] - max(decoys) < .09:
            continue
        candidates.append({
            "finished_sample": i, "source_sample": top[1],
            "geometry": top[2], "opposite_side_score": (
                round(max(decoys), 5) if decoys else None
            ),
        })
    # A still image repeated across the interval is not a temporal trajectory.
    # Distinct source frames must advance as finished video time increases.
    increasing = []
    for point in candidates:
        if (not increasing or (
            point["finished_sample"] - increasing[-1]["finished_sample"] >= 2
            and point["source_sample"] > increasing[-1]["source_sample"]
        )):
            increasing.append(point)
    if len(increasing) < 2:
        return {"status": "UNKNOWN_NO_UNIQUE_TEMPORAL_IMAGE_SEQUENCE",
                "matches": candidates[:MAX_FRAMES_PER_WINDOW],
                "rejected_duplicate_or_static": True}
    return {
        "status": "PARTIAL_SAMPLED_SIDE_FRAME_MATCH",
        "matches": increasing[:MAX_FRAMES_PER_WINDOW],
        "time_basis": "bounded_ffmpeg_uniform_sampling_clock_not_native_frame_pts",
    }


def compare_transition_sides(
    clip_media: Path, sections: list[dict], clocks: list[dict],
    refinement: dict, transition: dict, output_jump: dict,
    *, clip_duration: float, decode=decode_short_frames,
) -> dict:
    """Return editor-cut candidate only if both source sides fit independent images.

    This function does not claim the original stream had no natural transition:
    its two source moments can be far apart and may include edited gameplay.
    """
    unknown = {
        "version": VERSION, "status": "UNKNOWN_INDEPENDENT_VISUAL_SUPPORT",
        "verified_edit_cut": False,
        "exact_cut_frame_verified": False,
        "original_video_absolute_pts_verified": False,
        "output_source_map_verified": False,
        "side_matches": [],
    }
    if (transition.get("status") != "CUT_INTERVAL_CANDIDATE"
            or not isinstance(transition.get("interval"), list)
            or len(transition["interval"]) != 2
            or not all(isinstance(x, (float, int)) and math.isfinite(x)
                       for x in transition["interval"])
            or not 0 <= transition["interval"][0] < transition["interval"][1] <= clip_duration):
        return {**unknown, "status": "UNKNOWN_UNSUPPORTED_ACOUSTIC_INTERVAL"}
    if output_jump.get("relation") != "CORROBORATING_OUTPUT_CHANGE":
        return {**unknown, "status": "UNKNOWN_OUTPUT_VISUAL_JUMP"}
    points = refinement.get("points", [])
    sides = []
    for side, group_key in (("left", "left_group"), ("right", "right_group")):
        group = transition.get(group_key)
        if not isinstance(group, int) or not 0 <= group < len(sections):
            return {**unknown, "status": "UNKNOWN_SOURCE_GROUP"}
        clock = clocks[group]
        section = sections[group]
        if (clock.get("status") != "PARTIAL_SECTION_AUDIO_CLOCK"
                or clock.get("frame_pts_verified") is not True
                or clock.get("original_video_absolute_pts_verified") is not False
                or not isinstance(clock.get("origin_seconds"), (int, float))
                or not math.isfinite(clock["origin_seconds"])):
            return {**unknown, "status": "UNKNOWN_SECTION_CLOCK"}
        entries = [p for p in points if p.get("group") == group]
        if side == "left":
            entries = [p for p in entries if p["clip_end"] <= transition["interval"][1] + .25]
            entries.sort(key=lambda p: -p["clip_end"])
        else:
            entries = [p for p in entries if p["clip_start"] >= transition["interval"][0] - .25]
            entries.sort(key=lambda p: p["clip_start"])
        if not entries:
            return {**unknown, "status": "UNKNOWN_MISSING_SIDE_AUDIO_ANCHOR"}
        anchor = entries[0]
        clip_center = (anchor["clip_start"] + anchor["clip_end"]) / 2
        original_center = (anchor["source_start"] + anchor["source_end"]) / 2
        local_center = original_center - clock["origin_seconds"]
        if (not 1 <= clip_center < clip_duration - 1
                or not 1 <= local_center < section["source_measured_duration_seconds"] - 1
                or not section["source_start"] - 5 <= original_center <= section["source_end"] + 5):
            return {**unknown, "status": "UNKNOWN_SOURCE_SEEK_OUTSIDE_SECTION"}
        sides.append({
            "side": side, "group": group, "clip_center": clip_center,
            "source_center": original_center, "source_local_center": local_center,
            "source_media": Path(section["source_video_path"]),
        })
    left, right = sides
    source_gap = right["source_center"] - left["source_center"]
    clip_gap = right["clip_center"] - left["clip_center"]
    if clip_gap <= 0 or abs((source_gap - clip_gap)) < 1.:
        return {**unknown, "status": "UNKNOWN_SOURCE_CONTINUITY"}
    if source_gap < 0:
        # Reversed source temporal order is possible but cannot be promoted
        # as a simple forward cut transition.
        return {**unknown, "status": "UNKNOWN_REORDERED_SOURCE"}
    try:
        output_frames = [
            decode(clip_media, row["clip_center"], clip_duration)[0] for row in sides
        ]
        original_frames = [
            decode(row["source_media"], row["source_local_center"],
                   sections[row["group"]]["source_measured_duration_seconds"])[0]
            for row in sides
        ]
    except (ValueError, OSError, subprocess.SubprocessError):
        return {**unknown, "status": "UNKNOWN_BOUNDED_FRAME_DECODE"}
    verified = [
        _side_check(original_frames[i], output_frames[i], original_frames[1 - i])
        for i in (0, 1)
    ]
    observations = [{
        "side": row["side"], "group": row["group"],
        "source_audio_time_seconds": round(row["source_center"], 3),
        "finished_audio_time_seconds": round(row["clip_center"], 3),
        "native_frame_pts_verified": False,
        **found,
    } for row, found in zip(sides, verified)]
    if any(o["status"] != "PARTIAL_SAMPLED_SIDE_FRAME_MATCH" for o in observations):
        return {**unknown, "status": "UNKNOWN_ONE_OR_BOTH_SIDES_UNVERIFIED",
                "side_matches": observations}
    return {
        **unknown, "status": "MULTIMODAL_EDIT_JUMP_CANDIDATE",
        "side_matches": observations,
        "source_audio_time_gap_seconds": round(source_gap, 3),
        "finished_audio_time_gap_seconds": round(clip_gap, 3),
        "skipped_source_time_candidate_seconds": round(source_gap - clip_gap, 3),
        "acoustic_cut_bracket": transition["interval"],
        "unknowns": [
            "Sparse sampled image links are not exact source/output frame timestamps",
            "Cross-video source A/V offset and transcoding delay are uncalibrated",
            "Edited source scene versus human editor cut causation is not proved",
            "Original native frame PTS and complete OutputSourceTimeMap are not verified",
        ],
    }
