"""Prioritize local B2/B3 ASR and technical-QA review without judging edits.

All observations are *hints*: source-ASR and edited ASR are neither speech
ground truth nor evidence that a scene/punchline works. Read-only by default;
no video decoding, network requests, extra transcriptions, or auto-adoption.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.render_b2_b3_finished_audio_asr import (
    VERSION as ASR_VERSION, _NO_WARNING, _WARNINGS, _basis,
    _comparison, _four_way, _public as _asr_preview,
    prepare_b2_b3_audio_asr,
)
from holoclip.render_cut_extension_clip_asr_alignment import (
    VERSION as ALIGN_VERSION,
)
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-b2-b3-audio-review-priorities-v1"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_TASKS = 20
_PRIORITY = {"HIGH_REVIEW": 0, "NORMAL_REVIEW": 1, "LOW_REVIEW": 2}
_ROLES = ("b2", "b3")


def _saved_audio_asr(basis: dict) -> tuple[dict, str] | None:
    """Require canonical current source/media/QA lineage and bounded ASR data."""
    path = _no_links(basis["receipt"])
    if not path.exists() and not path.is_symlink():
        return None
    if (not path.is_file() or path.is_symlink()
            or not 0 < path.stat().st_size <= _MAX_BYTES):
        raise ValueError("LOCAL_B2_B3_REVIEW_ASR_RECEIPT_UNSAFE")
    raw = path.read_bytes()
    try:
        receipt = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("LOCAL_B2_B3_REVIEW_ASR_JSON_INVALID") from exc
    preview = _asr_preview(basis)
    changing = {"status", "edited_audio_transcribed",
                "actual_finished_audio_sent_to_provider"}
    if (not isinstance(receipt, dict)
            or set(receipt) != set(preview) | {"variants", "comparison"}
            or any(receipt.get(k) != v for k, v in preview.items()
                   if k not in changing)
            or receipt.get("version") != ASR_VERSION
            or receipt.get("status")
            != "LOCAL_B2_B3_ASR_DISCREPANCIES_NOT_VERIFIED_SPEECH"
            or receipt.get("edited_audio_transcribed") is not True
            or receipt.get("actual_finished_audio_sent_to_provider") is not True):
        raise ValueError("LOCAL_B2_B3_REVIEW_ASR_LINEAGE_INVALID")
    results = receipt.get("variants")
    if not isinstance(results, dict) or set(results) != set(_ROLES):
        raise ValueError("LOCAL_B2_B3_REVIEW_ASR_ROLES_INVALID")
    for role in _ROLES:
        row = results[role]
        focus = basis["focus"][role]
        references = basis["references"][role]
        expected_by_id = {ref["id"]: ref for ref in references}
        if (not isinstance(row, dict)
                or row.get("output_focus_start_seconds")
                != focus["output_start_seconds"]
                or row.get("output_focus_end_seconds")
                != focus["output_end_seconds"]
                or row.get("human_auditory_check_completed") is not False
                or type(row.get("recognized_segment_count")) is not int
                or not 0 <= row["recognized_segment_count"] <= 25
                or type(row.get("low_overlap_review_count")) is not int
                or not 0 <= row["low_overlap_review_count"] <= 8):
            raise ValueError("LOCAL_B2_B3_REVIEW_ASR_OBSERVATION_UNTRUSTED")
        temporal = row.get("temporal_asr_review")
        if (not isinstance(temporal, dict)
                or temporal.get("version") != ALIGN_VERSION
                or temporal.get("status") != "LOCAL_ASR_TIME_ANCHORED_REVIEW_ONLY"
                or temporal.get("human_audio_review_completed") is not False
                or temporal.get("ready") is not False
                or temporal.get("missing_speech_proven") is True
                or temporal.get("auto_adoption_allowed") is not False
                or temporal.get("observed_segment_count")
                != row["recognized_segment_count"]):
            raise ValueError("LOCAL_B2_B3_REVIEW_ALIGNMENT_UNTRUSTED")
        aligned = temporal.get("source_utterance_alignment")
        if not isinstance(aligned, list) or len(aligned) != len(references):
            raise ValueError("LOCAL_B2_B3_REVIEW_ALIGNMENT_COUNT_CHANGED")
        seen = set()
        warning_count = 0
        for aligned_row in aligned:
            if not isinstance(aligned_row, dict):
                raise ValueError("LOCAL_B2_B3_REVIEW_ALIGNMENT_INVALID")
            identity = aligned_row.get("source_evidence_id")
            if identity not in expected_by_id or identity in seen:
                raise ValueError("LOCAL_B2_B3_REVIEW_ALIGNMENT_ID_CHANGED")
            seen.add(identity)
            ref = expected_by_id[identity]
            if (aligned_row.get("status") not in _WARNINGS | {_NO_WARNING}
                    or aligned_row.get("expected_output_start_seconds")
                    != ref["output_start_seconds"]
                    or aligned_row.get("expected_output_end_seconds")
                    != ref["output_end_seconds"]
                    or aligned_row.get("word_alignment_verified") is not False
                    or aligned_row.get("missing_speech_proven") is not False
                    or aligned_row.get("repetition_proven") is not False):
                raise ValueError("LOCAL_B2_B3_REVIEW_ALIGNMENT_STATUS_INVALID")
            warning_count += aligned_row["status"] in _WARNINGS
        if (temporal.get("expected_utterance_count") != len(references)
                or temporal.get("time_anchor_warning_count") != warning_count):
            raise ValueError("LOCAL_B2_B3_REVIEW_WARNING_COUNT_INVALID")
    if receipt.get("comparison") != _comparison(results, basis):
        raise ValueError("LOCAL_B2_B3_REVIEW_COMPARISON_INCONSISTENT")
    return receipt, hashlib.sha256(raw).hexdigest()


def _task(
    kind: str, priority: str, *, ref: dict | None = None,
    b2: dict | None = None, b3: dict | None = None,
) -> dict:
    if priority not in _PRIORITY:
        raise ValueError("LOCAL_B2_B3_REVIEW_PRIORITY_INVALID")
    return {
        "kind": kind, "priority": priority,
        "source_asr_interval_id": ref["id"] if ref else None,
        "source_start_seconds": ref["source_start_seconds"] if ref else None,
        "source_end_seconds": ref["source_end_seconds"] if ref else None,
        "b2_output_start_seconds": (
            b2["output_start_seconds"] if b2 else None
        ),
        "b3_output_start_seconds": (
            b3["output_start_seconds"] if b3 else None
        ),
        "b2_asr_status": b2["status"] if b2 else None,
        "b3_asr_status": b3["status"] if b3 else None,
        "reviewed_by_human": False,
        "missing_speech_or_repetition_proven": False,
        "creative_quality_proven": False,
    }


def _review(basis: dict, receipt: dict, audio_sha: str) -> dict:
    qa, qa_sha = _four_way(basis["context"])
    if qa_sha != basis["four_way_sha256"]:
        raise ValueError("LOCAL_B2_B3_REVIEW_FOUR_WAY_CHANGED")
    reports = receipt["variants"]
    align = {
        role: {
            row["source_evidence_id"]: row
            for row in reports[role]["temporal_asr_review"]["source_utterance_alignment"]
        } for role in _ROLES
    }
    ref = {
        role: {row["id"]: row for row in basis["references"][role]}
        for role in _ROLES
    }
    tasks = []
    for identity in sorted(set(ref["b2"]) | set(ref["b3"])):
        old, new = align["b2"].get(identity), align["b3"].get(identity)
        if old and new:
            if old["status"] == _NO_WARNING and new["status"] in _WARNINGS:
                kind, priority = "B3_NEW_ASR_WARNING", "HIGH_REVIEW"
            elif old["status"] in _WARNINGS and new["status"] in _WARNINGS:
                kind, priority = "BOTH_HAVE_ASR_WARNINGS", "NORMAL_REVIEW"
            elif old["status"] in _WARNINGS and new["status"] == _NO_WARNING:
                kind, priority = "B3_FEWER_WARNINGS_NOT_VERIFIED_BETTER", "LOW_REVIEW"
            else:
                continue
        elif new:
            kind, priority = "B3_NEW_SOURCE_ASR_INTERVAL_UNVERIFIED", "NORMAL_REVIEW"
        else:
            kind, priority = "B2_ONLY_SOURCE_ASR_INTERVAL_RECHECK", "HIGH_REVIEW"
        chosen = ref["b3"].get(identity) or ref["b2"][identity]
        tasks.append(_task(
            kind, priority, ref=chosen,
            b2=({**ref["b2"][identity], "status": old["status"]}
                if old else None),
            b3=({**ref["b3"][identity], "status": new["status"]}
                if new else None),
        ))
    old_diag = qa["variants"]["b2"]["technical_diagnostic"]
    new_diag = qa["variants"]["b3"]["technical_diagnostic"]
    tech = {}
    for key in ("flag_count", "black_interval_count", "freeze_interval_count"):
        old, new = old_diag.get(key), new_diag.get(key)
        if (type(old) is not int or type(new) is not int
                or not 0 <= old <= 600 or not 0 <= new <= 600):
            raise ValueError("LOCAL_B2_B3_REVIEW_QA_TECHNICAL_UNTRUSTED")
        tech[f"b2_{key}"] = old
        tech[f"b3_{key}"] = new
        tech[f"b3_minus_b2_{key}"] = new - old
    if tech["b3_minus_b2_flag_count"] > 0:
        tasks.append(_task("B3_MORE_TECHNICAL_FLAGS_NOT_QUALITY", "HIGH_REVIEW"))
    b2_samples = qa["variants"]["b2"]["edge_audio_samples"]
    b3_samples = qa["variants"]["b3"]["edge_audio_samples"]
    if (not isinstance(b2_samples, list) or not isinstance(b3_samples, list)
            or len(b2_samples) != len(b3_samples)):
        raise ValueError("LOCAL_B2_B3_REVIEW_AUDIO_WINDOWS_CHANGED")
    for old, new in zip(b2_samples, b3_samples):
        if old["kind"] != new["kind"]:
            raise ValueError("LOCAL_B2_B3_REVIEW_AUDIO_WINDOW_KIND_CHANGED")
        if (new["near_silent_energy_hint"] is True
                and old["near_silent_energy_hint"] is False):
            tasks.append(_task("B3_NEAR_SILENT_ENERGY_AT_EDIT_EDGE", "NORMAL_REVIEW"))
    tasks.sort(key=lambda t: (
        _PRIORITY[t["priority"]],
        t["source_start_seconds"] if t["source_start_seconds"] is not None else -1,
        t["kind"], t["source_asr_interval_id"] or "",
    ))
    if len(tasks) > _MAX_TASKS:
        raise ValueError("LOCAL_B2_B3_REVIEW_TASK_LIMIT_EXCEEDED")
    return {
        "version": VERSION,
        "status": "LOCAL_B2_B3_REVIEW_TARGETS_UNRATED_ASR_AND_QA",
        "job_id": basis["context"]["job_id"],
        "trial_id": basis["context"]["trial_id"],
        "b2_id": basis["context"]["b2_id"],
        "b3_id": basis["context"]["b3_id"],
        "source_cut_index": basis["context"]["source_cut_index"],
        "candidate_index": basis["context"]["candidate_index"],
        "source_asr_sha256": basis["source_sha256"],
        "four_way_diagnostics_sha256": qa_sha,
        "b2_b3_audio_asr_receipt_sha256": audio_sha,
        "finished_mp4_sha256": _asr_preview(basis)["finished_mp4_sha256"],
        "comparison": receipt["comparison"],
        "technical_signal_counts": tech,
        "review_target_count": len(tasks),
        "high_review_count": sum(t["priority"] == "HIGH_REVIEW" for t in tasks),
        "normal_review_count": sum(t["priority"] == "NORMAL_REVIEW" for t in tasks),
        "low_review_count": sum(t["priority"] == "LOW_REVIEW" for t in tasks),
        "targets": tasks,
        "source_asr_not_ground_truth": True,
        "edited_asr_not_ground_truth": True,
        "audio_energy_not_spoken_words": True,
        "technical_flags_not_editorial_quality": True,
        "no_targets_is_not_proof_of_speech_completeness": True,
        "transcript_text_exported": False,
        "video_or_audio_decoding_performed": False,
        "provider_requests_performed": False,
        "human_audio_review_completed": False,
        "editorial_quality_improvement": None,
        "automatic_winner": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


def run_b2_b3_review(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    save: bool = False,
) -> dict:
    """Read existing artifacts; optionally create one immutable review plan."""
    if type(save) is not bool:
        raise ValueError("LOCAL_B2_B3_REVIEW_SAVE_INVALID")
    params = dict(
        queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root, job_id=job_id,
        proposal_index=proposal_index, refinement_index=refinement_index,
        candidate_index=candidate_index,
    )
    def work() -> dict:
        basis = prepare_b2_b3_audio_asr(**params)
        if basis.get("status") == "LOCAL_B2_B3_ASR_NO_COMPLETED_JOB":
            return {
                "version": VERSION, "status": "LOCAL_B2_B3_REVIEW_NO_COMPLETED_JOB",
                "ready": False, "production_ready": False,
            }
        saved = _saved_audio_asr(basis)
        if saved is None:
            return {
                "version": VERSION, "status": "LOCAL_B2_B3_REVIEW_ASR_NOT_MEASURED",
                "job_id": basis["context"]["job_id"],
                "b3_id": basis["context"]["b3_id"],
                "four_way_diagnostics_sha256": basis["four_way_sha256"],
                "review_target_count": 0,
                "missing_asr_means_unknown_not_clean": True,
                "ready": False, "production_ready": False,
            }
        receipt, sha = saved
        result = _review(basis, receipt, sha)
        if save:
            output = _no_links(basis["receipt"].with_name(
                f"{basis['context']['found']['target'].stem}.b2-b3-review.json"
            ))
            validate_semantic_output(output)
            fresh = _basis(basis["inputs"], basis["context"]["candidate_index"])
            fresh["inputs"] = basis["inputs"]
            current = _saved_audio_asr(fresh)
            if current is None or current[1] != sha:
                raise ValueError("LOCAL_B2_B3_REVIEW_ASR_CHANGED_BEFORE_SAVE")
            if _review(fresh, current[0], current[1]) != result:
                raise ValueError("LOCAL_B2_B3_REVIEW_LINEAGE_CHANGED_BEFORE_SAVE")
            save_semantic_review(result, output)
        return result

    if save:
        # Use the same job lock as local render/review writers.
        # No decode/transcription occurs inside the lock.
        preliminary = prepare_b2_b3_audio_asr(**params)
        if preliminary.get("status") == "LOCAL_B2_B3_ASR_NO_COMPLETED_JOB":
            return work()
        with _exclusive_run_lock(_no_links(Path(preview_root)), preliminary["context"]["job_id"]):
            return work()
    return work()
