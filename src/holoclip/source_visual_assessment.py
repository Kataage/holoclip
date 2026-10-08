"""Reference-free comparison of shortlisted original video candidates.

A deterministic, auditably derived *assessment* of checked positive evidence
is NOT an estimated source probability, a source selection, negative evidence
about unsampled windows or an editorial quality evaluation. All candidates
remain eligible for unattended investigation and no training split changes.
"""
from __future__ import annotations

import hashlib
import json

from holoclip.source_visual_ledger import VERSION as LEDGER_VERSION
from holoclip.source_visual_ledger import checked_ledger

VERSION = "source-visual-candidate-assessment-v1"

_NO_PROMOTION = {
    "source_identity_verified": False,
    "original_video_absolute_pts_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "event_split_verified": False,
    "used_in_learning_or_holdout": False,
    "source_candidate_selected": False,
    "candidate_rejected_from_missing_match": False,
    "calibrated_source_probability": None,
}


def _digest(ledger: dict) -> str:
    """Bind the summary to exactly the validated, bounded sampled events."""
    payload = json.dumps(
        ledger["events"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def assess_visual_candidates(
    ledger: dict, *, clip_id: str, candidate_ids: list[str],
) -> dict:
    """Summarize only observed video support; never choose an original stream.

    In particular, 0 saved matches means NO SAVED POSITIVE OBSERVATION,
    not evidence that a source is wrong. Recorded windows are not a complete
    source search, and repeated finishes are not independent samples.
    """
    checked = checked_ledger(ledger, clip_id=clip_id, candidate_ids=candidate_ids)
    observed = checked["events"]
    counts = checked["summary"]["independent_two_sided_window_counts"]
    active = sorted(k for k in candidate_ids if counts.get(k, 0) > 0)
    competing = len(active) > 1
    candidates = []
    for source_id in candidate_ids:
        own = [e for e in observed if e["source_stream_id"] == source_id]
        finish_spans = sorted({
            tuple(e["finished_range_seconds"]) for e in own
        })
        source_spans = sorted({
            tuple(e["source_requested_range_seconds"]) for e in own
        })
        independence = counts.get(source_id, 0)
        if independence >= 2:
            tier = "MULTIPLE_DISTINCT_WINDOWS_UNREVIEWED"
        elif independence == 1:
            tier = "SINGLE_DISTINCT_WINDOW_UNREVIEWED"
        else:
            tier = "NO_SAVED_POSITIVE_VISUAL_WINDOWS"
        candidates.append({
            "source_stream_id": source_id,
            "evidence_tier": tier,
            "recorded_positive_observations": len(own),
            "independent_two_sided_window_lower_bound": independence,
            "distinct_finished_contexts_observed": len(finish_spans),
            "distinct_original_contexts_observed": len(source_spans),
            "sampled_finished_positive_ranges_seconds": [
                list(span) for span in finish_spans
            ],
            "sampled_original_positive_ranges_seconds": [
                list(span) for span in source_spans
            ],
            "has_competing_candidate_positive_evidence": (
                competing and source_id in active
            ),
            "source_rejected": False,
            "unseen_video_coverage_verified": False,
            "source_identity_verified": False,
        })
    if competing:
        status = "UNKNOWN_COMPETING_VISUAL_ORIGINALS"
        next_step = "COMPARE_COMPETING_ORIGINALS_AUTOMATICALLY"
    elif active and counts[active[0]] >= 2:
        status = "UNREVIEWED_DIVERSE_SINGLE_ORIGINAL_HYPOTHESIS"
        next_step = "SEEK_INDEPENDENT_ORIGINAL_IDENTITY_CHECK"
    elif active:
        status = "UNREVIEWED_SINGLE_CONTEXT_ORIGINAL_HYPOTHESIS"
        next_step = "SEEK_ADDITIONAL_DISTINCT_VIDEO_CONTEXTS"
    else:
        status = "UNKNOWN_NO_SAVED_POSITIVE_SOURCE_EVIDENCE"
        next_step = "CONTINUE_BOUNDED_AUTONOMOUS_SOURCE_SCANNING"
    return {
        "version": VERSION,
        "source_evidence_version": LEDGER_VERSION,
        "clip_id": clip_id,
        "ledger_events_sha256": _digest(checked),
        "status": status,
        "next_autonomous_evidence_step": next_step,
        "candidate_count": len(candidate_ids),
        "candidate_observations": candidates,
        "candidate_ids_with_positive_evidence": active,
        "competing_positive_candidates": competing,
        "source_identity_decision": "UNKNOWN_NOT_SELECTED",
        "evidence_is_partial_positive_only": True,
        "source_native_timestamps_available": False,
        "negative_source_labels_available": False,
        **_NO_PROMOTION,
    }


def checked_visual_candidate_assessment(
    saved: dict | None, ledger: dict, *, clip_id: str,
    candidate_ids: list[str],
) -> dict:
    """Refuse stale, inconsistent or upgraded saved assessments on resume."""
    expected = assess_visual_candidates(
        ledger, clip_id=clip_id, candidate_ids=candidate_ids,
    )
    if saved is not None and saved != expected:
        raise ValueError(
            "Saved visual candidate assessment is stale, malformed or promoted"
        )
    return expected
