"""Offline editorial-evidence triage for immutable source/B2/B3 playback packs.

Source ASR, independent edited ASR, short playback clips and technical
QA cannot establish speech naturalness, story continuity or a payoff.
No provider, media decode, download, auto-adoption or publication.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.render_b2_b3_local_review_pack import _basis, _report, _ROLES
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-b2-b3-editorial-evidence-v1"
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_CLIP_BYTES = 512 * 1024 * 1024
_MUTABLE_PACK_FIELDS = {
    "status", "source_media_sha256", "rendered_source_window",
    "created_review_clips", "number_of_review_clips",
}


def _read_pack(state: dict) -> tuple[dict, str] | None:
    """Independently verify the saved receipt, every clip, and current lineage."""
    path = _no_links(state["receipt"])
    if not path.exists() and not path.is_symlink():
        return None
    if (not path.is_file() or path.is_symlink()
            or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES):
        raise ValueError("EDITORIAL_EVIDENCE_PACK_RECEIPT_UNSAFE")
    raw = path.read_bytes()
    if not 0 < len(raw) <= _MAX_RECEIPT_BYTES:
        raise ValueError("EDITORIAL_EVIDENCE_PACK_RECEIPT_CHANGED")
    try:
        saved = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("EDITORIAL_EVIDENCE_PACK_JSON_INVALID") from exc
    expected = _report(state)
    if (not isinstance(saved, dict) or set(saved) != set(expected)
            or saved.get("status") != "LOCAL_REVIEW_PACK_LOCAL_CLIPS_UNRATED"
            or saved.get("number_of_review_clips") != 3
            or any(saved[k] != v for k, v in expected.items()
                   if k not in _MUTABLE_PACK_FIELDS)
            or not isinstance(saved.get("created_review_clips"), dict)
            or set(saved["created_review_clips"]) != set(_ROLES)):
        raise ValueError("EDITORIAL_EVIDENCE_PACK_LINEAGE_CHANGED")
    source_sha = saved.get("source_media_sha256")
    if (type(source_sha) is not str or len(source_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in source_sha)):
        raise ValueError("EDITORIAL_EVIDENCE_SOURCE_SHA_INVALID")
    window = saved.get("rendered_source_window")
    clock = state["selected"]["source_clock_seconds"]
    if (not isinstance(window, dict)
            or set(window) != {"start_seconds", "end_seconds", "expected_seconds"}
            or any(type(window[k]) not in (float, int)
                   or not math.isfinite(window[k]) for k in window)
            or not 0 <= window["start_seconds"] <= clock <= window["end_seconds"]
            or not .3 <= window["expected_seconds"] <= 6.1
            or abs(window["end_seconds"] - window["start_seconds"]
                   - window["expected_seconds"]) > .002):
        raise ValueError("EDITORIAL_EVIDENCE_SOURCE_WINDOW_INVALID")
    for role in _ROLES:
        row = saved["created_review_clips"][role]
        destination = _no_links(state["artifacts"][role])
        expected_window = (window if role == "source"
                           else state["selected"]["output_windows"][role])
        if (not isinstance(row, dict)
                or set(row) != {"filename", "sha256", "size_bytes",
                                "measured_seconds", "window"}
                or row["filename"] != destination.name
                or row["window"] != expected_window
                or type(row["size_bytes"]) is not int
                or not 0 < row["size_bytes"] <= _MAX_CLIP_BYTES
                or type(row["measured_seconds"]) not in (float, int)
                or not math.isfinite(row["measured_seconds"])
                or abs(row["measured_seconds"] -
                       expected_window["expected_seconds"]) > .75
                or type(row["sha256"]) is not str
                or len(row["sha256"]) != 64
                or any(ch not in "0123456789abcdef" for ch in row["sha256"])
                or not destination.is_file() or destination.is_symlink()
                or destination.stat().st_size != row["size_bytes"]
                or _hash(destination) != (row["sha256"], row["size_bytes"])):
            raise ValueError("EDITORIAL_EVIDENCE_PACK_CLIP_UNTRUSTED")
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(raw).digest():
        raise ValueError("EDITORIAL_EVIDENCE_PACK_RECEIPT_MUTATED")
    return saved, hashlib.sha256(raw).hexdigest()


def _evidence(state: dict, receipt: dict, receipt_sha: str) -> dict:
    """Automate bounded evidence organization, not creative-quality scoring."""
    target = state["selected"]["target"]
    windows = state["selected"]["output_windows"]
    clips = receipt["created_review_clips"]
    asr_hints = {
        role: {
            "edited_asr_status": target.get(f"{role}_asr_status"),
            "review_output_clock_seconds": target.get(f"{role}_output_start_seconds"),
            "spoken_word_accuracy_verified": False,
        } for role in ("b2", "b3")
    }
    projection = {
        role: {
            "source_clock_present_exactly_once": windows[role][
                "selected_source_clock_visible"],
            "edit_boundary_fallback": windows[role]["fallback_to_edit_boundary"],
            "source_to_output_clock_seconds": windows[role][
                "source_to_output_time_seconds"],
        } for role in ("b2", "b3")
    }
    return {
        "version": VERSION,
        "status": "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED",
        "job_id": receipt["job_id"],
        "trial_id": receipt["trial_id"],
        "b2_id": receipt["b2_id"],
        "b3_id": receipt["b3_id"],
        "review_target_index": receipt["review_target_index"],
        "review_target_kind": receipt["review_target_kind"],
        "review_target_priority": receipt["review_target_priority"],
        "source_clock_seconds": receipt["source_clock_seconds"],
        "source_asr_sha256": receipt["source_asr_sha256"],
        "four_way_diagnostics_sha256": receipt["four_way_diagnostics_sha256"],
        "b2_b3_asr_receipt_sha256": receipt["b2_b3_asr_receipt_sha256"],
        "playback_pack_receipt_sha256": receipt_sha,
        "source_media_sha256_claim_from_playback_pack_not_rehashed": receipt[
            "source_media_sha256"],
        "verified_playback_clips": {
            role: {
                "filename": clips[role]["filename"],
                "sha256": clips[role]["sha256"],
                "measured_seconds": clips[role]["measured_seconds"],
            } for role in _ROLES
        },
        "review_order": ["source", "b2", "b3"],
        "dimensions": {
            "speech_naturalness": {
                "status": "UNVERIFIED_REQUIRES_LISTENING",
                "asr_hints_not_spoken_word_proof": asr_hints,
                "questions": [
                    "Is any syllable clipped at the join?",
                    "Do speech and pauses sound natural in each full edit?",
                ],
                "score": None,
            },
            "narrative_continuity": {
                "status": "UNVERIFIED_REQUIRES_FULL_CONTEXT",
                "source_clock_projection_not_story_proof": projection,
                "questions": [
                    "Can an unfamiliar viewer follow the prior context?",
                    "Does the transition connect in each full edit?",
                ],
                "score": None,
            },
            "payoff_completion": {
                "status": "UNVERIFIED_REQUIRES_FULL_CONTEXT",
                "short_excerpt_seconds": {
                    role: clips[role]["measured_seconds"] for role in _ROLES
                },
                "questions": [
                    "Is the setup present in the complete edit?",
                    "Is the punchline or reaction completed rather than clipped?",
                ],
                "score": None,
            },
        },
        "source_and_edited_asr_not_ground_truth": True,
        "short_excerpts_cannot_certify_full_edit_quality": True,
        "human_auditory_review_completed": False,
        "real_footage_creative_quality_verified": False,
        "automatic_winner": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "new_provider_request": False,
        "source_video_downloaded": False,
        "ready": False,
        "production_ready": False,
    }


def run_editorial_evidence(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, save: bool = False,
) -> dict:
    """Read-only unless --save; immutable JSON remains unqualified evidence."""
    if (type(save) is not bool or type(target_index) is not int
            or not 0 <= target_index < 20):
        raise ValueError("EDITORIAL_EVIDENCE_ARGUMENT_INVALID")
    options = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "proposal_index": proposal_index,
        "refinement_index": refinement_index,
        "candidate_index": candidate_index,
    }

    def build() -> dict:
        state = _basis(options, target_index)
        if state["status"] != "SUPPORTED":
            return {"version": VERSION, "status": state["status"],
                    "ready": False, "production_ready": False}
        pack = _read_pack(state)
        if pack is None:
            return {
                "version": VERSION,
                "status": "LOCAL_EDITORIAL_EVIDENCE_PLAYBACK_PACK_NOT_RENDERED",
                "job_id": state["basis"]["context"]["job_id"],
                "review_target_index": target_index,
                "ready": False, "production_ready": False,
            }
        return _evidence(state, *pack)

    if not save:
        return build()
    preliminary = _basis(options, target_index)
    if preliminary["status"] != "SUPPORTED":
        return build()
    with _exclusive_run_lock(
        _no_links(Path(preview_root)), preliminary["basis"]["context"]["job_id"]
    ):
        report = build()
        if report["status"] != "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED":
            return report
        state = _basis(options, target_index)
        dest = _no_links(state["receipt"].with_name(
            f"{state['receipt'].stem}.creative-evidence.json"
        ))
        validate_semantic_output(dest)
        if build() != report:
            raise ValueError("EDITORIAL_EVIDENCE_CHANGED_BEFORE_SAVE")
        save_semantic_review(report, dest)
        return report
