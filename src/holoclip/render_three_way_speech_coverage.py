"""Compare edit-clock source-ASR retention near the changed A/B/B2 cut.

This measures *timeline inclusion*, not heard words, intelligibility, comedy
quality, factual language accuracy, or clip publishability. All input is local,
already canonical and independently SHA-checked.
"""
from __future__ import annotations

import hashlib
import math

from holoclip.render_cut_extension_three_way_diagnostics import _source_evidence
from holoclip.render_same_source_transcript_context import _read_transcript
from holoclip.render_source_resolver import _canonical_assets
from holoclip.render_plan import _no_links

VERSION = "holoclip-source-asr-edit-clock-coverage-v1"
_ROLES = ("a", "b", "b2")
_FULL = "ASR_INTERVAL_INSIDE_SINGLE_EDIT_SEGMENT"
_PARTIAL = "ASR_INTERVAL_PARTLY_IN_EDIT"
_ABSENT = "ASR_INTERVAL_OUTSIDE_EDIT"
_AMBIGUOUS = "MULTIPLE_SOURCE_EDIT_OVERLAPS_UNVERIFIED"
_EPS = .002
_MAX_NEARBY = 64
_MAX_EDGE_SPREAD_SECONDS = 12.0


def classify_source_clock_interval(utterance, cuts: list) -> str:
    """Conservatively classify one ASR clock interval, not audible speech."""
    if (not math.isfinite(utterance.start) or not math.isfinite(utterance.end)
            or not 0 <= utterance.start < utterance.end <= 86400):
        raise ValueError("LOCAL_COVERAGE_INVALID_ASR_CLOCK")
    intersections = [
        cut for cut in cuts
        if min(utterance.end, cut.source_end)
        - max(utterance.start, cut.source_start) > _EPS
    ]
    if len(intersections) > 1:
        # Even if both fragments reassemble, ordering/repetition is unknown.
        return _AMBIGUOUS
    if not intersections:
        return _ABSENT
    cut = intersections[0]
    if (cut.source_start <= utterance.start + _EPS
            and cut.source_end >= utterance.end - _EPS):
        return _FULL
    return _PARTIAL


