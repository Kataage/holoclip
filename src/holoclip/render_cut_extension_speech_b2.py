"""Explicit local technical B2 render for ASR-completed cut-extension edges.

A and B must be existing hash-verified finished videos. B2 is a separate
technical MP4, *not* equal audio/caption finishing and not editorial QA.
"""
from __future__ import annotations

import hashlib
import math
from holoclip.domain import stable_id
from holoclip.render_cut_extension_finish_ab import prepare_matched_finishing_trial
from holoclip.render_cut_extension_mp4_ab import _trial_timeline
from holoclip.render_cut_extension_speech_audit import _load_matched
from holoclip.render_cut_extension_speech_refine import (
    suggest_speech_boundary_refinements,
)
from holoclip.render_plan import (
    _hash, _no_links, _probe, _target_file, compile_local_render_plan,
    render_local_preview,
)
from holoclip.render_ready_queue import _offline_render_policy
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_resolver import resolve_cached_render_sources

VERSION = "holoclip-local-speech-refinement-b2-mp4-v1"
_EXPECTED_QA = "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"


def _timeline(plan: dict, choice: dict):
    """Always reconstruct from verified source intervals, never trust JSON input."""
    baseline = plan["original"]["baseline_timeline"]
    ordered_a = sorted(baseline.segments, key=lambda s: s.output_order)
    ordered_b = sorted(plan["trial"].segments, key=lambda s: s.output_order)
    idx = choice["source_cut_index"]
    if (type(idx) is not int or idx != plan["source_cut_index"]
            or choice["trial_action"] != plan["trial_action"]
            or len(ordered_a) != len(ordered_b)):
        raise ValueError("LOCAL_B2_PROPOSAL_LINEAGE_CHANGED")
    before = [
        {"source_start_seconds": seg.source_start,
         "source_end_seconds": seg.source_end}
        for seg in ordered_a
    ]
    after = [
        {"source_start_seconds": (
            choice["proposed_source_start_seconds"]
            if i == idx else seg.source_start
        ), "source_end_seconds": (
            choice["proposed_source_end_seconds"]
            if i == idx else seg.source_end
        )}
        for i, seg in enumerate(ordered_b)
    ]
    result = _trial_timeline(baseline, {
        "baseline_source_intervals": before,
        "trial_source_intervals": after,
        "source_cut_index": idx,
        "trial_action": choice["trial_action"],
    })
    digest = hashlib.sha256(
        result.model_dump_json().encode("utf-8")
    ).hexdigest()
    if (digest != choice["proposed_timeline_sha256"]
            or not math.isclose(
                result.estimated_duration,
                choice["proposed_playback_seconds"], abs_tol=0.001,
            )):
        raise ValueError("LOCAL_B2_PROPOSAL_FINGERPRINT_MISMATCH")
    return result


def prepare_speech_b2_preview(
    *, proposal_index: int = 0,
    refinement_index: int = 0,
    **kwargs,
) -> dict:
    """Re-derive pinned A/B, ASR proposal and distinct B2 output identity."""
    if type(refinement_index) is not int or not 0 <= refinement_index < 2:
        raise ValueError("LOCAL_B2_REFINEMENT_INDEX_INVALID")
    prepared = prepare_matched_finishing_trial(
        proposal_index=proposal_index, **kwargs
    )
    proposals = suggest_speech_boundary_refinements(prepared)
    choices = proposals["proposals"]
    if refinement_index >= len(choices):
        raise ValueError("LOCAL_B2_NO_VERIFIED_REFINEMENT")
    choice = choices[refinement_index]
    timeline = _timeline(prepared["trial"], choice)
    trial = prepared["trial"]
    identity = stable_id(
        "speechb2", VERSION, trial["job_id"], trial["trial_id"],
        proposals["matched_receipt_sha256"],
        choice["proposed_timeline_sha256"], length=20,
    )
    root = trial["output"].parent
    target = _no_links(root / f"{trial['job_id']}-speechb2-{identity}.mp4")
    receipt = _no_links(target.with_suffix(".json"))
    return {
        "prepared": prepared, "proposal": proposals, "choice": choice,
        "timeline": timeline, "output": target, "receipt": receipt,
        "refinement_index": refinement_index, "b2_id": identity,
    }


def _summary(prepared: dict) -> dict:
    plan = prepared["prepared"]["trial"]
    choice = prepared["choice"]
    return {
        "version": VERSION,
        "status": "LOCAL_B2_PREVIEW_NOT_RENDERED",
        "job_id": plan["job_id"],
        "trial_id": plan["trial_id"],
        "b2_id": prepared["b2_id"],
        "refinement_index": prepared["refinement_index"],
        "baseline_a_filename": plan["baseline_mp4_filename"],
        "trial_b_mp4_filename": plan["trial_mp4_filename"],
        "b2_mp4_filename": prepared["output"].name,
        "b2_receipt_filename": prepared["receipt"].name,
        "matched_ab_receipt_sha256": prepared["proposal"]["matched_receipt_sha256"],
        "baseline_a_final_sha256": prepared["proposal"]["baseline_final_sha256"],
        "trial_b_final_sha256": prepared["proposal"]["trial_final_sha256"],
        "local_source_asr_sha256": prepared["proposal"]["source_asr_sha256"],
        "source_clock_original_extension_seconds":
            choice["total_extension_from_baseline_seconds"],
        "b2_extra_seconds_over_b":
            choice["increment_beyond_current_trial_seconds"],
        "b2_timeline_sha256": choice["proposed_timeline_sha256"],
        "b2_expected_seconds": round(prepared["timeline"].estimated_duration, 3),
        "a_b_b2_media_compared": False,
        "b2_caption_audio_finishing_matched_to_a_b": False,
        "editorial_quality_improvement": None,
        "human_rating_available": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
        "youtube_contacted": False,
    }


