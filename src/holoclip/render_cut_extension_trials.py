"""Bounded, reversible-on-paper extensions of an existing source cut.

Use real completed-preview *source selection*, nearby trusted human-only
regions, and local ASR timestamps to suggest short alternative cut
boundaries. The source MP4 is never modified/rendered and this does not
prove that an extension improves editing, speech or narrative quality.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.render_same_source_geometry import (
    DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE, DEFAULT_TRANSCRIPT_ROOT,
    compare_same_source_edit_geometry,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_source_worker import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_selection_presence import (
    parse_generated_intervals, source_span_coverage,
)
from holoclip.render_same_source_narrative_reference import _union, _shared
from holoclip.render_same_source_semantic_review import _clean
from holoclip.render_same_source_semantic_review import save_semantic_review

VERSION = "holoclip-asr-grounded-cut-extension-trials-v1"
_MAX_EXTRA_SECONDS = 8.0
_MAX_GAP_SECONDS = 4.0
_MAX_CUT_SECONDS = 600.0
_MAX_JOBS = 40
_MAX_PROPOSALS_PER_JOB = 2
_MAX_PROPOSALS = 12
_ACCEPTED_IDENTITY = {
    "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
    "TRUSTED_EXAMPLE_MANIFEST_AND_SOURCE_URL_ID_MATCH_AUDIO_UNVERIFIED",
}


def _valid_span(start: object, end: object) -> bool:
    return (
        type(start) in (float, int) and type(end) in (float, int)
        and math.isfinite(start) and math.isfinite(end)
        and 0 <= start < end <= 86400
    )


def _nearby_extensions(
    cuts: list[tuple[float, float]], human_window: tuple[float, float],
) -> list[tuple[int, str, tuple[float, float], float]]:
    """Extend only one adjacent cut; no jumps, duplicate source or reordering."""
    left, right = human_window
    if not _valid_span(left, right):
        raise ValueError("LOCAL_EXTENSION_HUMAN_WINDOW_INVALID")
    results = []
    for i, (start, end) in enumerate(cuts):
        for side, new_span, distance, added in (
            ("EXTEND_START", (left, end), start - right, start - left),
            ("EXTEND_END", (start, right), left - end, right - end),
        ):
            if (not 0 <= distance <= _MAX_GAP_SECONDS
                    or not 0 < added <= _MAX_EXTRA_SECONDS
                    or not _valid_span(*new_span)):
                continue
            if any(
                min(new_span[1], other_end) -
                max(new_span[0], other_start) > 1e-6
                for j, (other_start, other_end) in enumerate(cuts)
                if j != i
            ):
                continue
            results.append((i, side, new_span, added))
    return results


def _asr_gained(
    clips_before: list[tuple[float, float]],
    clips_after: list[tuple[float, float]],
    excerpts: object,
) -> list[dict]:
    if not isinstance(excerpts, list) or len(excerpts) > 3:
        raise ValueError("LOCAL_EXTENSION_ASR_EXCERPTS_INVALID")
    evidence = []
    for excerpt in excerpts:
        if not isinstance(excerpt, dict):
            raise ValueError("LOCAL_EXTENSION_ASR_EXCERPT_SHAPE_INVALID")
        start, end = excerpt.get("start_seconds"), excerpt.get("end_seconds")
        utterance = _clean(excerpt.get("unverified_asr_text"), 160)
        if not _valid_span(start, end) or utterance is None:
            continue
        before = source_span_coverage(start, end, clips_before)
        after = source_span_coverage(start, end, clips_after)
        gained = round(
            after["source_clock_selected_seconds"] -
            before["source_clock_selected_seconds"], 3,
        )
        # Evidence must refer to source-time speech newly included by
        # the trial, not already-present context or unrelated ASR.
        if gained < .25:
            continue
        evidence.append({
            "asr_source_start_seconds": start,
            "asr_source_end_seconds": end,
            "asr_text_sha256": hashlib.sha256(utterance.encode()).hexdigest(),
            "source_clock_seconds_gained": gained,
            "baseline_source_clock_selection":
                before["source_clock_selection_status"],
            "trial_source_clock_selection":
                after["source_clock_selection_status"],
            "actual_audible_speech_verified": False,
        })
    return evidence


def propose_cut_extensions(report: dict) -> dict:
    """Create text-free, bounded candidate timelines from read-only geometry."""
    summary = {
        "version": VERSION,
        "status": "LOCAL_CUT_EXTENSION_GATES_BLOCKED",
        "scope": "CURRENT_VERIFIED_RENDER_JOB_PAGE_ONLY",
        "source_geometry_version": report.get("version"),
        "source_growth_state_sha256": report.get("growth_state_sha256"),
        "proposed_count": 0,
        "proposal_limit": _MAX_PROPOSALS,
        "proposals": [],
        "human_reference_is_training_in_sample_not_editorial_gold": True,
        "source_time_asr_not_audible_speech_or_context_truth": True,
        "trial_cuts_not_validated_against_source_media_duration": True,
        "original_mp4_not_modified_or_rerendered": True,
        "editing_improvement_measured": False,
        "quality_precision": None,
        "quality_recall": None,
        "remote_requests_performed": 0,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
    }
    if not isinstance(report, dict):
        return summary
    queue = report.get("inspection_queue")
    corpus = report.get("corpus")
    if (report.get("holdout_reservation_metadata_available") is not True
            or report.get("heldout_qualification_allowed") is not False
            or report.get("ready") is not False
            or report.get("auto_adoption_allowed") is not False
            or not isinstance(corpus, dict)
            or corpus.get("holdout_training_conflicts") != 0
            or not isinstance(queue, dict)
            or queue.get("source_reservation_metadata_verified") is not True
            or queue.get("auto_adoption_allowed") is not False
            or queue.get("ready") is not False
            or not isinstance(queue.get("results"), list)
            or len(queue["results"]) > 10):
        return summary
    summary["status"] = "LOCAL_CUT_EXTENSION_NO_GROUNDED_TRIALS"
    out = []
    seen_jobs: set[str] = set()
    for entry in queue["results"]:
        job = entry.get("job_id")
        if not isinstance(job, str) or job in seen_jobs:
            continue
        seen_jobs.add(job)
        if entry.get("evaluation_split") != "TRAINING_REFERENCE_IN_SAMPLE":
            continue
        context = entry.get("transcript_context")
        if (not isinstance(context, dict)
                or context.get("status") != "TRANSCRIPT_CONTEXT_AVAILABLE"
                or context.get("source_identity_basis") not in _ACCEPTED_IDENTITY
                or context.get("local_asr_audio_independently_verified") is not False
                or not isinstance(context.get("transcript_sha256"), str)):
            continue
        try:
            cuts = parse_generated_intervals(
                entry.get("generated_source_intervals"))
        except (ValueError, TypeError):
            continue
        total = sum(end-start for start, end in cuts)
        if not 0 < total <= _MAX_CUT_SECONDS:
            continue
        # Only use the human-only source window actually sampled by the
        # verified transcript context, NEVER infer evidence from generic
        # human-only geometry windows that have no source-timed ASR.
        windows = context.get("windows")
        if not isinstance(windows, list) or len(windows) > 2:
            continue
        job_proposals = []
        for window in windows:
            if (not isinstance(window, dict)
                    or window.get("selection_side") != "human_only"):
                continue
            hs, he = window.get("source_start_seconds"), window.get(
                "source_end_seconds")
            if not _valid_span(hs, he):
                continue
            asr = window.get("asr")
            if not isinstance(asr, dict):
                continue
            for cut_index, action, proposed, added in _nearby_extensions(
                cuts, (hs, he)
            ):
                changed = list(cuts)
                changed[cut_index] = proposed
                if total + added > _MAX_CUT_SECONDS:
                    continue
                try:
                    excerpt_evidence = _asr_gained(
                        cuts, changed, asr.get("within")
                    )
                except (ValueError, TypeError):
                    continue
                if not excerpt_evidence:
                    continue
                baseline_unique = _union(cuts)
                trial_unique = _union(changed)
                source_gain = sum(end-start for start,end in trial_unique) - (
                    sum(end-start for start,end in baseline_unique))
                if not 0 < source_gain <= _MAX_EXTRA_SECONDS + 1e-6:
                    continue
                result = {
                    "job_id": job,
                    "canonical_source_stream_sha256":
                        entry.get("canonical_source_stream_sha256"),
                    "human_clip_identity_sha256":
                        entry.get("human_clip_identity_sha256"),
                    "local_transcript_sha256": context["transcript_sha256"],
                    "source_cut_index": cut_index,
                    "trial_action": action,
                    "human_only_reference_window": {
                        "source_start_seconds": hs, "source_end_seconds": he,
                    },
                    "baseline_source_intervals": [
                        {"source_start_seconds": a, "source_end_seconds": b}
                        for a,b in cuts
                    ],
                    "trial_source_intervals": [
                        {"source_start_seconds": a, "source_end_seconds": b}
                        for a,b in changed
                    ],
                    "baseline_playback_seconds": round(total, 3),
                    "trial_playback_seconds": round(total + added, 3),
                    "additional_playback_seconds": round(added, 3),
                    "newly_selected_unique_source_seconds": round(source_gain, 3),
                    "asr_clock_evidence": excerpt_evidence,
                    "asr_source_clock_seconds_gained": round(
                        sum(x["source_clock_seconds_gained"]
                            for x in excerpt_evidence), 3,
                    ),
                    "source_time_window_not_verified_narrative_role": True,
                    "preview_only_no_automatic_edit": True,
                }
                job_proposals.append(result)
        job_proposals.sort(key=lambda x: (
            -x["asr_source_clock_seconds_gained"],
            x["additional_playback_seconds"],
            x["source_cut_index"], x["trial_action"],
        ))
        out.extend(job_proposals[:_MAX_PROPOSALS_PER_JOB])
    out.sort(key=lambda x: (
        -x["asr_source_clock_seconds_gained"],
        x["additional_playback_seconds"],
        x["job_id"], x["source_cut_index"], x["trial_action"],
    ))
    summary["proposals"] = out[:_MAX_PROPOSALS]
    summary["proposed_count"] = len(summary["proposals"])
    if summary["proposed_count"]:
        summary["status"] = "LOCAL_CUT_EXTENSION_TRIALS_PROPOSED_UNQUALIFIED"
    return summary


def preview_cut_extensions(
    *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = _MAX_JOBS,
    output: Path | None = None,
) -> dict:
    original = compare_same_source_edit_geometry(
        queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root,
        after_job_id=after_job_id, max_jobs=max_jobs,
    )
    proposed = propose_cut_extensions(original)
    proposed["source_page_cursor"] = original.get("after_job_id")
    proposed["source_next_cursor"] = original.get("next_cursor")
    proposed["source_page_has_more"] = original.get("has_more")
    if output is not None:
        save_semantic_review(proposed, output)
    return proposed