def compare_source_asr_coverage(prepared: dict, plan: dict) -> dict:
    """Report bounded cross-edit ASR timing deltas without exporting ASR text."""
    observed = _source_evidence(prepared["diagnostics"])
    if (plan.get("status") != "LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED"
            or plan.get("job_id") != observed["job_id"]
            or plan.get("source_asr_sha256") != observed["asr_sha256"]
            or plan.get("three_way_diagnostic_sha256") is None):
        raise ValueError("LOCAL_COVERAGE_REVIEW_LINEAGE_UNVERIFIED")

    b2 = prepared["diagnostics"]["finished"]["b2"]
    trial = b2["prepared"]["trial"]
    original = trial["original"]
    timeline_by_role = {
        "a": original["baseline_timeline"],
        "b": trial["trial"],
        "b2": b2["timeline"],
    }
    index = trial["source_cut_index"]
    side = trial["trial_action"]
    if type(index) is not int or side not in {"EXTEND_START", "EXTEND_END"}:
        raise ValueError("LOCAL_COVERAGE_CHANGED_CUT_INVALID")
    cuts = {
        role: sorted(timeline.segments, key=lambda part: part.output_order)
        for role, timeline in timeline_by_role.items()
    }
    if (not cuts["a"] or any(len(cuts[role]) != len(cuts["a"])
                              or not 0 <= index < len(cuts[role])
                              for role in _ROLES)):
        raise ValueError("LOCAL_COVERAGE_TIMELINE_GEOMETRY_INVALID")
    asset_ids = {
        cut.source_asset_id for role in _ROLES for cut in cuts[role]
    }
    if len(asset_ids) != 1:
        raise ValueError("LOCAL_COVERAGE_MULTISOURCE_NOT_ATTESTED")
    asset_id = next(iter(asset_ids))
    source = _canonical_assets(original["database"], {asset_id})[asset_id]
    transcript_root = _no_links(b2["prepared"]["transcript_root"])
    parsed = _read_transcript(transcript_root, source.external_id)
    if parsed is None or parsed[1] != observed["asr_sha256"] or parsed[2] not in {
        "FILENAME_ONLY_IDENTITY_UNVERIFIED",
        "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
    }:
        raise ValueError("LOCAL_COVERAGE_TRANSCRIPT_NOT_VERIFIED")
    utterances, digest, basis = parsed
    edge = {}
    for role in _ROLES:
        cut = cuts[role][index]
        edge[role] = cut.source_start if side == "EXTEND_START" else cut.source_end
        if not math.isfinite(edge[role]) or not 0 <= edge[role] <= 86400:
            raise ValueError("LOCAL_COVERAGE_INVALID_EDIT_BOUNDARY")
    low, high = min(edge.values()), max(edge.values())
    if high - low > _MAX_EDGE_SPREAD_SECONDS:
        raise ValueError("LOCAL_COVERAGE_EDIT_EDGE_SPREAD_UNBOUNDED")
    # Observe two seconds either side of all three changed-cut edges.
    start, end = max(0.0, low - 2.0), min(86400.0, high + 2.0)
    selected = [
        (i, item) for i, item in enumerate(utterances)
        if item.end > start and item.start < end and bool(item.text.strip())
    ]
    if len(selected) > _MAX_NEARBY:
        raise ValueError("LOCAL_COVERAGE_TOO_MANY_ASR_SEGMENTS")
    counts = {role: {_FULL: 0, _PARTIAL: 0, _ABSENT: 0, _AMBIGUOUS: 0}
              for role in _ROLES}
    rows = []
    for i, item in selected:
        coverage = {
            role: classify_source_clock_interval(item, cuts[role])
            for role in _ROLES
        }
        for role in _ROLES:
            counts[role][coverage[role]] += 1
        key = f"{digest}|{i}|{item.start:.6f}|{item.end:.6f}"
        rows.append({
            "source_asr_interval_id": hashlib.sha256(key.encode()).hexdigest()[:20],
            "source_start_seconds": round(item.start, 3),
            "source_end_seconds": round(item.end, 3),
            "coverage_by_variant": coverage,
            "b_full_not_a": coverage["b"] == _FULL
                            and coverage["a"] in {_ABSENT, _PARTIAL},
            "b2_full_not_b": coverage["b2"] == _FULL
                             and coverage["b"] in {_ABSENT, _PARTIAL},
            "b2_partial_not_b": coverage["b2"] == _PARTIAL
                                and coverage["b"] == _ABSENT,
            "b2_loses_b_full_interval": coverage["b"] == _FULL
                                       and coverage["b2"] != _FULL,
        })
    # No schema permits transcript excerpts, sensitive file paths or tokens.
    metrics = {
        "b_gains_full_vs_a": sum(row["b_full_not_a"] for row in rows),
        "b2_gains_full_vs_b": sum(row["b2_full_not_b"] for row in rows),
        "b2_adds_partial_vs_b": sum(row["b2_partial_not_b"] for row in rows),
        "b2_loses_b_full": sum(row["b2_loses_b_full_interval"] for row in rows),
    }
    fresh = _read_transcript(transcript_root, source.external_id)
    if (fresh is None or fresh[1] != digest or fresh[2] != basis
            or _source_evidence(prepared["diagnostics"])["receipt_hashes"]
            != observed["receipt_hashes"]):
        raise ValueError("LOCAL_COVERAGE_SOURCE_CHANGED_DURING_SCAN")
    return {
        "version": VERSION,
        "status": "LOCAL_SOURCE_ASR_EDIT_CLOCK_COVERAGE_NOT_HEARD_SPEECH",
        "source_asr_sha256": digest,
        "source_asr_identity_basis": basis,
        "changed_cut_index": index,
        "extension_side": side,
        "boundary_source_seconds_by_variant": {
            role: round(edge[role], 3) for role in _ROLES
        },
        "source_focus_start_seconds": round(start, 3),
        "source_focus_end_seconds": round(end, 3),
        "asr_interval_count": len(rows),
        "coverage_counts_by_variant": counts,
        "paired_coverage_deltas": metrics,
        "source_asr_intervals": rows,
        "asr_text_included": False,
        "source_asr_audio_verified": False,
        "source_clock_inclusion_is_not_audible_speech": True,
        "joined_or_repeated_segments_cannot_prove_speech_completeness": True,
        "best_variant": None,
        "automatic_edit_adoption": False,
        "ready": False,
        "production_ready": False,
    }
