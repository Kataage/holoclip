"""Conservative, never-rendered B3 boundary hypotheses after verified A/B/B2.

Propose only when BOTH B2's source-ASR clock splits an utterance and an
independently re-observed edited-audio ASR timing/text hint is nearby. Neither
signal proves clipped speech or warrants adoption. No new media or network.
"""
from __future__ import annotations

import hashlib
import math

from holoclip.render_cut_extension_mp4_ab import _trial_timeline
from holoclip.render_plan import _no_links
from holoclip.render_same_source_transcript_context import _read_transcript
from holoclip.render_source_resolver import _canonical_assets

VERSION = "holoclip-guarded-b3-boundary-hypotheses-v1"
_FULL = "ASR_INTERVAL_INSIDE_SINGLE_EDIT_SEGMENT"
_PARTIAL = "ASR_INTERVAL_PARTLY_IN_EDIT"
_ROLES = ("a", "b", "b2")
_MAX_EXTRA_SECONDS = 2.0
_MAX_FROM_BASELINE_SECONDS = 8.0
_MAX_TOTAL_SECONDS = 180.0
_MAX_PROPOSALS = 2
_MAX_WARNINGS_NEAR_EDGE_SECONDS = 2.5
_EPS = .004


def _unqualified(status: str) -> dict:
    return {
        "version": VERSION,
        "status": status,
        "proposed_count": 0,
        "proposals": [],
        "requires_manual_or_independent_audio_verification": True,
        "source_asr_is_not_verified_speech": True,
        "edited_asr_is_not_word_alignment": True,
        "renders_performed": 0,
        "media_modified": False,
        "automatic_adoption_allowed": False,
        "best_variant": None,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }


def _edge_hypothesis(*, original, b2, side: str, utterance) -> dict | None:
    """Determine only an outward, nonoverlapping change to B2's active edge."""
    start, end = utterance.start, utterance.end
    if (side not in {"EXTEND_START", "EXTEND_END"}
            or not all(math.isfinite(n) for n in (
                start, end, original.source_start, original.source_end,
                b2.source_start, b2.source_end,
            )) or not 0 <= start < end <= 86400):
        raise ValueError("LOCAL_B3_SOURCE_CLOCK_INVALID")
    if side == "EXTEND_START":
        current = b2.source_start
        if not start + _EPS < current < end - _EPS:
            return None
        boundary = start
        added = current - boundary
        total = original.source_start - boundary
        new_start, new_end = boundary, b2.source_end
    else:
        current = b2.source_end
        if not start + _EPS < current < end - _EPS:
            return None
        boundary = end
        added = boundary - current
        total = boundary - original.source_end
        new_start, new_end = b2.source_start, boundary
    if (not _EPS < added <= _MAX_EXTRA_SECONDS
            or not 0.25 <= total <= _MAX_FROM_BASELINE_SECONDS
            or not 0 <= new_start < new_end <= 86400):
        return None
    return {
        "new_source_start_seconds": new_start,
        "new_source_end_seconds": new_end,
        "original_b2_edge_seconds": current,
        "new_edge_seconds": boundary,
        "extra_seconds_beyond_b2": added,
        "total_extension_from_a_seconds": total,
    }


def _nearby_b2_hints(edited_asr: dict, edge_output: float) -> list[dict]:
    hints = edited_asr.get("asr_review_hints", [])
    if (not isinstance(hints, list) or len(hints) > 24
            or edited_asr.get("source_asr_coverage_joined") is not True):
        raise ValueError("LOCAL_B3_EDITED_ASR_HINTS_INVALID")
    near = []
    for hint in hints:
        if hint.get("variant") != "b2":
            continue
        lo, hi = hint.get("output_start_seconds"), hint.get("output_end_seconds")
        if (type(lo) not in (int, float) or type(hi) not in (int, float)
                or not math.isfinite(lo) or not math.isfinite(hi)
                or not 0 <= lo < hi <= _MAX_TOTAL_SECONDS
                or hint.get("requires_audio_listening") is not True):
            raise ValueError("LOCAL_B3_EDITED_ASR_HINT_CLOCK_INVALID")
        if lo <= edge_output + _MAX_WARNINGS_NEAR_EDGE_SECONDS and (
                hi >= edge_output - _MAX_WARNINGS_NEAR_EDGE_SECONDS):
            near.append(hint)
    return near