def run_speech_b2_preview(
    prepared: dict, *, execute: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Render only B2, reject stale lineage, never overwrite A/B or publish."""
    result = _summary(prepared)
    if not execute:
        return result
    if approve_local_preview is not True:
        raise ValueError("LOCAL_B2_EXPLICIT_PREVIEW_PERMISSION_REQUIRED")
    matched = prepared["prepared"]
    plan = matched["trial"]
    original = plan["original"]
    target = prepared["output"]
    sidecar = prepared["receipt"]
    _target_file(
        target, [original["baseline_media"], plan["output"]]
    )
    if sidecar.exists() or not sidecar.parent.is_dir():
        raise ValueError("LOCAL_B2_RECEIPT_EXISTS_OR_UNSAFE")
    # Recompute all proposals from present local media and source ASR. This
    # makes a stale caller-supplied prepared object incapable of bypassing
    # the current source/holdout/receipt/QA lineage gate.
    fresh = suggest_speech_boundary_refinements(matched)
    if (fresh != prepared["proposal"]
            or prepared["refinement_index"] >= len(fresh["proposals"])
            or fresh["proposals"][prepared["refinement_index"]] != prepared["choice"]):
        raise ValueError("LOCAL_B2_REFINEMENT_CHANGED")
    rederived = _timeline(plan, prepared["choice"])
    if rederived.model_dump_json() != prepared["timeline"].model_dump_json():
        raise ValueError("LOCAL_B2_TIMELINE_CHANGED")
    matched_receipt, a_video, b_video, matching_sha = _load_matched(matched)
    if matching_sha != fresh["matched_receipt_sha256"]:
        raise ValueError("LOCAL_B2_MATCHED_RECEIPT_CHANGED")
    a_sha = matched_receipt["baseline_final_sha256"]
    b_sha = matched_receipt["trial_final_sha256"]
    if _hash(a_video)[0] != a_sha or _hash(b_video)[0] != b_sha:
        raise ValueError("LOCAL_B2_OLD_VIDEO_CHANGED")
    a_time, a_has_video, a_has_audio = _probe(a_video)
    b_time, b_has_video, b_has_audio = _probe(b_video)
    if not all((a_has_video, a_has_audio, b_has_video, b_has_audio)):
        raise ValueError("LOCAL_B2_OLD_VIDEO_UNTRUSTED")
    sources = resolve_cached_render_sources(
        rederived, database=original["database"],
        cache_root=original["cache_root"], approve_local_preview=True,
    )
    _offline_render_policy(rederived, sources, original["policy_file"])
    render_plan = compile_local_render_plan(rederived, sources, target)
    produced = render_local_preview(render_plan)
    if produced.get("status") != _EXPECTED_QA:
        raise ValueError("LOCAL_B2_MEDIA_QA_FAILED")
    b2_sha, b2_size = _hash(target)
    b2_time, b2_video, b2_audio = _probe(target)
    if (not b2_size or b2_sha != produced.get("output_sha256")
            or not b2_video or not b2_audio
            or abs(b2_time - rederived.estimated_duration) > 0.5):
        raise ValueError("LOCAL_B2_RESULT_UNTRUSTED")
    # A, B, base MP4s, optional stage receipts and source ASR are rechecked
    # *after* FFmpeg before committing any A/B/B2 measurement.
    after = suggest_speech_boundary_refinements(matched)
    if after != fresh:
        raise ValueError("LOCAL_B2_SOURCE_EVIDENCE_CHANGED_DURING_RENDER")
    if (_hash(a_video)[0] != a_sha or _hash(b_video)[0] != b_sha
            or _hash(original["baseline_media"])[0] != original["baseline_sha256"]
            or _hash(plan["output"])[0] != matched_receipt["trial_source_mp4_sha256"]
            or _hash(target)[0] != b2_sha):
        raise ValueError("LOCAL_B2_MEDIA_CHANGED_DURING_RENDER")
    result.update({
        "status": "LOCAL_B2_RENDERED_THREE_WAY_TECHNICAL_ONLY",
        "baseline_a_final_filename": a_video.name,
        "trial_b_final_filename": b_video.name,
        "baseline_a_final_sha256": a_sha,
        "trial_b_final_sha256": b_sha,
        "b2_mp4_sha256": b2_sha,
        "baseline_a_measured_seconds": round(a_time, 3),
        "trial_b_measured_seconds": round(b_time, 3),
        "b2_measured_seconds": round(b2_time, 3),
        "b2_vs_b_duration_difference_seconds": round(b2_time - b_time, 3),
        "b2_vs_a_duration_difference_seconds": round(b2_time - a_time, 3),
        "b2_render_plan_id": render_plan.id,
        "b2_technical_qa": _EXPECTED_QA,
        "a_b_b2_media_compared": True,
        "b2_caption_audio_finishing_matched_to_a_b": False,
        "editorial_quality_improvement": None,
        "ready": False,
        "production_ready": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
    })
    save_semantic_review(result, sidecar)
    return result
