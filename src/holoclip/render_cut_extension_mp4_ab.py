"""Explicit one-shot local MP4 A/B trial for an ASR-backed cut extension.

The existing QA-passed render is immutable baseline A; only trial B is newly
rendered. Source and policy evidence are rechecked before any FFmpeg call.
This remains technical A/B, not proof that the narrative edit improved.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from holoclip.domain import (
    ClipTimeline, TimelinePurpose, make_clip_timeline_id,
    make_timeline_segment_id, stable_id,
)
from holoclip.render_cut_extension_trials import preview_cut_extensions
from holoclip.render_plan import (
    _hash, _no_links, _probe, _target_file, compile_local_render_plan,
    render_local_preview,
)
from holoclip.render_ready_queue import (
    DEFAULT_PREVIEW_ROOT, _offline_render_policy,
)
from holoclip.render_source_queue import DEFAULT_QUEUE, _connect
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_same_source_geometry import (
    DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE, DEFAULT_TRANSCRIPT_ROOT,
)
from holoclip.render_same_source_semantic_review import save_semantic_review

VERSION = "holoclip-cut-extension-mp4-ab-v1"
_JOB_ID = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_TIMELINE = 1_048_576
_MAX_TRIAL_SECONDS = 180.0
_MAX_PROPOSALS_PER_JOB = 2


def _read_original(queue: Path, job_id: str, preview_root: Path) -> dict:
    """Read-only canonical job and original QA record, never initialize DB."""
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("LOCAL_EXTENSION_AB_JOB_ID_INVALID")
    conn = _connect(queue, create=False)
    if conn is None:
        raise ValueError("LOCAL_EXTENSION_AB_QUEUE_MISSING")
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT q.timeline_json,q.database_path,q.cache_root,q.policy_file,"
            "q.state AS queue_state,r.state AS render_state,r.output_path,"
            "r.output_sha256,r.render_plan_id,r.measured_duration "
            "FROM queue_jobs AS q JOIN render_jobs AS r ON q.job_id=r.job_id "
            "WHERE q.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or row["queue_state"] != "COMPLETE" or (
            row["render_state"] != "QA_PASSED"
        ):
            raise ValueError("LOCAL_EXTENSION_AB_BASELINE_NOT_QA_PASSED")
        raw = row["timeline_json"]
        if not isinstance(raw, str) or len(raw) > _MAX_TIMELINE:
            raise ValueError("LOCAL_EXTENSION_AB_BASELINE_TIMELINE_INVALID")
        baseline = ClipTimeline.model_validate_json(raw)
        baseline_file = _no_links(Path(row["output_path"]))
        if (baseline_file.parent != preview_root
                or baseline_file.name != f"{job_id}.mp4"
                or not baseline_file.is_file()
                or not isinstance(row["output_sha256"], str)
                or not _SHA.fullmatch(row["output_sha256"])
                or not isinstance(row["render_plan_id"], str)
                or not row["render_plan_id"]
                or not isinstance(row["measured_duration"], float | int)
                or row["measured_duration"] <= 0):
            raise ValueError("LOCAL_EXTENSION_AB_ORIGINAL_QA_UNVERIFIED")
        return {
            "baseline_timeline": baseline,
            "baseline_media": baseline_file,
            "baseline_sha256": row["output_sha256"],
            "baseline_measured_seconds": row["measured_duration"],
            "database": _no_links(Path(row["database_path"])),
            "cache_root": _no_links(Path(row["cache_root"])),
            "policy_file": _no_links(Path(row["policy_file"])),
        }
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_EXTENSION_AB_QUEUE_READ_UNAVAILABLE") from exc
    finally:
        conn.close()


def _trial_timeline(original: ClipTimeline, proposal: dict) -> ClipTimeline:
    ordered = sorted(original.segments, key=lambda x: x.output_order)
    before = proposal["baseline_source_intervals"]
    after = proposal["trial_source_intervals"]
    if (not isinstance(before, list) or not isinstance(after, list)
            or len(ordered) != len(before) or len(before) != len(after)
            or len(ordered) > 32 or not ordered):
        raise ValueError("LOCAL_EXTENSION_AB_TIMELINE_DISAGREEMENT")
    old_pairs = [(s.source_start, s.source_end) for s in ordered]
    proposal_before = [
        (part["source_start_seconds"], part["source_end_seconds"])
        for part in before
    ]
    if old_pairs != proposal_before:
        raise ValueError("LOCAL_EXTENSION_AB_TIMELINE_DISAGREEMENT")
    idx = proposal["source_cut_index"]
    if type(idx) is not int or not 0 <= idx < len(ordered):
        raise ValueError("LOCAL_EXTENSION_AB_CUT_INDEX_INVALID")
    new_ranges = [(part["source_start_seconds"], part["source_end_seconds"])
                  for part in after]
    new_start, new_end = new_ranges[idx]
    old_start, old_end = old_pairs[idx]
    side = proposal["trial_action"]
    if (side == "EXTEND_START"
            and not (new_start < old_start and new_end == old_end)
            or side == "EXTEND_END"
            and not (new_start == old_start and new_end > old_end)
            or side not in {"EXTEND_START", "EXTEND_END"}
            or any(x != y for i, (x, y) in enumerate(zip(
                old_pairs, new_ranges, strict=True)) if i != idx)):
        raise ValueError("LOCAL_EXTENSION_AB_NOT_A_SINGLE_EXTENSION")
    segments = []
    for i, segment in enumerate(ordered):
        if i == idx:
            segment = segment.model_copy(update={
                "id": make_timeline_segment_id(
                    segment.source_asset_id, new_start, new_end,
                    segment.output_order,
                ),
                "source_start": new_start, "source_end": new_end,
                "source_segment_ids": [],
                "purpose": TimelinePurpose.OTHER,
                "confidence": 0.0,
                "transition_hint": None,
            })
        segments.append(segment)
    total = sum(s.source_end - s.source_start for s in segments)
    if not 0 < total <= _MAX_TRIAL_SECONDS:
        raise ValueError("LOCAL_EXTENSION_AB_DURATION_BUDGET")
    return ClipTimeline(
        id=make_clip_timeline_id([s.id for s in segments]),
        segments=segments, estimated_duration=total,
        timeline_version=original.timeline_version + 1,
        evidence_ids=[],
    )


def prepare_cut_extension_mp4_trial(
    *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = 40,
    job_id: str | None = None,
    proposal_index: int = 0,
) -> dict:
    """Rebuild a trial from trusted local evidence, not an imported JSON plan."""
    if type(proposal_index) is not int or not 0 <= proposal_index < (
            _MAX_PROPOSALS_PER_JOB):
        raise ValueError("LOCAL_EXTENSION_AB_PROPOSAL_INDEX_INVALID")
    if job_id is not None and not _JOB_ID.fullmatch(job_id):
        raise ValueError("LOCAL_EXTENSION_AB_JOB_ID_INVALID")
    root = _no_links(preview_root)
    suggested = preview_cut_extensions(
        queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root,
        after_job_id=after_job_id, max_jobs=max_jobs,
    )
    if suggested["status"] != "LOCAL_CUT_EXTENSION_TRIALS_PROPOSED_UNQUALIFIED":
        raise ValueError("LOCAL_EXTENSION_AB_NO_VERIFIED_PROPOSAL")
    # At most two trials per job. The first eligible original source is the
    # automatic choice; a job ID can pin a particular already-completed cut.
    if job_id is None:
        job_id = suggested["proposals"][0]["job_id"]
    matches = [row for row in suggested["proposals"]
               if row["job_id"] == job_id]
    if proposal_index >= len(matches):
        raise ValueError("LOCAL_EXTENSION_AB_PROPOSAL_NOT_FOUND")
    proposal = matches[proposal_index]
    source = _read_original(queue, job_id, root)
    original = source["baseline_timeline"]
    trial = _trial_timeline(original, proposal)
    if len({s.source_asset_id for s in original.segments}) != 1:
        raise ValueError("LOCAL_EXTENSION_AB_MULTISOURCE_NOT_SUPPORTED")
    # Only a specific canonical source stream whose hash agrees with the
    # proposal can be used. Permission and full media cache are rechecked
    # again before performing the actual render.
    from holoclip.render_source_resolver import _canonical_assets
    asset_id = original.segments[0].source_asset_id
    asset = _canonical_assets(source["database"], {asset_id})[asset_id]
    if hashlib.sha256(asset.external_id.encode("utf-8")).hexdigest() != (
            proposal["canonical_source_stream_sha256"]):
        raise ValueError("LOCAL_EXTENSION_AB_SOURCE_ID_MISMATCH")
    trial_id = stable_id(
        "extensionab", VERSION, job_id, proposal["local_transcript_sha256"],
        trial.model_dump(mode="json"), length=20,
    )
    trial_name = f"{job_id}-extension-{trial_id}.mp4"
    target = _no_links(root / trial_name)
    sidecar = _no_links(target.with_suffix(".json"))
    return {
        "version": VERSION,
        "status": "LOCAL_EXTENSION_AB_TRIAL_NOT_RENDERED",
        "job_id": job_id,
        "trial_id": trial_id,
        "proposal_index": proposal_index,
        "baseline_mp4_filename": source["baseline_media"].name,
        "trial_mp4_filename": target.name,
        "trial_receipt_filename": sidecar.name,
        "baseline_mp4_sha256_from_previous_qa": source["baseline_sha256"],
        "baseline_measured_seconds_from_previous_qa":
            source["baseline_measured_seconds"],
        "source_stream_sha256": proposal["canonical_source_stream_sha256"],
        "human_training_reference_sha256": proposal["human_clip_identity_sha256"],
        "local_source_asr_sha256": proposal["local_transcript_sha256"],
        "trial_action": proposal["trial_action"],
        "source_cut_index": proposal["source_cut_index"],
        "additional_playback_seconds": proposal["additional_playback_seconds"],
        "baseline_timeline_sha256": hashlib.sha256(
            original.model_dump_json().encode()).hexdigest(),
        "trial_timeline_sha256": hashlib.sha256(
            trial.model_dump_json().encode()).hexdigest(),
        "trial_timeline": trial.model_dump(mode="json"),
        "original": source,
        "trial": trial,
        "output": target,
        "receipt": sidecar,
        "source_candidate_is_in_sample_not_quality_gold": True,
        "baseline_not_necessarily_same_caption_or_audio_postprocessing": True,
        "human_quality_improvement_verified": False,
        "technical_qa_not_editorial_qa": True,
        "youtube_contacted": False,
        "publication_performed": False,
        "auto_adoption_allowed": False,
        "ready": False,
        "production_ready": False,
    }


def _public_summary(prepared: dict) -> dict:
    return {
        key: value for key, value in prepared.items()
        if key not in {"original", "trial", "output", "receipt"}
    }


def run_cut_extension_mp4_trial(prepared: dict, *,
                                execute: bool = False,
                                approve_local_preview: bool = False) -> dict:
    """Explicit local FFmpeg trial B, keeping the original MP4 A unchanged."""
    result = _public_summary(prepared)
    if not execute:
        return result
    if approve_local_preview is not True:
        raise ValueError("LOCAL_EXTENSION_AB_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    orig = prepared["original"]
    trial = prepared["trial"]
    output = prepared["output"]
    receipt = prepared["receipt"]
    _target_file(output, [orig["baseline_media"]])
    if receipt.exists() or not receipt.parent.is_dir():
        raise ValueError("LOCAL_EXTENSION_AB_RECEIPT_EXISTS_OR_UNSAFE")
    # The original was QA-passed, but its bytes may have changed since that
    # receipt. Check current bytes and decoded video+audio before new FFmpeg.
    sha, size = _hash(orig["baseline_media"])
    if size <= 0 or sha != orig["baseline_sha256"]:
        raise ValueError("LOCAL_EXTENSION_AB_BASELINE_BYTES_CHANGED")
    baseline_seconds, video, audio = _probe(orig["baseline_media"])
    if not (video and audio) or abs(
            baseline_seconds - orig["baseline_measured_seconds"]) > .05:
        raise ValueError("LOCAL_EXTENSION_AB_BASELINE_AV_CHANGED")
    sources = resolve_cached_render_sources(
        trial, database=orig["database"], cache_root=orig["cache_root"],
        approve_local_preview=True,
    )
    _offline_render_policy(trial, sources, orig["policy_file"])
    plan = compile_local_render_plan(trial, sources, output)
    rendered = render_local_preview(plan)
    if rendered["status"] != "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY":
        raise ValueError("LOCAL_EXTENSION_AB_TRIAL_TECHNICAL_QA_FAILED")
    # Recheck original after the lengthy encode. Never declare a comparison
    # valid if the baseline changed while processing the trial.
    sha_after, size_after = _hash(orig["baseline_media"])
    if (sha_after, size_after) != (sha, size):
        raise ValueError("LOCAL_EXTENSION_AB_BASELINE_CHANGED_DURING_RENDER")
    trial_sha, trial_size = _hash(output)
    if trial_sha != rendered["output_sha256"] or trial_size <= 0:
        raise ValueError("LOCAL_EXTENSION_AB_TRIAL_OUTPUT_CHANGED")
    result.pop("trial_timeline", None)
    result.update({
        "status": "LOCAL_EXTENSION_AB_RENDERED_TECHNICAL_ONLY",
        "baseline_mp4_sha256_reverified": sha,
        "baseline_measured_seconds_reverified": round(baseline_seconds, 3),
        "trial_mp4_sha256": trial_sha,
        "trial_measured_seconds": rendered["measured_duration"],
        "measured_duration_difference_seconds": round(
            rendered["measured_duration"] - baseline_seconds, 3),
        "trial_plan_id": plan.id,
        "trial_av_qa": "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
        "trial_subtitle_evaluation": "NOT_MEASURED",
        "trial_audio_quality_vs_baseline": "NOT_MEASURED",
        "human_quality_improvement_verified": False,
        "auto_adoption_allowed": False,
        "ready": False,
        "production_ready": False,
    })
    # Create-only report. The new video, never the original, is the trial.
    # If a filesystem error prevents a sidecar, the new MP4 remains a
    # standalone unqualified preview; never delete another user's file.
    save_semantic_review(result, receipt)
    return result