def propose_guarded_b3_boundaries(
    *, prepared: dict, plan: dict, coverage: dict, edited_asr: dict,
) -> dict:
    """Use only already vetted local A/B/B2 plus optional completed audio ASR."""
    if edited_asr.get("status") == "LOCAL_EDITED_AUDIO_ASR_RECEIPT_NOT_AVAILABLE":
        return _unqualified("LOCAL_B3_EDITED_AUDIO_ASR_UNMEASURED")
    if (edited_asr.get("status")
            != "LOCAL_EXISTING_EDITED_ASR_DISCREPANCY_HINTS_UNRATED"
            or edited_asr.get("existing_audio_asr_receipt_checked") is not True
            or edited_asr.get("human_listening_verified") is not False
            or plan.get("status") != "LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED"
            or coverage.get("status")
            != "LOCAL_SOURCE_ASR_EDIT_CLOCK_COVERAGE_NOT_HEARD_SPEECH"
            or coverage.get("source_asr_sha256") != plan.get("source_asr_sha256")):
        raise ValueError("LOCAL_B3_INPUTS_UNVERIFIED")
    b2 = prepared["diagnostics"]["finished"]["b2"]
    trial = b2["prepared"]["trial"]
    original = trial["original"]["baseline_timeline"]
    active_side = trial["trial_action"]
    index = trial["source_cut_index"]
    cuts = {
        "a": sorted(original.segments, key=lambda s: s.output_order),
        "b": sorted(trial["trial"].segments, key=lambda s: s.output_order),
        "b2": sorted(b2["timeline"].segments, key=lambda s: s.output_order),
    }
    if (type(index) is not int or active_side not in {"EXTEND_START", "EXTEND_END"}
            or not cuts["a"] or len(cuts["a"]) > 32
            or any(len(cuts[role]) != len(cuts["a"])
                   or index not in range(len(cuts[role])) for role in _ROLES)):
        raise ValueError("LOCAL_B3_EDIT_GEOMETRY_INVALID")
    changed = cuts["b2"][index]
    if (len({c.source_asset_id for cuts_for_role in cuts.values()
             for c in cuts_for_role}) != 1
            or changed.source_asset_id != cuts["a"][index].source_asset_id):
        raise ValueError("LOCAL_B3_MULTI_SOURCE_NOT_SUPPORTED")
    asset = _canonical_assets(
        trial["original"]["database"], {changed.source_asset_id},
    )[changed.source_asset_id]
    parsed = _read_transcript(_no_links(b2["prepared"]["transcript_root"]),
                              asset.external_id)
    if parsed is None or parsed[1] != coverage["source_asr_sha256"]:
        raise ValueError("LOCAL_B3_SOURCE_ASR_CHANGED")
    source, digest, _basis = parsed
    # A report can cover a bounded subset of ASR intervals. Validate every
    # identifier against unrounded canonical local source-ASR before use.
    ids = {}
    for pos, utterance in enumerate(source):
        key = f"{digest}|{pos}|{utterance.start:.6f}|{utterance.end:.6f}"
        token = hashlib.sha256(key.encode()).hexdigest()[:20]
        if token in ids:
            raise ValueError("LOCAL_B3_ASR_IDENTIFIER_AMBIGUOUS")
        ids[token] = utterance
    rows = coverage["source_asr_intervals"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("LOCAL_B3_SOURCE_COVERAGE_INVALID")
    focus = plan["review_focus_per_variant"]["b2"]
    if focus.get("source_cut_index") != index:
        raise ValueError("LOCAL_B3_REVIEW_CUT_CHANGED")
    output_cursor = sum(s.source_end - s.source_start for s in cuts["b2"][:index])
    output_edge = (output_cursor if active_side == "EXTEND_START"
                   else output_cursor + changed.source_end - changed.source_start)
    if not 0 <= output_edge <= _MAX_TOTAL_SECONDS:
        raise ValueError("LOCAL_B3_EDIT_OUTPUT_CLOCK_INVALID")
    nearby = _nearby_b2_hints(edited_asr, output_edge)
    if not nearby:
        return _unqualified("LOCAL_B3_NO_NEARBY_EDITED_AUDIO_ASR_WARNING")
    candidates = []
    other = [seg for j, seg in enumerate(cuts["b2"]) if j != index]
    for row in rows:
        token = row.get("source_asr_interval_id")
        if (token not in ids or row.get("coverage_by_variant", {}).get("b2")
                != _PARTIAL):
            continue
        utterance = ids[token]
        if (round(utterance.start, 3) != row["source_start_seconds"]
                or round(utterance.end, 3) != row["source_end_seconds"]):
            raise ValueError("LOCAL_B3_ASR_CLOCK_MISMATCH")
        hypothesis = _edge_hypothesis(
            original=cuts["a"][index], b2=changed,
            side=active_side, utterance=utterance,
        )
        if hypothesis is None:
            continue
        # Any overlap with another source cut may produce repeated or
        # reordered speech. Do not suggest this as a safe single-edge fix.
        start = hypothesis["new_source_start_seconds"]
        end = hypothesis["new_source_end_seconds"]
        if any(c.source_asset_id == changed.source_asset_id
               and min(end, c.source_end) - max(start, c.source_start) > _EPS
               for c in other):
            continue
        baseline = [
            {"source_start_seconds": s.source_start,
             "source_end_seconds": s.source_end} for s in cuts["a"]
        ]
        proposed = [
            {"source_start_seconds": (
                start if j == index else s.source_start),
             "source_end_seconds": (end if j == index else s.source_end)}
            for j, s in enumerate(cuts["b2"])
        ]
        try:
            rebuilt = _trial_timeline(original, {
                "baseline_source_intervals": baseline,
                "trial_source_intervals": proposed,
                "source_cut_index": index,
                "trial_action": active_side,
            })
        except (ValueError, TypeError):
            continue
        if (rebuilt.estimated_duration > _MAX_TOTAL_SECONDS
                or changed.source_asset_id
                != rebuilt.segments[index].source_asset_id):
            continue
        candidates.append({
            "source_asr_interval_id": token,
            "source_cut_index": index,
            "trial_action": active_side,
            "proposed_source_start_seconds": round(start, 3),
            "proposed_source_end_seconds": round(end, 3),
            "extra_seconds_beyond_b2": round(
                hypothesis["extra_seconds_beyond_b2"], 3,
            ),
            "total_extension_from_a_seconds": round(
                hypothesis["total_extension_from_a_seconds"], 3,
            ),
            "proposed_timeline_sha256": hashlib.sha256(
                rebuilt.model_dump_json().encode("utf-8"),
            ).hexdigest(),
            "proposed_playback_seconds": round(rebuilt.estimated_duration, 3),
            "source_clock_partial_to_full_hypothesis": True,
            "nearby_independent_edited_asr_warning_count": len(nearby),
            "asr_warning_is_not_proof_of_this_specific_clipped_utterance": True,
            "requires_new_render_and_independent_audio_review": True,
        })
    candidates.sort(key=lambda r: (
        r["extra_seconds_beyond_b2"],
        r["proposed_source_start_seconds"],
        r["proposed_source_end_seconds"],
    ))
    candidates = candidates[:_MAX_PROPOSALS]
    result = _unqualified(
        "LOCAL_B3_UNRENDERED_SOURCE_CLOCK_REPAIR_HYPOTHESES"
        if candidates else "LOCAL_B3_NO_SUPPORTED_BOUNDARY_REPAIR",
    )
    result.update({
        "proposed_count": len(candidates),
        "proposals": candidates,
        "changed_cut_index": index,
        "active_extension_side": active_side,
        "source_asr_sha256": digest,
        "b2_independent_edited_asr_warning_count_near_edge": len(nearby),
        "source_clock_evidence_crosschecked_against_existing_audio_asr": True,
    })
    return result
