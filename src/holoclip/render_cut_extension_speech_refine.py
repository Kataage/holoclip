"""Bounded, source-ASR-timed alternative cut boundaries for existing finished A/B.

A complete utterance at an ASR-clock boundary is only a hypothesis, not proof
of audible dialogue, punchline quality or permission to render/publish.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from holoclip.render_cut_extension_mp4_ab import _trial_timeline
from holoclip.render_cut_extension_speech_audit import (
    _added_region, audit_finished_extension,
)
from holoclip.render_plan import _no_links
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_same_source_transcript_context import _read_transcript
from holoclip.render_source_resolver import _canonical_assets

VERSION = "holoclip-source-asr-extension-boundary-refinements-v1"
_EPS = 0.002
_MAX_BEYOND_TRIAL = 2.0
_MAX_TOTAL_EXTENSION = 8.0
_MAX_RESULTS = 2
_ACCEPTED_BASIS = {
    "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
    "FILENAME_ONLY_IDENTITY_UNVERIFIED",
}


def _crossings(segments: list, boundary: float) -> int:
    """Source-timed utterances straddling an exact cut boundary."""
    return sum(
        item.start + _EPS < boundary < item.end - _EPS
        for item in segments
    )


def _fully_contained(segments: list, start: float, end: float) -> int:
    return sum(
        item.start >= start - _EPS and item.end <= end + _EPS
        for item in segments
    )


def _boundary_refinements(plan: dict, segments: list) -> list[dict]:
    """Propose only a small outward fix of a *current* clipped ASR utterance."""
    span = _added_region(plan)
    ordered_a = sorted(
        plan["original"]["baseline_timeline"].segments,
        key=lambda item: item.output_order,
    )
    ordered_b = sorted(
        plan["trial"].segments, key=lambda item: item.output_order,
    )
    index = span["changed_cut_index"]
    original, trial = ordered_a[index], ordered_b[index]
    side = span["action"]
    current_edge = (
        trial.source_end if side == "EXTEND_END" else trial.source_start
    )
    current_crossings = _crossings(segments, current_edge)
    if not current_crossings:
        return []

    original_full = _fully_contained(
        segments, original.source_start, original.source_end,
    )
    proposals = []
    seen: set[float] = set()
    for utterance in segments:
        # Only a speech interval *currently cut by B* can produce a
        # candidate. No speculative changes elsewhere in the edit.
        if not utterance.start + _EPS < current_edge < utterance.end - _EPS:
            continue
        edge = (
            utterance.end if side == "EXTEND_END" else utterance.start
        )
        if edge in seen:
            continue
        seen.add(edge)
        if side == "EXTEND_END":
            proposed_start, proposed_end = trial.source_start, edge
            extra = proposed_end - original.source_end
            beyond = proposed_end - trial.source_end
        else:
            proposed_start, proposed_end = edge, trial.source_end
            extra = original.source_start - proposed_start
            beyond = trial.source_start - proposed_start
        if not (
            _EPS < beyond <= _MAX_BEYOND_TRIAL + _EPS
            and 0.25 <= extra <= _MAX_TOTAL_EXTENSION + _EPS
            and 0 <= proposed_start < proposed_end <= 86400
        ):
            continue
        if any(
            part.source_asset_id == trial.source_asset_id
            and min(proposed_end, part.source_end) -
            max(proposed_start, part.source_start) > _EPS
            for j, part in enumerate(ordered_b) if j != index
        ):
            continue
        remaining = _crossings(segments, edge)
        if remaining >= current_crossings:
            continue
        new_full = _fully_contained(
            segments, proposed_start, proposed_end,
        )
        if new_full <= original_full:
            continue
        before = [
            {"source_start_seconds": item.source_start,
             "source_end_seconds": item.source_end}
            for item in ordered_a
        ]
        after = [
            {"source_start_seconds": (
                proposed_start if j == index else item.source_start
             ), "source_end_seconds": (
                proposed_end if j == index else item.source_end
             )}
            for j, item in enumerate(ordered_b)
        ]
        try:
            proposed_timeline = _trial_timeline(
                plan["original"]["baseline_timeline"],
                {
                    "baseline_source_intervals": before,
                    "trial_source_intervals": after,
                    "source_cut_index": index,
                    "trial_action": side,
                },
            )
        except (ValueError, TypeError):
            continue
        proposals.append({
            "source_cut_index": index,
            "trial_action": side,
            "proposed_source_start_seconds": round(proposed_start, 3),
            "proposed_source_end_seconds": round(proposed_end, 3),
            "increment_beyond_current_trial_seconds": round(beyond, 3),
            "total_extension_from_baseline_seconds": round(extra, 3),
            "old_trial_boundary_asr_crossings": current_crossings,
            "proposed_boundary_asr_crossings": remaining,
            "baseline_fully_contained_asr_utterances": original_full,
            "proposed_fully_contained_asr_utterances": new_full,
            "proposed_timeline_sha256": hashlib.sha256(
                proposed_timeline.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "proposed_playback_seconds": round(
                proposed_timeline.estimated_duration, 3
            ),
            "source_clock_evidence_not_verified_audio": True,
            "requires_fresh_render_policy_and_source_media_qa": True,
            "unrendered_preview_only": True,
        })
    proposals.sort(key=lambda item: (
        item["proposed_boundary_asr_crossings"],
        item["increment_beyond_current_trial_seconds"],
        -item["proposed_fully_contained_asr_utterances"],
        item["proposed_source_start_seconds"],
        item["proposed_source_end_seconds"],
    ))
    return proposals[:_MAX_RESULTS]


def suggest_speech_boundary_refinements(
    prepared: dict, *, output: Path | None = None,
) -> dict:
    """Recheck a finished pair and local ASR; never render or adopt a cut."""
    # Inherits checksum/queue/holdout/QA lineage checks and ASR clock audit
    # from #243; no FFmpeg audio decode or external services.
    diagnostic = audit_finished_extension(prepared)
    if diagnostic["asr"]["status"] != (
        "SOURCE_ASR_TIMING_OBSERVED_UNVERIFIED_AUDIO"
    ):
        raise ValueError("LOCAL_SPEECH_REFINE_ASR_UNAVAILABLE")
    plan = prepared["trial"]
    asset_id = plan["trial"].segments[0].source_asset_id
    asset = _canonical_assets(
        plan["original"]["database"], {asset_id}
    )[asset_id]
    transcript = _read_transcript(
        _no_links(prepared["transcript_root"]), asset.external_id
    )
    if transcript is None:
        raise ValueError("LOCAL_SPEECH_REFINE_ASR_UNAVAILABLE")
    segments, digest, basis = transcript
    if (
        digest != plan["local_source_asr_sha256"]
        or basis not in _ACCEPTED_BASIS
    ):
        raise ValueError("LOCAL_SPEECH_REFINE_ASR_LINEAGE_CHANGED")

    alternatives = _boundary_refinements(plan, segments)
    report = {
        "version": VERSION,
        "status": (
            "LOCAL_SPEECH_BOUNDARY_ALTERNATIVES_UNRENDERED"
            if alternatives else "LOCAL_SPEECH_BOUNDARY_NO_SAFER_CLOCK_ALTERNATIVE"
        ),
        "job_id": plan["job_id"],
        "trial_id": plan["trial_id"],
        "matched_receipt_sha256": diagnostic["matched_receipt_sha256"],
        "baseline_final_sha256": diagnostic["baseline_final_sha256"],
        "trial_final_sha256": diagnostic["trial_final_sha256"],
        "source_asr_sha256": digest,
        "changed_cut_index": diagnostic["changed_cut_index"],
        "trial_boundary_crossing_count": (
            diagnostic["asr"]["boundary_overlap_trial_count"]
        ),
        "proposal_count": len(alternatives),
        "proposals": alternatives,
        "asr_text_exported": False,
        "source_asr_linguistically_verified": False,
        "audio_quality_improvement": None,
        "editorial_quality_improvement": None,
        "human_rating_available": False,
        "automatic_repair_executed": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "youtube_contacted": False,
        "ready": False,
        "production_ready": False,
    }
    if output is not None:
        save_semantic_review(report, _no_links(output))
    return report
