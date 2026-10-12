"""Read-only stage-by-stage doctor for *existing* local A/B/B2 evaluation.

Every actionable stage is derived from the current canonical queue/holdout,
source ASR, local media and existing SHA-linked receipts. The doctor does not
download, render, decode video, request AI, repair partial writes or publish.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from holoclip.render_cut_extension_mp4_ab import prepare_cut_extension_mp4_trial
from holoclip.render_cut_extension_finish_ab import (
    _stage_paths, _trial_receipt, prepare_matched_finishing_trial,
)
from holoclip.render_cut_extension_speech_audit import _load_matched
from holoclip.render_cut_extension_speech_b2 import prepare_speech_b2_preview
from holoclip.render_cut_extension_speech_b2_finish import (
    _b2_receipt, prepare_speech_b2_finishing,
)
from holoclip.render_cut_extension_three_way_diagnostics import (
    _finished_b2_receipt, _source_evidence, prepare_three_way_diagnostics,
)
from holoclip.render_cut_extension_three_way_triage import _load_diagnostics
from holoclip.render_three_way_existing_media_pilot import (
    _completed_jobs, _existing_acoustic,
)
from holoclip.render_cut_extension_three_way_acoustic import prepare_three_way_acoustic
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_plan import _no_links
from holoclip.render_source_queue import DEFAULT_QUEUE

VERSION = "holoclip-existing-video-prerequisites-v1"
_STAGES = (
    "NO_VERIFIED_AB_PROPOSAL", "RENDER_B", "FINISH_AB", "NO_VERIFIED_B2_REFINEMENT",
    "RENDER_B2", "FINISH_B2", "MEASURE_TECHNICAL", "MEASURE_ACOUSTIC",
    "EVIDENCE_COMPLETE", "BLOCKED_UNVERIFIED",
)
_NEXT_COMMAND = {
    "RENDER_B": "render-cut-extension-mp4-trial",
    "FINISH_AB": "render-cut-extension-finished-ab",
    "RENDER_B2": "render-cut-extension-speech-b2",
    "FINISH_B2": "render-cut-extension-speech-b2-finish",
    "MEASURE_TECHNICAL": "render-cut-extension-three-way-diagnostics",
    "MEASURE_ACOUSTIC": "render-cut-extension-three-way-acoustic",
}
_MAX_CASES = 4


def _row(job_id: str, proposal_index: int, refinement_index: int,
         stage: str, *, verified: tuple[str, ...] = (), reason: str | None = None) -> dict:
    if stage not in _STAGES:
        raise ValueError("LOCAL_PREREQUISITES_UNSUPPORTED_STAGE")
    return {
        "job_id": job_id,
        "proposal_index": proposal_index,
        "refinement_index": refinement_index,
        "next_stage": stage,
        "next_command": _NEXT_COMMAND.get(stage),
        "completed_verified_stages": list(verified),
        "blocking_reason": reason,
        "operator_local_approval_required": stage in {
            "RENDER_B", "FINISH_AB", "RENDER_B2", "FINISH_B2",
        },
        "action_executed": False,
        "media_downloads": 0, "remote_requests": 0,
        "quality_verified": False, "ready": False,
    }


def _both_absent(left: Path, right: Path) -> bool:
    return not _no_links(left).exists() and not _no_links(right).exists()


def _unsafe_existing_stages(base: Path, stages: dict) -> bool:
    """An interrupted stage is not a fresh candidate for implicit rewrite."""
    paths = _stage_paths(base)
    return any(paths[stage].exists() for stage in ("audio", "caption", "burnin"))


def inspect_local_prerequisites(
    *, job_id: str, queue: Path, example_store: Path, growth_state: Path,
    preview_root: Path, transcript_root: Path, proposal_index: int = 0,
    refinement_index: int = 0,
) -> dict:
    kwargs = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "max_jobs": 40, "proposal_index": proposal_index,
    }
    verified: list[str] = []
    try:
        trial = prepare_cut_extension_mp4_trial(**kwargs)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error) as exc:
        code = str(exc)
        if code in {"LOCAL_EXTENSION_AB_NO_VERIFIED_PROPOSAL",
                    "LOCAL_EXTENSION_AB_PROPOSAL_NOT_FOUND"}:
            return _row(job_id, proposal_index, refinement_index,
                        "NO_VERIFIED_AB_PROPOSAL", reason="NO_CANONICAL_TRUSTED_PROPOSAL")
        return _row(job_id, proposal_index, refinement_index,
                    "BLOCKED_UNVERIFIED", reason="BASELINE_OR_AB_PROPOSAL_UNVERIFIED")
    verified.append("CANONICAL_SOURCE_AND_AB_PROPOSAL")
    if _both_absent(trial["output"], trial["receipt"]):
        return _row(job_id, proposal_index, refinement_index,
                    "RENDER_B", verified=tuple(verified))
    if not trial["output"].is_file() or not trial["receipt"].is_file():
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B_MEDIA_OR_RECEIPT_PARTIAL")
    try:
        _trial_receipt(trial)
        matched = prepare_matched_finishing_trial(**kwargs)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B_MEDIA_OR_SOURCE_PROOF_UNTRUSTED")
    verified.append("B_RENDER_QA")
    if not matched["matched_receipt"].exists():
        if _unsafe_existing_stages(trial["output"], matched["baseline_stages"]):
            return _row(job_id, proposal_index, refinement_index,
                        "BLOCKED_UNVERIFIED", verified=tuple(verified),
                        reason="B_FINISHING_FILES_PARTIAL_WITHOUT_RECEIPT")
        return _row(job_id, proposal_index, refinement_index,
                    "FINISH_AB", verified=tuple(verified))
    try:
        _load_matched(matched)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="MATCHED_AB_PROOF_UNTRUSTED")
    verified.append("AB_MATCHED_FINISHING")
    try:
        b2 = prepare_speech_b2_preview(
            **kwargs, refinement_index=refinement_index,
        )
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error) as exc:
        if str(exc) == "LOCAL_B2_NO_VERIFIED_REFINEMENT":
            return _row(job_id, proposal_index, refinement_index,
                        "NO_VERIFIED_B2_REFINEMENT", verified=tuple(verified),
                        reason="ASR_NO_SAFER_REFINEMENT_PROVEN")
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B2_ASR_OR_HOLDOUT_UNVERIFIED")
    verified.append("SOURCE_ASR_B2_REFINEMENT")
    if _both_absent(b2["output"], b2["receipt"]):
        return _row(job_id, proposal_index, refinement_index,
                    "RENDER_B2", verified=tuple(verified))
    if not b2["output"].is_file() or not b2["receipt"].is_file():
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B2_MEDIA_OR_RECEIPT_PARTIAL")
    context = {"b2": b2}
    try:
        _, digest = _b2_receipt(context)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B2_RENDER_PROOF_UNTRUSTED")
    verified.append("B2_RENDER_QA")
    finish_path = _no_links(
        b2["output"].with_name(f"{b2['output'].stem}.matched.json")
    )
    if not finish_path.exists():
        if _unsafe_existing_stages(b2["output"], matched["baseline_stages"]):
            return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                        verified=tuple(verified),
                        reason="B2_FINISHING_FILES_PARTIAL_WITHOUT_RECEIPT")
        return _row(job_id, proposal_index, refinement_index,
                    "FINISH_B2", verified=tuple(verified))
    try:
        _finished_b2_receipt({"b2": b2, "finished_receipt": finish_path}, digest)
        prepared = prepare_three_way_diagnostics(
            **kwargs, refinement_index=refinement_index,
        )
        source = _source_evidence(prepared)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="B2_FINISHING_OR_MEDIA_UNTRUSTED")
    verified.append("FINISHED_A_B_B2_SOURCE_LINKED")
    if not prepared["receipt"].exists():
        return _row(job_id, proposal_index, refinement_index,
                    "MEASURE_TECHNICAL", verified=tuple(verified))
    try:
        _load_diagnostics({"diagnostics": prepared}, source)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="TECHNICAL_REPORT_UNTRUSTED")
    verified.append("REAL_TECHNICAL_DIAGNOSTICS")
    try:
        acoustic = prepare_three_way_acoustic(
            **kwargs, refinement_index=refinement_index,
        )
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="ACOUSTIC_INPUTS_UNTRUSTED")
    if not acoustic["receipt"].exists():
        return _row(job_id, proposal_index, refinement_index,
                    "MEASURE_ACOUSTIC", verified=tuple(verified))
    try:
        _existing_acoustic(acoustic, source, {"status":
                           "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY"})
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return _row(job_id, proposal_index, refinement_index, "BLOCKED_UNVERIFIED",
                    verified=tuple(verified), reason="ACOUSTIC_REPORT_UNTRUSTED")
    verified.append("REAL_LOCAL_ACOUSTIC_SAMPLES")
    return _row(job_id, proposal_index, refinement_index,
                "EVIDENCE_COMPLETE", verified=tuple(verified))


def inspect_existing_media_prerequisites(
    *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, max_jobs: int = 2,
    proposal_index: int = 0, refinement_index: int = 0,
) -> dict:
    if type(proposal_index) is not int or proposal_index not in (0, 1):
        raise ValueError("LOCAL_PREREQUISITES_PROPOSAL_INVALID")
    if type(refinement_index) is not int or refinement_index not in (0, 1):
        raise ValueError("LOCAL_PREREQUISITES_REFINEMENT_INVALID")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(x)) for x in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    )
    jobs, total = _completed_jobs(queue, job_id=job_id, max_jobs=max_jobs)
    rows = [
        inspect_local_prerequisites(
            job_id=jid, queue=queue, example_store=example_store,
            growth_state=growth_state, preview_root=preview_root,
            transcript_root=transcript_root, proposal_index=proposal_index,
            refinement_index=refinement_index,
        ) for jid in jobs
    ]
    counts = {stage: sum(row["next_stage"] == stage for row in rows)
              for stage in _STAGES}
    return {
        "version": VERSION,
        "status": "LOCAL_EXISTING_MEDIA_PREREQUISITES_INSPECTED_NO_ACTION",
        "completed_queue_jobs_matching_filter": total,
        "jobs_selected": len(jobs),
        "stage_counts": counts,
        "cases": rows,
        "operator_can_choose_approved_offline_next_command": True,
        "all_commands_are_suggestions_not_executed": True,
        "source_video_downloads_performed": False,
        "media_decoding_performed": False,
        "remote_requests_performed": False,
        "original_queue_or_media_modified": False,
        "transcript_text_included": False,
        "local_paths_included": False,
        "creative_quality_verified": False,
        "ready": False, "production_ready": False,
        "auto_adoption_allowed": False, "publication_performed": False,
    }
