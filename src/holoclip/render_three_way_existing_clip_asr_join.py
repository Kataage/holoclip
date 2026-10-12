"""Read-only, fail-closed join of existing edited-audio ASR with source edit clocks.

An existing, opt-in three-way edited-audio ASR receipt is a *hint*, not an
authenticated speech transcript, independent human hearing or a quality grade.
No model invocation, media decoding, source download or new receipt here.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from holoclip.render_cut_extension_clip_asr_alignment import VERSION as TIME_VERSION
from holoclip.render_cut_extension_three_way_clip_asr import (
    VERSION as CLIP_VERSION,
    _basis,
    _evidence_for_focus,
    prepare_three_way_clip_asr,
    preview_clip_asr,
)
from holoclip.render_plan import _no_links

VERSION = "holoclip-existing-three-way-edited-asr-join-v1"
_ROLES = ("a", "b", "b2")
_MAX_RECEIPT_BYTES = 1024 * 1024
_WARNINGS = {
    "SHORT_TEXT_AMBIGUOUS_REVIEW",
    "PARTIAL_ASR_TEXT_OVERLAP_REVIEW",
    "LOW_ASR_TEXT_OVERLAP_AT_EXPECTED_TIME",
    "NO_EDITED_ASR_NEAR_EXPECTED_TIME",
    "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK",
    "MULTIPLE_PLAUSIBLE_TRANSCRIPT_OCCURRENCES",
}
_STATUSES = _WARNINGS | {"TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"}
_OVERLAP = {
    "LOW_TEXTUAL_OVERLAP_REVIEW_REQUIRED",
    "TEXTUAL_OVERLAP_OBSERVED_UNVERIFIED",
}
_FULL = "ASR_INTERVAL_INSIDE_SINGLE_EDIT_SEGMENT"
_PARTIAL = "ASR_INTERVAL_PARTLY_IN_EDIT"
_MAX_HINTS = 24


def _not_measured() -> dict:
    return {
        "version": VERSION,
        "status": "LOCAL_EDITED_AUDIO_ASR_RECEIPT_NOT_AVAILABLE",
        "source_asr_coverage_joined": False,
        "existing_audio_asr_receipt_checked": False,
        "audio_asr_warnings_by_variant": {role: None for role in _ROLES},
        "asr_review_hints": [],
        "asr_review_hint_count": 0,
        "data_upload_or_transcription_performed": False,
        "human_listening_verified": False,
        "source_asr_audio_verified": False,
        "missing_or_repeated_words_proven": False,
        "best_variant": None,
        "ready": False,
    }


def _number(value: object, *, minimum: float, maximum: float) -> bool:
    return (type(value) in (int, float)
            and math.isfinite(value) and minimum <= value <= maximum)


def _count(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _validate_role(
    role: str, report: dict, expected: list[dict], focus: dict,
) -> tuple[list[dict], int]:
    if not isinstance(report, dict):
        raise ValueError("LOCAL_EDITED_ASR_ROLE_INVALID")
    lo, hi = focus["output_start_seconds"], focus["output_end_seconds"]
    if (report.get("output_focus_start_seconds") != lo
            or report.get("output_focus_end_seconds") != hi
            or report.get("edited_audio_transcribed_from_local_crop") is not True
            or report.get("human_auditory_check_completed") is not False):
        raise ValueError("LOCAL_EDITED_ASR_FOCUS_CHANGED")
    recognized = report.get("recognized_segment_count")
    comparison = report.get("comparison")
    if (not _count(recognized, 25)
            or not _count(report.get("recognized_normalized_character_count"), 700)
            or not isinstance(comparison, list)
            or len(comparison) != len(expected)
            or report.get("source_fully_contained_asr_excerpt_count") != len(expected)
            or report.get("comparison_count") != len(expected)
            or type(report.get("no_recognized_text_is_not_proof_of_silence")) is not bool
            or report.get("repeated_words_or_missing_phrases_not_independently_verified")
            is not True):
        raise ValueError("LOCAL_EDITED_ASR_COMPARISON_INVALID")
    low = 0
    for item, reference in zip(comparison, expected):
        if (not isinstance(item, dict)
                or item.get("source_evidence_id") != reference["id"]
                or not _number(item.get("text_similarity_proxy"),
                               minimum=0, maximum=1)
                or type(item.get("exact_normalized_containment")) is not bool
                or item.get("signal") not in _OVERLAP
                or item.get("spoken_words_verified") is not False
                or item.get("source_asr_verified") is not False
                or item.get("missing_speech_proven") is not False):
            raise ValueError("LOCAL_EDITED_ASR_COMPARISON_INVALID")
        low += item["signal"] == "LOW_TEXTUAL_OVERLAP_REVIEW_REQUIRED"
    if report.get("low_overlap_review_count") != low:
        raise ValueError("LOCAL_EDITED_ASR_COUNT_MISMATCH")
    temporal = report.get("temporal_asr_review")
    if (not isinstance(temporal, dict)
            or temporal.get("version") != TIME_VERSION
            or temporal.get("status") != "LOCAL_ASR_TIME_ANCHORED_REVIEW_ONLY"
            or temporal.get("expected_utterance_count") != len(expected)
            or temporal.get("observed_segment_count") != recognized
            or temporal.get("human_audio_review_completed") is not False
            or temporal.get("ready") is not False
            or temporal.get("auto_adoption_allowed") is not False
            or temporal.get("source_asr_not_ground_truth") is not True
            or temporal.get("edited_asr_not_ground_truth") is not True
            or temporal.get("ambiguous_text_is_not_missing_speech") is not True
            or temporal.get("duplicate_text_is_not_proven_repetition") is not True):
        raise ValueError("LOCAL_EDITED_ASR_TEMPORAL_INVALID")
    alignments = temporal.get("source_utterance_alignment")
    unmatched = temporal.get("observed_outside_sampled_references")
    if (not isinstance(alignments, list) or len(alignments) != len(expected)
            or not isinstance(unmatched, list)
            or len(unmatched) > recognized
            or temporal.get("observed_segment_without_matched_sampled_reference_count")
            != len(unmatched)):
        raise ValueError("LOCAL_EDITED_ASR_ALIGNMENTS_INVALID")
    by_id = {ref["id"]: ref for ref in expected}
    if len(by_id) != len(expected):
        raise ValueError("LOCAL_EDITED_ASR_REFERENCE_DUPLICATED")
    seen = set()
    alerts = []
    for row in alignments:
        if not isinstance(row, dict):
            raise ValueError("LOCAL_EDITED_ASR_ALIGNMENT_INVALID")
        evidence_id = row.get("source_evidence_id")
        if evidence_id not in by_id or evidence_id in seen:
            raise ValueError("LOCAL_EDITED_ASR_ALIGNMENT_ID_INVALID")
        seen.add(evidence_id)
        ref = by_id[evidence_id]
        status = row.get("status")
        indices = row.get("observed_segment_indices")
        if (status not in _STATUSES
                or row.get("expected_output_start_seconds")
                != ref["output_start_seconds"]
                or row.get("expected_output_end_seconds")
                != ref["output_end_seconds"]
                or not isinstance(indices, list) or len(indices) > 2
                or any(type(i) is not int or not 1 <= i <= recognized
                       for i in indices)
                or row.get("word_alignment_verified") is not False
                or row.get("missing_speech_proven") is not False
                or row.get("repetition_proven") is not False
                or not _number(row.get("observed_text_similarity_proxy"),
                               minimum=0, maximum=1)
                or not _count(row.get("plausible_transcript_window_count"), 49)):
            raise ValueError("LOCAL_EDITED_ASR_ALIGNMENT_INVALID")
        observed_start = row.get("observed_output_start_seconds")
        observed_end = row.get("observed_output_end_seconds")
        if indices:
            if (not _number(observed_start, minimum=lo, maximum=hi + .75)
                    or not _number(observed_end, minimum=observed_start,
                                   maximum=hi + .75)
                    or observed_end <= observed_start):
                raise ValueError("LOCAL_EDITED_ASR_OBSERVED_CLOCK_INVALID")
        elif observed_start is not None or observed_end is not None:
            raise ValueError("LOCAL_EDITED_ASR_OBSERVED_CLOCK_INVALID")
        if status in _WARNINGS:
            alerts.append((evidence_id, status, ref))
    if temporal.get("time_anchor_warning_count") != len(alerts):
        raise ValueError("LOCAL_EDITED_ASR_WARNINGS_COUNT_INVALID")
    for extra in unmatched:
        if (not isinstance(extra, dict)
                or not _count(extra.get("observed_segment_index"), recognized)
                or extra["observed_segment_index"] == 0
                or not _number(extra.get("output_start_seconds"),
                               minimum=lo, maximum=hi + .75)
                or not _number(extra.get("output_end_seconds"),
                               minimum=extra["output_start_seconds"], maximum=hi + .75)
                or extra["output_end_seconds"] <= extra["output_start_seconds"]
                or extra.get("may_be_unsampled_source_context_or_asr_error") is not True):
            raise ValueError("LOCAL_EDITED_ASR_UNMATCHED_INVALID")
    return alerts, len(alerts)


def join_saved_edited_asr(*, inputs: dict, coverage: dict) -> dict:
    """Return reviewed clocks, or missing-evidence status; never invoke ASR."""
    prepared = prepare_three_way_clip_asr(**inputs, max_jobs=40)
    target: Path = _no_links(prepared["receipt"])
    if not target.exists() and not target.is_symlink():
        return _not_measured()
    if not target.is_file() or target.is_symlink():
        raise ValueError("LOCAL_EDITED_ASR_RECEIPT_UNSAFE")
    size = target.stat().st_size
    if not 0 < size <= _MAX_RECEIPT_BYTES:
        raise ValueError("LOCAL_EDITED_ASR_RECEIPT_SIZE_UNTRUSTED")
    raw = target.read_bytes()
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_EDITED_ASR_RECEIPT_JSON_INVALID") from exc
    preview = preview_clip_asr(prepared)
    observed, pack, focus = _basis(prepared)
    if not isinstance(report, dict):
        raise ValueError("LOCAL_EDITED_ASR_RECEIPT_INVALID")
    keys = (
        "job_id", "trial_id", "b2_id", "source_asr_sha256",
        "three_way_diagnostic_sha256", "review_plan_sha256",
        "finished_mp4_sha256", "expected_source_asr_excerpts_in_focus",
    )
    if (report.get("version") != CLIP_VERSION
            or report.get("status")
            != "LOCAL_CLIP_ASR_TEXTUAL_MISMATCH_HINTS_NOT_VERIFIED_SPEECH"
            or any(report.get(k) != preview[k] for k in keys)
            or report.get("source_asr_sha256") != coverage.get("source_asr_sha256")
            or report.get("clip_audio_transcribed") is not True
            or report.get("actual_finished_clip_audio_sent_to_asr_provider") is not True
            or report.get("transcript_text_exported") is not False
            or report.get("missing_or_repeated_speech_confirmed") is not False
            or report.get("human_editorial_qa_completed") is not False
            or report.get("auto_adoption_allowed") is not False
            or report.get("publication_performed") is not False
            or report.get("ready") is not False
            or report.get("production_ready") is not False
            or report.get("best_variant") is not None):
        raise ValueError("LOCAL_EDITED_ASR_LINEAGE_OR_STATUS_UNTRUSTED")
    roles = report.get("variants")
    if not isinstance(roles, dict) or set(roles) != set(_ROLES):
        raise ValueError("LOCAL_EDITED_ASR_VARIANTS_INVALID")

    # Match exact source-ASR segment clocks. Duplicate identical clocks are
    # deliberately unjoinable rather than silently assigned to a transcript.
    time_map: dict[tuple[float, float], dict | None] = {}
    for item in coverage["source_asr_intervals"]:
        key = (item["source_start_seconds"], item["source_end_seconds"])
        time_map[key] = None if key in time_map else item
    hints = []
    counts = {}
    for role in _ROLES:
        refs = _evidence_for_focus(pack["variants"][role]["excerpts"], focus[role])
        alerts, count = _validate_role(role, roles[role], refs, focus[role])
        counts[role] = count
        by_id = {
            r["evidence_id"]: r for r in pack["variants"][role]["excerpts"]
        }
        for evidence_id, status, ref in alerts:
            segment = by_id[evidence_id]
            item = time_map.get((
                segment["source_start_seconds"], segment["source_end_seconds"],
            ))
            inclusion = item["coverage_by_variant"][role] if item else None
            if inclusion == _FULL:
                kind = "SOURCE_CLOCK_INCLUDED_BUT_EDITED_ASR_REVIEW"
            elif inclusion == _PARTIAL:
                kind = "SOURCE_CLOCK_PARTIAL_AND_EDITED_ASR_REVIEW"
            else:
                kind = "EDITED_ASR_TEXT_OR_TIME_REVIEW_ONLY"
            hints.append({
                "variant": role,
                "source_evidence_id": evidence_id,
                "source_asr_interval_id": (
                    item["source_asr_interval_id"] if item else None
                ),
                "source_clock_inclusion": inclusion,
                "edited_audio_asr_status": status,
                "review_reason": kind,
                "output_start_seconds": ref["output_start_seconds"],
                "output_end_seconds": ref["output_end_seconds"],
                "requires_audio_listening": True,
            })
    if len(hints) > _MAX_HINTS:
        raise ValueError("LOCAL_EDITED_ASR_REVIEW_LIMIT_EXCEEDED")
    fresh_preview = preview_clip_asr(prepared)
    new_observed, new_pack, new_focus = _basis(prepared)
    if (fresh_preview != preview or new_pack != pack or new_focus != focus
            or new_observed["receipt_hashes"] != observed["receipt_hashes"]
            or new_observed["sibling_hashes"] != observed["sibling_hashes"]
            or target.read_bytes() != raw):
        raise ValueError("LOCAL_EDITED_ASR_CHANGED_DURING_JOIN")
    return {
        "version": VERSION,
        "status": "LOCAL_EXISTING_EDITED_ASR_DISCREPANCY_HINTS_UNRATED",
        "source_asr_coverage_joined": True,
        "existing_audio_asr_receipt_checked": True,
        "audio_asr_warnings_by_variant": counts,
        "asr_review_hints": hints,
        "asr_review_hint_count": len(hints),
        "data_upload_or_transcription_performed": False,
        "saved_receipt_not_cryptographically_authenticated": True,
        "human_listening_verified": False,
        "source_asr_audio_verified": False,
        "missing_or_repeated_words_proven": False,
        "best_variant": None,
        "ready": False,
    }
