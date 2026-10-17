"""Pair submitted blind ratings with the *measured B2 to B3 source-edge change*.

This is a strictly observational, offline comparison: rating submissions are
unverified self-reports and cannot prove speech, creative quality or causality.
Original videos, reviewer materials and upstream edit evidence are untouched.
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from holoclip.render_b2_b3_blind_full_study import _prepare
from holoclip.render_b2_b3_blind_rating_ingest import (
    _MAX_FORM_BYTES, _read_json, _validate_rating, ingest_blind_ratings,
)
from holoclip.render_human_edit_context import DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE
from holoclip.render_plan import _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-b2-b3-blind-edge-association-v1"
_FIELDS = ("speech_naturalness", "narrative_continuity", "payoff_completion")
_ROLES = ("b2", "b3")
_MAX_CUTS = 32
_MAX_EXTENSION = 2.0
_EPS = 0.005


def _cut_pair(state: dict) -> dict:
    """Inspect only one edge from SHA-pinned canonical B2/B3 timelines."""
    context = state["state"]["basis"]["context"]
    side, index = context["trial_action"], context["source_cut_index"]
    if side not in ("EXTEND_START", "EXTEND_END") or type(index) is not int:
        raise ValueError("BLIND_ASSOCIATION_CUT_SELECTION_INVALID")
    roles = context["roles"]
    segments = {
        name: sorted(roles[name]["timeline"].segments,
                     key=lambda x: x.output_order)
        for name in _ROLES
    }
    old, new = segments["b2"], segments["b3"]
    if (not 1 <= len(old) == len(new) <= _MAX_CUTS or not 0 <= index < len(old)):
        raise ValueError("BLIND_ASSOCIATION_CUT_COUNT_INVALID")
    changed = []
    for idx, (before, after) in enumerate(zip(old, new, strict=True)):
        fields = (
            before.source_start, before.source_end,
            after.source_start, after.source_end,
        )
        if (not all(type(v) in (int, float) and math.isfinite(v) for v in fields)
                or before.source_asset_id != after.source_asset_id
                or before.output_order != after.output_order
                or before.output_order != idx
                or not 0 <= before.source_start < before.source_end <= 86400
                or not 0 <= after.source_start < after.source_end <= 86400):
            raise ValueError("BLIND_ASSOCIATION_SOURCE_TIMELINE_CHANGED")
        if (abs(before.source_start - after.source_start) > _EPS
                or abs(before.source_end - after.source_end) > _EPS):
            changed.append(idx)
    if changed != [index]:
        raise ValueError("BLIND_ASSOCIATION_NOT_SINGLE_EDGE_CHANGE")
    b2 = old[index]
    b3 = new[index]
    if side == "EXTEND_START":
        if abs(b2.source_end - b3.source_end) > _EPS:
            raise ValueError("BLIND_ASSOCIATION_OTHER_EDGE_CHANGED")
        extra = b2.source_start - b3.source_start
        old_boundary, new_boundary = b2.source_start, b3.source_start
    else:
        if abs(b2.source_start - b3.source_start) > _EPS:
            raise ValueError("BLIND_ASSOCIATION_OTHER_EDGE_CHANGED")
        extra = b3.source_end - b2.source_end
        old_boundary, new_boundary = b2.source_end, b3.source_end
    if not _EPS < extra <= _MAX_EXTENSION + _EPS:
        raise ValueError("BLIND_ASSOCIATION_OUTWARD_EXTENSION_INVALID")
    return {
        "status": "B3_OUTWARD_SINGLE_SOURCE_EDGE_EXTENSION",
        "source_cut_index": index,
        "edge": side,
        "source_asset_identity_disclosed": False,
        "b2_source_boundary_seconds": round(old_boundary, 3),
        "b3_source_boundary_seconds": round(new_boundary, 3),
        "b3_added_source_duration_seconds": round(extra, 3),
        "number_of_other_changed_cuts": 0,
        "asr_speech_or_creative_benefit_not_proven": True,
    }


def compare_blind_edge_association(
    *, rating_paths: list[Path], queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, output: Path | None = None,
) -> dict:
    """Bind untrusted paired scores to one verified editing change, never train."""
    params = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "proposal_index": proposal_index, "refinement_index": refinement_index,
        "candidate_index": candidate_index, "target_index": target_index,
    }

    def collect() -> dict:
        aggregate = ingest_blind_ratings(rating_paths=rating_paths, **params)
        if aggregate["status"] != "BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED":
            return {
                "version": VERSION,
                "status": "BLIND_ASSOCIATION_NO_VERIFIED_STUDY_AND_SUBMISSIONS",
                "study_evidence_status": aggregate["status"],
                "human_quality_gold_verified": False,
                "automatic_winner": None, "automatic_adoption_allowed": False,
                "ready": False, "production_ready": False,
                "publication_performed": False,
            }
        state = _prepare(params)
        if state["status"] != "SUPPORTED" or state["report"]["study_id"] != aggregate["study_id"]:
            raise ValueError("BLIND_ASSOCIATION_STUDY_CHANGED")
        edge = _cut_pair(state)
        assignment = {
            f"candidate-{i}": role
            for i, role in enumerate(state["order"], start=1)
        }
        paired = []
        for path in rating_paths:
            rating, _ = _read_json(path, _MAX_FORM_BYTES)
            scores, preference = _validate_rating(rating, aggregate["study_id"])
            role_scores = {assignment[slot]: score for slot, score in scores.items()}
            preferred = (assignment[preference] if preference in assignment
                         else preference)
            paired.append((role_scores, preferred))
        counts = Counter(value for _, value in paired)
        comparison = {}
        for dimension in _FIELDS:
            deltas = [
                scores["b3"][dimension] - scores["b2"][dimension]
                for scores, _ in paired
            ]
            comparison[dimension] = {
                "unverified_b3_minus_b2_score_sum": sum(deltas),
                "unverified_b3_minus_b2_mean": round(sum(deltas) / len(deltas), 3),
                "unverified_b3_higher_count": sum(v > 0 for v in deltas),
                "unverified_b2_higher_count": sum(v < 0 for v in deltas),
                "unverified_equal_count": sum(v == 0 for v in deltas),
            }
        return {
            "version": VERSION,
            "status": "BLIND_ASSOCIATION_UNVERIFIED_OBSERVATION_ONLY",
            "study_id": aggregate["study_id"],
            "rating_summary_receipts_sha256": aggregate["study_receipts_sha256"],
            "submitted_rating_sha256": aggregate["submitted_rating_sha256"],
            "unverified_submission_count": len(paired),
            "changed_edit": edge,
            "unverified_paired_role_score_differences": comparison,
            "unverified_preference_counts_by_edit": {
                key: counts[key] for key in (*_ROLES, "tie", "undecided")
            },
            "source_diverse_holdout_verified": False,
            "viewed_complete_videos_verified": False,
            "independent_raters_verified": False,
            "human_quality_gold_verified": False,
            "observational_association_not_causal_improvement": True,
            "caption_audio_repair_not_certified": True,
            "automatic_winner": None,
            "automatic_adoption_allowed": False,
            "originals_modified": False,
            "provider_requests": 0,
            "publication_performed": False,
            "ready": False, "production_ready": False,
        }

    report = collect()
    if output is not None:
        path = validate_semantic_output(Path(output))
        if report["status"] != "BLIND_ASSOCIATION_UNVERIFIED_OBSERVATION_ONLY":
            raise ValueError("BLIND_ASSOCIATION_NOT_ELIGIBLE_FOR_SAVE")
        state = _prepare(params)
        if state["status"] != "SUPPORTED":
            raise ValueError("BLIND_ASSOCIATION_STUDY_CHANGED_BEFORE_SAVE")
        with _exclusive_run_lock(
            _no_links(Path(preview_root)), state["evidence"]["job_id"],
        ):
            if collect() != report:
                raise ValueError("BLIND_ASSOCIATION_EVIDENCE_CHANGED_BEFORE_SAVE")
            save_semantic_review(report, path)
    return report
