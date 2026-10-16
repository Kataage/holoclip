"""Owner-approved local source/B2/B3 review playback pack for one ASR-QA target.

Strictly a listening/viewing aid, never an autonomous editorial judgement.
Read-only by default. The source is the *already cached complete video*;
no downloads, remote ASR, publishing, or rewriting earlier output files.
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
from pathlib import Path

from holoclip.render_b2_b3_audio_review import _review, _saved_audio_asr
from holoclip.render_b2_b3_finished_audio_asr import prepare_b2_b3_audio_asr
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _call, _hash, _no_links, _probe, _source_file
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-source-b2-b3-local-review-playback-pack-v1"
_ROLES = ("source", "b2", "b3")
_MAX_TARGETS = 20
_SOURCE_RADIUS = 3.0
_OUTPUT_RADIUS = 2.5


def _clip_window(center: float, duration: float, radius: float) -> dict:
    if (not all(math.isfinite(v) for v in (center, duration, radius))
            or not 0 <= center <= duration or not .3 <= duration
            or duration > 86400 or radius <= 0):
        raise ValueError("LOCAL_REVIEW_PACK_WINDOW_INVALID")
    lo, hi = max(0., center - radius), min(duration, center + radius)
    if hi - lo < .3:
        raise ValueError("LOCAL_REVIEW_PACK_WINDOW_TOO_SHORT")
    return {
        "start_seconds": round(lo, 3),
        "end_seconds": round(hi, 3),
        "expected_seconds": round(hi - lo, 3),
    }


def _project(timeline, asset_id: str, source_clock: float) -> float | None:
    """Use the source clock, not matching output timestamps across cuts."""
    cursor = 0.
    points = []
    for cut in sorted(timeline.segments, key=lambda c: c.output_order):
        span = cut.source_end - cut.source_start
        if (cut.source_asset_id == asset_id
                and cut.source_start <= source_clock <= cut.source_end):
            points.append(cursor + source_clock - cut.source_start)
        cursor += span
    if len(points) > 1:
        # Source material can repeat in a video: choose no ambiguous alignment.
        return None
    return points[0] if points else None


def _select(basis: dict, plan: dict, index: int) -> dict:
    context = basis["context"]
    found = context["found"]
    baseline = sorted(
        found["trial"]["original"]["baseline_timeline"].segments,
        key=lambda c: c.output_order,
    )
    cut_index = context["source_cut_index"]
    if (not 0 <= cut_index < len(baseline)
            or len(plan["targets"]) != plan["review_target_count"]
            or plan["status"] != "LOCAL_B2_B3_REVIEW_TARGETS_UNRATED_ASR_AND_QA"
            or not plan["targets"]):
        raise ValueError("LOCAL_REVIEW_PACK_NO_ELIGIBLE_TARGET")
    if type(index) is not int or not 0 <= index < min(
        _MAX_TARGETS, len(plan["targets"])
    ):
        raise ValueError("LOCAL_REVIEW_PACK_TARGET_INDEX_INVALID")
    cut = baseline[cut_index]
    source_ids = {
        seg.source_asset_id for role in ("b2", "b3")
        for seg in context["roles"][role]["timeline"].segments
    }
    if len(source_ids) != 1 or cut.source_asset_id not in source_ids:
        raise ValueError("LOCAL_REVIEW_PACK_MULTISOURCE_NOT_ATTESTED")
    target = plan["targets"][index]
    source = (
        (target["source_start_seconds"] + target["source_end_seconds"]) / 2
        if target["source_start_seconds"] is not None
        else (cut.source_start if context["trial_action"] == "EXTEND_START"
              else cut.source_end)
    )
    if not math.isfinite(source) or source < 0:
        raise ValueError("LOCAL_REVIEW_PACK_SOURCE_CLOCK_INVALID")
    windows = {}
    for role in ("b2", "b3"):
        timeline = context["roles"][role]["timeline"]
        projected = _project(timeline, cut.source_asset_id, source)
        focus = basis["focus"][role]
        # When a source utterance is absent from one edit, show its *edit
        # boundary* instead of pretending the utterance is still present.
        midpoint = (
            projected if projected is not None else min(
                timeline.estimated_duration,
                focus["output_start_seconds"] + 2.,
            )
        )
        windows[role] = {
            **_clip_window(midpoint, timeline.estimated_duration, _OUTPUT_RADIUS),
            "selected_source_clock_visible": projected is not None,
            "source_to_output_time_seconds": (
                round(projected, 3) if projected is not None else None
            ),
            "fallback_to_edit_boundary": projected is None,
        }
    # Source window is bounded against the *actual source video duration*
    # only after its full local cache has been validated by the owner-approved
    # execution path. This preview is a clock proposal, not decoded evidence.
    return {
        "target": target, "target_index": index,
        "asset_id": cut.source_asset_id,
        "source_clock_seconds": round(source, 3),
        "output_windows": windows,
    }


def _basis(kwargs: dict, index: int) -> dict:
    basis = prepare_b2_b3_audio_asr(**kwargs)
    if basis.get("status") == "LOCAL_B2_B3_ASR_NO_COMPLETED_JOB":
        return {"status": "LOCAL_REVIEW_PACK_NO_COMPLETED_JOB"}
    saved = _saved_audio_asr(basis)
    if saved is None:
        return {"status": "LOCAL_REVIEW_PACK_ASR_NOT_MEASURED"}
    asr_receipt, audio_sha = saved
    plan = _review(basis, asr_receipt, audio_sha)
    if not plan["targets"]:
        return {"status": "LOCAL_REVIEW_PACK_NO_REVIEW_TARGETS_NOT_CLEAN"}
    selected = _select(basis, plan, index)
    source = basis["context"]["found"]["target"]
    prefix = f"{source.stem}.review-{index:02d}"
    folder = _no_links(source.parent)
    artifacts = {
        role: _no_links(folder / f"{prefix}-{role}.mp4")
        for role in _ROLES
    }
    receipt = _no_links(folder / f"{prefix}.playback.json")
    return {
        "status": "SUPPORTED", "basis": basis, "plan": plan,
        "selected": selected, "artifacts": artifacts,
        "receipt": receipt,
    }


def _report(state: dict) -> dict:
    basis = state["basis"]
    ctx = basis["context"]
    selected = state["selected"]
    return {
        "version": VERSION, "status": "LOCAL_REVIEW_PACK_READ_ONLY_PLAN",
        "job_id": ctx["job_id"], "trial_id": ctx["trial_id"],
        "b2_id": ctx["b2_id"], "b3_id": ctx["b3_id"],
        "source_asr_sha256": basis["source_sha256"],
        "four_way_diagnostics_sha256": basis["four_way_sha256"],
        "b2_b3_asr_receipt_sha256": state["plan"]["b2_b3_audio_asr_receipt_sha256"],
        "review_target_index": selected["target_index"],
        "review_target_priority": selected["target"]["priority"],
        "review_target_kind": selected["target"]["kind"],
        "source_asr_interval_id": selected["target"]["source_asr_interval_id"],
        "source_clock_seconds": selected["source_clock_seconds"],
        "source_media_sha256": None,
        "proposed_output_windows": selected["output_windows"],
        "rendered_source_window": None,
        "created_review_clips": {},
        "number_of_review_clips": 0,
        "originals_modified": False,
        "source_video_downloaded": False,
        "new_provider_request": False,
        "transcript_text_exported": False,
        "human_auditory_review_completed": False,
        "source_asr_not_verified_spoken_words": True,
        "comparison_not_a_creative_quality_verdict": True,
        "automatic_winner": None, "automatic_adoption_allowed": False,
        "publication_performed": False, "ready": False,
        "production_ready": False,
    }


def _encode(ffmpeg: str, source: Path, output: Path, window: dict) -> None:
    """Local video+audio extract; bounded duration, no URI/protocol access."""
    if (not _source_file(source).is_file()
            or output.exists() or output.is_symlink()
            or not .3 <= window["expected_seconds"] <= 6.1):
        raise ValueError("LOCAL_REVIEW_PACK_MEDIA_ARGUMENT_INVALID")
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-protocol_whitelist", "file,pipe",
        "-ss", str(window["start_seconds"]),
        "-i", str(source),
        "-t", str(window["expected_seconds"]),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart", "-f", "mp4",
        str(output),
    ]
    proc = _call(argv, timeout=150)
    if proc.returncode != 0 or not output.is_file():
        raise ValueError("LOCAL_REVIEW_PACK_FFMPEG_FAILED")


def _execute(state: dict, kwargs: dict, index: int) -> dict:
    ctx = state["basis"]["context"]
    for path in (*state["artifacts"].values(), state["receipt"]):
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise ValueError("LOCAL_REVIEW_PACK_OUTPUT_EXISTS_OR_UNSAFE")
    original = ctx["found"]["trial"]["original"]
    sources = resolve_cached_render_sources(
        ctx["roles"]["b2"]["timeline"],
        database=original["database"], cache_root=original["cache_root"],
        approve_local_preview=True,
    )
    if (len(sources) != 1
            or sources[0].asset.id != state["selected"]["asset_id"]):
        raise ValueError("LOCAL_REVIEW_PACK_SOURCE_ID_UNVERIFIED")
    source_file = _source_file(_no_links(sources[0].path))
    source_sha, source_size = _hash(source_file)
    source_duration, source_video, source_audio = _probe(source_file)
    if not source_video or not source_audio:
        raise ValueError("LOCAL_REVIEW_PACK_SOURCE_AV_MISSING")
    window = _clip_window(
        state["selected"]["source_clock_seconds"],
        source_duration, _SOURCE_RADIUS,
    )
    current = _basis(kwargs, index)
    if (current.get("status") != "SUPPORTED"
            or _report(current) != _report(state)):
        raise ValueError("LOCAL_REVIEW_PACK_LINEAGE_CHANGED_BEFORE_ENCODE")
    result = _report(state)
    role_sources = {
        "source": source_file,
        "b2": _source_file(_no_links(ctx["roles"]["b2"]["final"])),
        "b3": _source_file(_no_links(ctx["roles"]["b3"]["final"])),
    }
    windows = {"source": window, **state["selected"]["output_windows"]}
    if shutil.which("ffmpeg") is None:
        raise ValueError("LOCAL_REVIEW_PACK_FFMPEG_UNAVAILABLE")
    ffmpeg = shutil.which("ffmpeg")
    completed = {}
    with tempfile.TemporaryDirectory(
        prefix=".holoclip-b2-b3-pack-", dir=state["receipt"].parent,
    ) as folder:
        staged = {}
        for role in _ROLES:
            scratch = _no_links(Path(folder) / f"{role}.mp4")
            _encode(ffmpeg, role_sources[role], scratch, windows[role])
            duration, video, audio = _probe(scratch)
            expected = windows[role]["expected_seconds"]
            if (not video or not audio or not math.isfinite(duration)
                    or abs(duration - expected) > .75):
                raise ValueError("LOCAL_REVIEW_PACK_AV_QA_FAILED")
            sha, size = _hash(scratch)
            if not size:
                raise ValueError("LOCAL_REVIEW_PACK_EMPTY_OUTPUT")
            staged[role] = {
                "scratch": scratch, "sha": sha, "size": size,
                "measured_seconds": round(duration, 3),
            }
        # Avoid races, stale receipts and mutation during FFmpeg decoding.
        fresh = _basis(kwargs, index)
        if (fresh.get("status") != "SUPPORTED"
                or _report(fresh) != _report(state)
                or _hash(source_file) != (source_sha, source_size)):
            raise ValueError("LOCAL_REVIEW_PACK_INPUT_CHANGED_DURING_ENCODE")
        for role in ("b2", "b3"):
            if (_hash(role_sources[role])[0]
                    != ctx["roles"][role]["final_sha256"]):
                raise ValueError("LOCAL_REVIEW_PACK_FINISHED_VIDEO_CHANGED")
        result.update({
            "status": "LOCAL_REVIEW_PACK_LOCAL_CLIPS_UNRATED",
            "source_media_sha256": source_sha,
            "rendered_source_window": window,
            "created_review_clips": {
                role: {
                    "filename": state["artifacts"][role].name,
                    "sha256": staged[role]["sha"],
                    "size_bytes": staged[role]["size"],
                    "measured_seconds": staged[role]["measured_seconds"],
                    "window": windows[role],
                } for role in _ROLES
            },
            "number_of_review_clips": len(_ROLES),
        })
        # Hardlink create-only. If interrupted, any orphan clips are
        # intentional failures on rerun; do not replace user-owned files.
        try:
            for role in _ROLES:
                output = state["artifacts"][role]
                os.link(staged[role]["scratch"], output)
                completed[role] = staged[role]["sha"]
            save_semantic_review(result, validate_semantic_output(state["receipt"]))
        except (OSError, ValueError):
            for role, digest in completed.items():
                output = state["artifacts"][role]
                if output.is_file() and not output.is_symlink() and _hash(output)[0] == digest:
                    output.unlink()
            raise
    return result


def run_local_review_pack(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, execute: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Derive one context pack; owner approval needed to produce local videos."""
    if (type(target_index) is not int or not 0 <= target_index < _MAX_TARGETS
            or type(execute) is not bool or type(approve_local_preview) is not bool):
        raise ValueError("LOCAL_REVIEW_PACK_ARGUMENT_INVALID")
    if execute and not approve_local_preview:
        raise ValueError("LOCAL_REVIEW_PACK_EXPLICIT_APPROVAL_REQUIRED")
    kwargs = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "proposal_index": proposal_index, "refinement_index": refinement_index,
        "candidate_index": candidate_index,
    }
    if not execute:
        state = _basis(kwargs, target_index)
        return (_report(state) if state["status"] == "SUPPORTED"
                else {"version": VERSION, "status": state["status"],
                      "ready": False, "production_ready": False})
    # Bind writer lock to the current canonical job, not a caller-supplied path.
    first = _basis(kwargs, target_index)
    if first["status"] != "SUPPORTED":
        raise ValueError("LOCAL_REVIEW_PACK_NOT_READY_FOR_LOCAL_RENDER")
    with _exclusive_run_lock(
        _no_links(Path(preview_root)), first["basis"]["context"]["job_id"],
    ):
        fresh = _basis(kwargs, target_index)
        if fresh["status"] != "SUPPORTED" or _report(fresh) != _report(first):
            raise ValueError("LOCAL_REVIEW_PACK_CHANGED_BEFORE_LOCKED_EXECUTE")
        return _execute(fresh, kwargs, target_index)
