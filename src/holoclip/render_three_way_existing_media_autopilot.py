"""Bounded local-only A/B/B2 stage executor driven by the read-only doctor.

An explicit operator-approved invocation can advance existing *offline*
rendering and technical measurements, one proven stage at a time. No queue
creation, downloads, model requests, edit adoption, or publication.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from holoclip.render_cut_extension_mp4_ab import (
    prepare_cut_extension_mp4_trial, run_cut_extension_mp4_trial,
)
from holoclip.render_cut_extension_finish_ab import (
    prepare_matched_finishing_trial, run_matched_finishing_trial,
)
from holoclip.render_cut_extension_speech_b2 import (
    prepare_speech_b2_preview, run_speech_b2_preview,
)
from holoclip.render_cut_extension_speech_b2_finish import (
    prepare_speech_b2_finishing, finish_speech_b2,
)
from holoclip.render_cut_extension_three_way_diagnostics import (
    prepare_three_way_diagnostics, run_three_way_diagnostics,
)
from holoclip.render_cut_extension_three_way_acoustic import (
    prepare_three_way_acoustic, run_three_way_acoustic,
)
from holoclip.render_three_way_prerequisites import inspect_local_prerequisites
from holoclip.render_three_way_existing_media_pilot import _completed_jobs
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_plan import _no_links
from holoclip.render_source_queue import DEFAULT_QUEUE

VERSION = "holoclip-existing-three-way-offline-autopilot-v1"
_ORDER = (
    "RENDER_B", "FINISH_AB", "RENDER_B2", "FINISH_B2",
    "MEASURE_TECHNICAL", "MEASURE_ACOUSTIC", "EVIDENCE_COMPLETE",
)
_ACTIONS = set(_ORDER[:-1])
_RENDER_ACTIONS = {"RENDER_B", "FINISH_AB", "RENDER_B2", "FINISH_B2"}
_TERMINAL = {"EVIDENCE_COMPLETE", "BLOCKED_UNVERIFIED",
             "NO_VERIFIED_AB_PROPOSAL", "NO_VERIFIED_B2_REFINEMENT"}
_MAX_JOBS = 4
_MAX_STEPS = 6


@contextmanager
def _exclusive_run_lock(preview_root: Path, job_id: str):
    """Create-only short-lived lock. A stale lock is a blocker, never stolen."""
    root = _no_links(preview_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("LOCAL_AUTOPILOT_PREVIEW_ROOT_MISSING_OR_UNSAFE")
    target = _no_links(root / f".holoclip-three-way-autopilot-{job_id}.lock")
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("LOCAL_AUTOPILOT_CONCURRENT_OR_STALE_RUN") from exc
    try:
        os.close(fd)
        yield
    finally:
        target.unlink()


def _dispatch(stage: str, *, kwargs: dict, approval: bool) -> None:
    """Reconstruct canonical stage inputs; never trust a saved plan/command."""
    if stage in _RENDER_ACTIONS and approval is not True:
        raise ValueError("LOCAL_AUTOPILOT_LOCAL_RENDER_APPROVAL_REQUIRED")
    if stage == "RENDER_B":
        prep = prepare_cut_extension_mp4_trial(**kwargs)
        run_cut_extension_mp4_trial(prep, execute=True, approve_local_preview=True)
    elif stage == "FINISH_AB":
        prep = prepare_matched_finishing_trial(**kwargs)
        result = run_matched_finishing_trial(
            prep, execute=True, approve_local_preview=True,
        )
        if result.get("status") != "LOCAL_MATCHED_AB_STAGES_TECHNICALLY_COMPARABLE":
            raise ValueError("LOCAL_AUTOPILOT_AB_FINISH_INCOMPLETE")
    elif stage == "RENDER_B2":
        prep = prepare_speech_b2_preview(**kwargs)
        run_speech_b2_preview(prep, execute=True, approve_local_preview=True)
    elif stage == "FINISH_B2":
        prep = prepare_speech_b2_finishing(**kwargs)
        result = finish_speech_b2(
            prep, execute=True, approve_local_preview=True,
        )
        if result.get("status") != "LOCAL_B2_FINISH_STAGES_TECHNICALLY_COMPARABLE":
            raise ValueError("LOCAL_AUTOPILOT_B2_FINISH_INCOMPLETE")
    elif stage == "MEASURE_TECHNICAL":
        prep = prepare_three_way_diagnostics(**kwargs)
        result = run_three_way_diagnostics(prep, execute=True)
        if result.get("status") != "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY":
            raise ValueError("LOCAL_AUTOPILOT_TECHNICAL_INCOMPLETE")
    elif stage == "MEASURE_ACOUSTIC":
        prep = prepare_three_way_acoustic(**kwargs)
        result = run_three_way_acoustic(prep, execute=True)
        if result.get("status") != "LOCAL_ACOUSTIC_MEASURED_SIGNALS_NOT_SPEECH_PROOF":
            raise ValueError("LOCAL_AUTOPILOT_ACOUSTIC_INCOMPLETE")
    else:
        raise ValueError("LOCAL_AUTOPILOT_UNKNOWN_OR_UNSAFE_STAGE")


def _case_base(job_id: str, first: dict) -> dict:
    return {
        "job_id": job_id,
        "initial_stage": first["next_stage"],
        "final_stage": first["next_stage"],
        "blocking_reason": first["blocking_reason"],
        "executed_stages": [],
        "stage_count": 0,
        "status": "LOCAL_AUTOPILOT_INSPECTED_ONLY",
        "creative_quality_verified": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
    }


def _advance_job(
    *, job_id: str, queue: Path, example_store: Path, growth_state: Path,
    preview_root: Path, transcript_root: Path, proposal_index: int,
    refinement_index: int, execute: bool, approval: bool, remaining: int,
) -> dict:
    inspect_kwargs = {
        "job_id": job_id, "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root,
        "proposal_index": proposal_index,
        "refinement_index": refinement_index,
    }
    stage_kwargs = {**inspect_kwargs, "max_jobs": 40}
    first = inspect_local_prerequisites(**inspect_kwargs)
    output = _case_base(job_id, first)
    if not execute:
        output["status"] = "LOCAL_AUTOPILOT_PREVIEW_ONLY"
        return output

    if first["next_stage"] in _TERMINAL:
        output["status"] = "LOCAL_AUTOPILOT_NO_SAFE_ACTION"
        return output
    if first["next_stage"] in _RENDER_ACTIONS and not approval:
        output["status"] = "LOCAL_AUTOPILOT_RENDER_APPROVAL_REQUIRED"
        return output
    if remaining <= 0:
        output["status"] = "LOCAL_AUTOPILOT_STEP_BUDGET_EXHAUSTED"
        return output

    with _exclusive_run_lock(preview_root, job_id):
        # The queue, ASR and local files may have changed after inventory.
        current = inspect_local_prerequisites(**inspect_kwargs)
        if current != first:
            output["final_stage"] = current["next_stage"]
            output["blocking_reason"] = current["blocking_reason"]
            output["status"] = "LOCAL_AUTOPILOT_PLAN_CHANGED_NO_ACTION"
            return output
        for _ in range(remaining):
            stage = current["next_stage"]
            if stage in _TERMINAL:
                output["status"] = "LOCAL_AUTOPILOT_STOPPED_AT_TERMINAL_STAGE"
                break
            if stage not in _ACTIONS:
                output["status"] = "LOCAL_AUTOPILOT_UNKNOWN_STAGE_BLOCKED"
                break
            if stage in _RENDER_ACTIONS and not approval:
                output["status"] = "LOCAL_AUTOPILOT_RENDER_APPROVAL_REQUIRED"
                break
            try:
                _dispatch(stage, kwargs=stage_kwargs, approval=approval)
                # The output is only accepted after the full canonical
                # hashes and receipts have been independently rederived.
                following = inspect_local_prerequisites(**inspect_kwargs)
            except (ValueError, OSError, TypeError, UnicodeError, sqlite3.Error):
                output["status"] = "LOCAL_AUTOPILOT_STAGE_FAILED_STOPPED"
                following = inspect_local_prerequisites(**inspect_kwargs)
                output["final_stage"] = following["next_stage"]
                output["blocking_reason"] = following["blocking_reason"]
                break
            # If an intervention or a partial output changed the expected
            # path to a blocker, never proceed to another stage.
            output["executed_stages"].append(stage)
            output["stage_count"] += 1
            output["final_stage"] = following["next_stage"]
            output["blocking_reason"] = following["blocking_reason"]
            if (following["next_stage"] in _ORDER
                    and _ORDER.index(following["next_stage"]) ==
                    _ORDER.index(stage) + 1):
                current = following
                output["status"] = "LOCAL_AUTOPILOT_ADVANCED_UNQUALIFIED"
                continue
            output["status"] = "LOCAL_AUTOPILOT_UNEXPECTED_TRANSITION_STOPPED"
            break
        else:
            output["status"] = "LOCAL_AUTOPILOT_STEP_BUDGET_EXHAUSTED"
        if output["final_stage"] == "EVIDENCE_COMPLETE":
            output["status"] = "LOCAL_AUTOPILOT_TECHNICAL_EVIDENCE_COMPLETE_UNQUALIFIED"
    return output


def run_existing_media_autopilot(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, max_jobs: int = 1,
    proposal_index: int = 0, refinement_index: int = 0,
    max_steps: int = 6, execute: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Make strictly bounded offline progress without silently adopting edits."""
    if (type(execute) is not bool or type(approve_local_preview) is not bool
            or type(max_jobs) is not int or not 1 <= max_jobs <= _MAX_JOBS
            or type(max_steps) is not int or not 1 <= max_steps <= _MAX_STEPS
            or type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)):
        raise ValueError("LOCAL_AUTOPILOT_ARGUMENTS_INVALID")
    inputs = [
        _no_links(Path(p)) for p in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    ]
    queue, example_store, growth_state, preview_root, transcript_root = inputs
    jobs, total = _completed_jobs(queue, job_id=job_id, max_jobs=max_jobs)
    reports = []
    remaining = max_steps
    for jid in jobs:
        try:
            row = _advance_job(
                job_id=jid, queue=queue, example_store=example_store,
                growth_state=growth_state, preview_root=preview_root,
                transcript_root=transcript_root,
                proposal_index=proposal_index, refinement_index=refinement_index,
                execute=execute, approval=approve_local_preview,
                remaining=remaining,
            )
        except (ValueError, OSError, TypeError, UnicodeError, sqlite3.Error):
            row = {
                "job_id": jid, "initial_stage": "BLOCKED_UNVERIFIED",
                "final_stage": "BLOCKED_UNVERIFIED",
                "blocking_reason": "LOCAL_AUTOPILOT_CONCURRENT_OR_UNSAFE_INPUT",
                "executed_stages": [], "stage_count": 0,
                "status": "LOCAL_AUTOPILOT_INPUT_OR_LOCK_BLOCKED",
                "creative_quality_verified": False,
                "auto_adoption_allowed": False,
                "publication_performed": False, "ready": False,
            }
        remaining -= row["stage_count"]
        reports.append(row)
    return {
        "version": VERSION,
        "status": (
            "LOCAL_AUTOPILOT_EXPLICIT_LOCAL_EXECUTION_UNQUALIFIED" if execute
            else "LOCAL_AUTOPILOT_READ_ONLY_PREVIEW"
        ),
        "completed_queue_jobs_matching_filter": total,
        "selected_job_count": len(jobs),
        "jobs": reports,
        "total_executed_stage_count": max_steps - remaining,
        "stage_execution_budget": max_steps,
        "downloaded_video_count": 0, "remote_model_requests": 0,
        "publication_performed": False,
        "media_edited_or_replaced": False,
        "new_only_offline_preview_artifacts_possible": execute,
        "automatic_winner_selected": False,
        "real_editorial_quality_verified": False,
        "auto_adoption_allowed": False, "ready": False,
        "production_ready": False,
    }
