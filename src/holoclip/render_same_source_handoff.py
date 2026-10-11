"""Source-clock handoff observations from existing, unverified local ASR.

This models only source-time continuity and boundaries. Neither an ASR
utterance crossing a boundary nor a source-time jump establishes a bad cut,
a missing setup, a joke payoff, or an editing recommendation.
"""
from __future__ import annotations

import math

from holoclip.models import TranscriptSegment

_MAX_CUTS = 200
_MAX_HANDOFFS = 3
_EPS = 0.05


def _speech_boundary(segments: list[TranscriptSegment], moment: float) -> dict:
    crossing = False
    nearest_before: float | None = None
    nearest_after: float | None = None
    for segment in segments:
        if segment.end <= moment:
            gap = moment - segment.end
            if gap <= 8 and (nearest_before is None or gap < nearest_before):
                nearest_before = gap
            continue
        if segment.start >= moment:
            gap = segment.start - moment
            if gap <= 8 and (nearest_after is None or gap < nearest_after):
                nearest_after = gap
            break
        if segment.start + _EPS < moment < segment.end - _EPS:
            crossing = True
        # Nearest preceding/following ASR does not imply actual speech timing.
    return {
        "source_second": round(moment, 3),
        "asr_segment_spans_boundary": crossing,
        "nearest_prior_asr_end_distance_seconds": (
            round(nearest_before, 3) if nearest_before is not None else None
        ),
        "nearest_next_asr_start_distance_seconds": (
            round(nearest_after, 3) if nearest_after is not None else None
        ),
        "asr_word_boundary_precision_available": False,
    }


def _relation(delta: float) -> str:
    if delta > _EPS:
        return "FORWARD_SOURCE_SKIP"
    if delta < -_EPS:
        return "SOURCE_TIME_OVERLAP_OR_REPLAY"
    return "CONTIGUOUS_SOURCE_CLOCK"


def summarize_playback_handoffs(
    cuts: list[tuple[float, float]],
    segments: list[TranscriptSegment],
) -> dict:
    """Inspect playback order, not sorted set union; bounded evidence only."""
    if (not 1 <= len(cuts) <= _MAX_CUTS
            or any(
                type(start) not in (int, float)
                or type(end) not in (int, float)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end <= 86400
                for start, end in cuts
            )):
        raise ValueError("LOCAL_INSPECTION_HANDOFF_CUTS_INVALID")
    joins = []
    for pos, (previous, next_) in enumerate(
        zip(cuts, cuts[1:]), start=1
    ):
        before = _speech_boundary(segments, previous[1])
        after = _speech_boundary(segments, next_[0])
        delta = next_[0] - previous[1]
        relation = _relation(delta)
        discontinuous = relation != "CONTIGUOUS_SOURCE_CLOCK"
        possible_speech_interruption = (
            discontinuous
            and (
                before["asr_segment_spans_boundary"]
                or after["asr_segment_spans_boundary"]
            )
        )
        joins.append({
            "join_output_index": pos,
            "source_clock_delta_seconds": round(delta, 3),
            "source_clock_relation": relation,
            "previous_segment_end": before,
            "next_segment_start": after,
            "asr_boundary_crossing_on_discontinuous_join": (
                possible_speech_interruption
            ),
            "actual_audible_speech_interruption_verified": False,
        })
    chosen = sorted(
        joins,
        key=lambda row: (
            -int(row["asr_boundary_crossing_on_discontinuous_join"]),
            -int(row["source_clock_relation"] != "CONTIGUOUS_SOURCE_CLOCK"),
            -abs(row["source_clock_delta_seconds"]),
            row["join_output_index"],
        ),
    )[:_MAX_HANDOFFS]
    chosen.sort(key=lambda row: row["join_output_index"])
    return {
        "generated_cut_count": len(cuts),
        "generated_join_count": len(joins),
        "forward_source_skip_joins": sum(
            item["source_clock_relation"] == "FORWARD_SOURCE_SKIP"
            for item in joins
        ),
        "source_overlap_or_replay_joins": sum(
            item["source_clock_relation"] == "SOURCE_TIME_OVERLAP_OR_REPLAY"
            for item in joins
        ),
        "asr_boundary_crossing_on_discontinuous_join_count": sum(
            item["asr_boundary_crossing_on_discontinuous_join"]
            for item in joins
        ),
        "displayed_join_count": len(chosen),
        "omitted_join_count": len(joins) - len(chosen),
        "opening_source_boundary": _speech_boundary(segments, cuts[0][0]),
        "ending_source_boundary": _speech_boundary(segments, cuts[-1][1]),
        "notable_joins_in_output_order": chosen,
        "local_asr_is_unverified_timing_evidence": True,
        "narrative_setup_development_payoff_reaction_inferred": False,
        "editorial_quality_label": None,
        "auto_edit_allowed": False,
        "ready": False,
    }
