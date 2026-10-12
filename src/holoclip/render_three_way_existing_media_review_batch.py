"""Batched, source-pinned review questions for already measured local A/B/B2.

This only prepares *unrated* review targets from canonical existing media and
SHA-bound technical/acoustic receipts. No decoding, ranking or network calls.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from holoclip.render_cut_extension_three_way_triage import (
    prepare_three_way_review,
    run_three_way_review,
)
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock
from holoclip.render_three_way_existing_media_pilot import _completed_jobs
from holoclip.render_three_way_prerequisites import inspect_local_prerequisites
from holoclip.render_three_way_speech_coverage import compare_source_asr_coverage
from holoclip.render_three_way_existing_clip_asr_join import join_saved_edited_asr
from holoclip.render_three_way_b3_boundary_hints import propose_guarded_b3_boundaries
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_plan import _no_links
from holoclip.render_source_queue import DEFAULT_QUEUE

VERSION = "holoclip-three-way-existing-media-review-batch-v1"
_MAX_JOBS = 4
_MAX_RECEIPT_BYTES = 1024 * 1024
_ROLES = ("a", "b", "b2")


def _empty_case(job_id: str, status: str, *, next_stage: str | None = None,
                blocking_reason: str | None = None) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "next_stage": next_stage,
        "blocking_reason": blocking_reason,
        "review_target_count": 0,
        "review_target_counts_by_variant": {role: 0 for role in _ROLES},
        "review_target_counts_by_kind": {},
        "priority_review_windows": [],
        "saved_receipt_verified": False,
        "new_receipt_created": False,
        "source_asr_coverage": None,
        "existing_edited_audio_asr": None,
        "b3_boundary_repair_hints": None,
        "human_editorial_qa_completed": False,
        "best_variant": None,
        "ready": False,
    }


def _verify_existing(path: Path, expected: dict) -> bool:
    """A prior receipt is resumable only if its entire content still matches."""
    target = _no_links(path)
    if (not target.is_file() or target.is_symlink()
            or not 0 < target.stat().st_size <= _MAX_RECEIPT_BYTES):
        return False
    try:
        existing = json.loads(target.read_bytes())
    except (ValueError, UnicodeError):
        return False
    return isinstance(existing, dict) and existing == expected


def _report_case(job_id: str, plan: dict, coverage: dict, edited_asr: dict,
                 b3_hints: dict, *, status: str,
                 existing: bool = False, created: bool = False) -> dict:
    if (plan.get("status") != "LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED"
            or plan.get("job_id") != job_id
            or plan.get("best_variant") is not None
            or plan.get("ready") is not False):
        raise ValueError("LOCAL_REVIEW_BATCH_UNQUALIFIED_PLAN_REQUIRED")
    rows = plan["review_targets"]
    if not isinstance(rows, list) or not 3 <= len(rows) <= 72:
        raise ValueError("LOCAL_REVIEW_BATCH_TARGET_COUNT_INVALID")
    counts_by_role = {role: 0 for role in _ROLES}
    counts_by_kind: dict[str, int] = {}
    for row in rows:
        role, kind = row["variant"], row["kind"]
        if role not in counts_by_role or not isinstance(kind, str):
            raise ValueError("LOCAL_REVIEW_BATCH_TARGET_INVALID")
        counts_by_role[role] += 1
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
    result = _empty_case(job_id, status, next_stage="EVIDENCE_COMPLETE")
    result.update({
        "review_target_count": len(rows),
        "source_asr_coverage": coverage,
        "existing_edited_audio_asr": edited_asr,
        "b3_boundary_repair_hints": b3_hints,
        "review_target_counts_by_variant": counts_by_role,
        "review_target_counts_by_kind": dict(sorted(counts_by_kind.items())),
        "priority_review_windows": [
            {key: row[key] for key in (
                "variant", "kind", "priority", "output_start_seconds",
                "output_end_seconds",
            )} for row in rows[:9]
        ],
        "saved_receipt_verified": existing or created,
        "new_receipt_created": created,
    })
    return result


def _case(*, job_id: str, queue: Path, example_store: Path,
          growth_state: Path, preview_root: Path, transcript_root: Path,
          proposal_index: int, refinement_index: int, save: bool) -> dict:
    inputs = dict(
        job_id=job_id, queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root, proposal_index=proposal_index,
        refinement_index=refinement_index,
    )
    prior = inspect_local_prerequisites(**inputs)
    if prior["next_stage"] != "EVIDENCE_COMPLETE":
        return _empty_case(
            job_id, "LOCAL_REVIEW_BATCH_PREREQUISITE_INCOMPLETE",
            next_stage=prior["next_stage"],
            blocking_reason=prior["blocking_reason"],
        )

    # Lock when saving, including the final re-inspection, to avoid racing
    # another owner-approved A/B/B2 autopilot invocation.
    def collect() -> dict:
        again = inspect_local_prerequisites(**inputs)
        if again != prior or again["next_stage"] != "EVIDENCE_COMPLETE":
            return _empty_case(
                job_id, "LOCAL_REVIEW_BATCH_EVIDENCE_CHANGED",
                next_stage=again["next_stage"],
                blocking_reason=again["blocking_reason"],
            )
        prepared = prepare_three_way_review(**inputs, max_jobs=40)
        plan = run_three_way_review(prepared, save=False)
        coverage = compare_source_asr_coverage(prepared, plan)
        try:
            edited_asr = join_saved_edited_asr(inputs=inputs, coverage=coverage)
        except (ValueError, TypeError, OSError, UnicodeError):
            return _empty_case(
                job_id, "LOCAL_REVIEW_BATCH_EDITED_ASR_RECEIPT_UNTRUSTED",
                next_stage="EVIDENCE_COMPLETE",
            )
        b3_hints = propose_guarded_b3_boundaries(
            prepared=prepared, plan=plan, coverage=coverage, edited_asr=edited_asr,
        )
        target = _no_links(prepared["receipt"])
        if target.exists() or target.is_symlink():
            if not _verify_existing(target, plan):
                return _empty_case(
                    job_id, "LOCAL_REVIEW_BATCH_EXISTING_RECEIPT_UNTRUSTED",
                    next_stage="EVIDENCE_COMPLETE",
                )
            return _report_case(
                job_id, plan, coverage, edited_asr, b3_hints,
                status="LOCAL_REVIEW_BATCH_EXISTING_RECEIPT_VERIFIED",
                existing=True,
            )
        if not save:
            return _report_case(
                job_id, plan, coverage, edited_asr, b3_hints,
                status="LOCAL_REVIEW_BATCH_READ_ONLY_PREVIEW",
            )
        written = run_three_way_review(prepared, save=True)
        if (written != plan or not _verify_existing(target, plan)
                or compare_source_asr_coverage(prepared, plan) != coverage
                or join_saved_edited_asr(inputs=inputs, coverage=coverage) != edited_asr
                or propose_guarded_b3_boundaries(
                    prepared=prepared, plan=plan, coverage=coverage,
                    edited_asr=edited_asr,
                ) != b3_hints):
            raise ValueError("LOCAL_REVIEW_BATCH_SAVED_RECEIPT_UNTRUSTED")
        return _report_case(
            job_id, plan, coverage, edited_asr, b3_hints,
            status="LOCAL_REVIEW_BATCH_NEW_RECEIPT_SAVED_UNRATED",
            created=True,
        )

    if save:
        with _exclusive_run_lock(preview_root, job_id):
            return collect()
    return collect()


def run_existing_media_review_batch(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, max_jobs: int = 1,
    proposal_index: int = 0, refinement_index: int = 0,
    save: bool = False,
) -> dict:
    """Inspect up to four existing completed queue jobs, optionally save plans."""
    if (type(save) is not bool or type(max_jobs) is not int
            or not 1 <= max_jobs <= _MAX_JOBS
            or type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)):
        raise ValueError("LOCAL_REVIEW_BATCH_ARGUMENTS_INVALID")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(p)) for p in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    )
    jobs, total = _completed_jobs(queue, job_id=job_id, max_jobs=max_jobs)
    cases = []
    for jid in jobs:
        try:
            row = _case(
                job_id=jid, queue=queue, example_store=example_store,
                growth_state=growth_state, preview_root=preview_root,
                transcript_root=transcript_root,
                proposal_index=proposal_index,
                refinement_index=refinement_index, save=save,
            )
        except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
            row = _empty_case(jid, "LOCAL_REVIEW_BATCH_INPUT_OR_LOCK_BLOCKED")
        cases.append(row)
    return {
        "version": VERSION,
        "status": ("LOCAL_REVIEW_BATCH_SAVE_REQUEST_UNQUALIFIED" if save
                   else "LOCAL_REVIEW_BATCH_READ_ONLY_INVENTORY"),
        "completed_queue_jobs_matching_filter": total,
        "selected_job_count": len(jobs),
        "cases": cases,
        "verified_review_case_count": sum(
            row["saved_receipt_verified"] for row in cases
        ),
        "new_review_receipt_count": sum(
            row["new_receipt_created"] for row in cases
        ),
        "existing_edited_asr_case_count": sum(
            row["existing_edited_audio_asr"] is not None
            and row["existing_edited_audio_asr"]["existing_audio_asr_receipt_checked"]
            for row in cases
        ),
        "unrendered_b3_boundary_hypothesis_count": sum(
            row["b3_boundary_repair_hints"]["proposed_count"]
            for row in cases if row["b3_boundary_repair_hints"] is not None
        ),
        "total_unrated_review_target_count": sum(
            row["review_target_count"] for row in cases
        ),
        "manual_source_or_label_preparation_needed": False,
        "media_decoding_performed": False,
        "youtube_contacted": False,
        "external_model_requests": 0,
        "source_or_edited_media_modified": False,
        "auto_winner_selected": False,
        "human_editorial_qa_completed": False,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }
