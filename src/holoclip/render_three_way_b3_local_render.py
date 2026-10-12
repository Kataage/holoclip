"""Explicit, create-only B3 local trial from verified source+edited-ASR cut hints.

No automatic adoption, source download, external inference, audio finishing,
narrative qualification, or publishing. The default is read-only. A separate
owner-approved invocation may produce one basic A/V-QA-checked trial MP4.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.domain import stable_id
from holoclip.render_cut_extension_mp4_ab import _trial_timeline
from holoclip.render_cut_extension_three_way_diagnostics import _source_evidence
from holoclip.render_cut_extension_three_way_triage import (
    prepare_three_way_review, run_three_way_review,
)
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import (
    _hash, _no_links, _probe, _target_file,
    compile_local_render_plan, render_local_preview,
)
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT, _offline_render_policy
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_three_way_b3_boundary_hints import (
    propose_guarded_b3_boundaries,
)
from holoclip.render_three_way_existing_clip_asr_join import join_saved_edited_asr
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock
from holoclip.render_three_way_existing_media_pilot import _completed_jobs
from holoclip.render_three_way_prerequisites import inspect_local_prerequisites
from holoclip.render_three_way_speech_coverage import compare_source_asr_coverage

VERSION = "holoclip-guarded-b3-local-preview-v1"
_EXPECTED_QA = "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"
_MAX_CANDIDATES = 2


def _timeline(original, b2_timeline, hint: dict):
    """Reconstruct B3 from canonical A + B2, never from arbitrary JSON."""
    ordered_a = sorted(original.segments, key=lambda s: s.output_order)
    ordered_b2 = sorted(b2_timeline.segments, key=lambda s: s.output_order)
    index = hint["source_cut_index"]
    if (type(index) is not int or not 0 <= index < len(ordered_a)
            or len(ordered_a) != len(ordered_b2)
            or hint["trial_action"] not in ("EXTEND_START", "EXTEND_END")):
        raise ValueError("LOCAL_B3_RENDER_TIMELINE_STRUCTURE_INVALID")
    a = [{"source_start_seconds": s.source_start,
          "source_end_seconds": s.source_end} for s in ordered_a]
    b3 = [{"source_start_seconds": (
            hint["proposed_source_start_seconds"] if i == index else s.source_start),
           "source_end_seconds": (
            hint["proposed_source_end_seconds"] if i == index else s.source_end)}
          for i, s in enumerate(ordered_b2)]
    rebuilt = _trial_timeline(original, {
        "baseline_source_intervals": a,
        "trial_source_intervals": b3,
        "source_cut_index": index,
        "trial_action": hint["trial_action"],
    })
    digest = hashlib.sha256(rebuilt.model_dump_json().encode("utf-8")).hexdigest()
    if (digest != hint["proposed_timeline_sha256"]
            or abs(rebuilt.estimated_duration
                   - hint["proposed_playback_seconds"]) > 0.001):
        raise ValueError("LOCAL_B3_RENDER_FINGERPRINT_CHANGED")
    # Re-check B3 modifies only the existing B2 boundary in the outward
    # direction. The proposal builder checked the 2 s / 8 s limits.
    cur, nxt = ordered_b2[index], sorted(
        rebuilt.segments, key=lambda s: s.output_order,
    )[index]
    if hint["trial_action"] == "EXTEND_END":
        permitted = nxt.source_start == cur.source_start and (
            0 < nxt.source_end - cur.source_end <= 2.001)
    else:
        permitted = nxt.source_end == cur.source_end and (
            0 < cur.source_start - nxt.source_start <= 2.001)
    if (not permitted or hint["requires_new_render_and_independent_audio_review"]
            is not True):
        raise ValueError("LOCAL_B3_RENDER_NOT_A_BOUNDED_EXTENSION")
    return rebuilt


def _evidence(inputs: dict, candidate_index: int) -> dict:
    """Reconstruct all source/video/audio lineage from current local assets."""
    doctor = inspect_local_prerequisites(**inputs)
    if doctor["next_stage"] != "EVIDENCE_COMPLETE":
        raise ValueError("LOCAL_B3_RENDER_A_B_B2_EVIDENCE_INCOMPLETE")
    prepared = prepare_three_way_review(**inputs, max_jobs=40)
    review = run_three_way_review(prepared, save=False)
    coverage = compare_source_asr_coverage(prepared, review)
    edited = join_saved_edited_asr(inputs=inputs, coverage=coverage)
    hints = propose_guarded_b3_boundaries(
        prepared=prepared, plan=review, coverage=coverage, edited_asr=edited,
    )
    if (hints["proposed_count"] != len(hints["proposals"])
            or not 0 <= hints["proposed_count"] <= _MAX_CANDIDATES):
        raise ValueError("LOCAL_B3_RENDER_HINTS_INVALID")
    if candidate_index >= hints["proposed_count"]:
        return {"hints": hints, "choice": None}
    choice = hints["proposals"][candidate_index]
    b2 = prepared["diagnostics"]["finished"]["b2"]
    trial = b2["prepared"]["trial"]
    original = trial["original"]
    timeline = _timeline(original["baseline_timeline"], b2["timeline"], choice)
    observed = _source_evidence(prepared["diagnostics"])
    media_sha = {
        role: observed["roles"][role]["final_sha256"]
        for role in ("a", "b", "b2")
    }
    source_sha = coverage["source_asr_sha256"]
    # The identity of the independent ASR discrepancy evidence is included.
    audio_sig = hashlib.sha256(
        json.dumps(edited, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    identity = stable_id(
        "speechb3", VERSION, trial["job_id"], trial["trial_id"],
        b2["b2_id"], source_sha, audio_sig, media_sha,
        choice["proposed_timeline_sha256"], length=20,
    )
    target = _no_links(
        b2["output"].parent / f"{trial['job_id']}-speechb3-{identity}.mp4"
    )
    return {
        "hints": hints, "choice": choice, "prepared": prepared,
        "review": review, "coverage": coverage, "edited": edited,
        "observed": observed, "media_sha": media_sha, "audio_sig": audio_sig,
        "source_sha": source_sha, "timeline": timeline,
        "trial": trial, "b2": b2, "target": target,
        "receipt": _no_links(target.with_suffix(".json")),
        "identity": identity,
    }


def _summary(inputs: dict, found: dict, index: int) -> dict:
    choice = found["choice"]
    report = {
        "version": VERSION,
        "status": ("LOCAL_B3_PREVIEW_NOT_RENDERED"
                   if choice is not None else "LOCAL_B3_NO_ELIGIBLE_HINT"),
        "job_id": inputs["job_id"],
        "candidate_index": index,
        "source_clock_edit_hint_available": choice is not None,
        "proposed_count": found["hints"]["proposed_count"],
        "source_video_downloaded": False,
        "remote_model_requests": 0,
        "original_or_a_b_b2_video_modified": False,
        "new_b3_local_mp4_rendered": False,
        "b3_finished_audio_or_captions_matched": False,
        "a_b_b2_b3_duration_compared": False,
        "human_auditory_qa_completed": False,
        "editorial_quality_improvement": None,
        "automatic_winner_selected": False,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }
    if choice is not None:
        report.update({
            "b3_id": found["identity"],
            "b3_mp4_filename": found["target"].name,
            "b3_receipt_filename": found["receipt"].name,
            "b3_timeline_sha256": choice["proposed_timeline_sha256"],
            "b3_expected_seconds": choice["proposed_playback_seconds"],
            "b3_extra_seconds_over_b2": choice["extra_seconds_beyond_b2"],
            "source_asr_sha256": found["source_sha"],
            "edited_audio_hint_digest": found["audio_sig"],
            "a_b_b2_finished_mp4_sha256": found["media_sha"],
            "requires_independent_listening_before_adoption": True,
        })
    return report


def run_existing_b3_preview(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    execute: bool = False, approve_local_preview: bool = False,
) -> dict:
    """Preview one candidate or render one isolated, owner-approved MP4."""
    if (type(execute) is not bool or type(approve_local_preview) is not bool
            or type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)
            or type(candidate_index) is not int
            or not 0 <= candidate_index < _MAX_CANDIDATES):
        raise ValueError("LOCAL_B3_RENDER_ARGUMENTS_INVALID")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(path)) for path in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    )
    if execute and approve_local_preview is not True:
        raise ValueError("LOCAL_B3_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    jobs, _total = _completed_jobs(queue, job_id=job_id, max_jobs=1)
    if not jobs:
        return {
            "version": VERSION, "status": "LOCAL_B3_NO_COMPLETED_SOURCE_JOB",
            "new_b3_local_mp4_rendered": False,
            "ready": False, "production_ready": False,
        }
    inputs = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": jobs[0],
        "proposal_index": proposal_index, "refinement_index": refinement_index,
    }

    def process() -> dict:
        prepared = _evidence(inputs, candidate_index)
        report = _summary(inputs, prepared, candidate_index)
        if not execute:
            return report
        if prepared["choice"] is None:
            raise ValueError("LOCAL_B3_RENDER_NO_PROVEN_CANDIDATE")
        target, receipt = prepared["target"], prepared["receipt"]
        baseline = prepared["trial"]["original"]
        _target_file(target, [
            baseline["baseline_media"], prepared["trial"]["output"],
            prepared["b2"]["output"],
        ])
        if receipt.exists() or receipt.is_symlink() or not receipt.parent.is_dir():
            raise ValueError("LOCAL_B3_RENDER_RECEIPT_EXISTS_OR_UNSAFE")
        # Fail before creating any B3 output if existing lineage changed.
        fresh = _evidence(inputs, candidate_index)
        if _summary(inputs, fresh, candidate_index) != report:
            raise ValueError("LOCAL_B3_RENDER_SOURCE_CHANGED_BEFORE_RENDER")
        sources = resolve_cached_render_sources(
            prepared["timeline"], database=baseline["database"],
            cache_root=baseline["cache_root"], approve_local_preview=True,
        )
        _offline_render_policy(
            prepared["timeline"], sources, baseline["policy_file"],
        )
        plan = compile_local_render_plan(prepared["timeline"], sources, target)
        produced = render_local_preview(plan)
        if produced.get("status") != _EXPECTED_QA:
            raise ValueError("LOCAL_B3_RENDER_TECHNICAL_QA_FAILED")
        digest, size = _hash(target)
        measured, has_video, has_audio = _probe(target)
        if (not size or digest != produced.get("output_sha256")
                or not has_video or not has_audio
                or abs(measured - prepared["timeline"].estimated_duration) > .5):
            raise ValueError("LOCAL_B3_RENDER_MEDIA_UNTRUSTED")
        # Reconstruct the *same* canonical and rechecked predecessor assets.
        after = _evidence(inputs, candidate_index)
        if _summary(inputs, after, candidate_index) != report:
            raise ValueError("LOCAL_B3_RENDER_SOURCE_CHANGED_DURING_RENDER")
        if _hash(target)[0] != digest:
            raise ValueError("LOCAL_B3_RENDER_RESULT_CHANGED")
        predecessors = prepared["observed"]["roles"]
        times = {}
        for role in ("a", "b", "b2"):
            path = _no_links(predecessors[role]["final"])
            if _hash(path)[0] != prepared["media_sha"][role]:
                raise ValueError("LOCAL_B3_RENDER_PREDECESSOR_CHANGED")
            duration, video, audio = _probe(path)
            if not video or not audio:
                raise ValueError("LOCAL_B3_RENDER_PREDECESSOR_AV_UNTRUSTED")
            times[role] = round(duration, 3)
        report.update({
            "status": "LOCAL_B3_RENDERED_BASIC_AV_QA_NOT_EDITED_QUALITY",
            "b3_mp4_sha256": digest,
            "b3_technical_qa": _EXPECTED_QA,
            "b3_measured_seconds": round(measured, 3),
            "a_b_b2_measured_seconds": times,
            "b3_vs_b2_duration_difference_seconds":
                round(measured - times["b2"], 3),
            "b3_render_plan_id": plan.id,
            "a_b_b2_b3_duration_compared": True,
            "new_b3_local_mp4_rendered": True,
        })
        save_semantic_review(report, receipt)
        return report

    if execute:
        with _exclusive_run_lock(preview_root, inputs["job_id"]):
            return process()
    return process()
