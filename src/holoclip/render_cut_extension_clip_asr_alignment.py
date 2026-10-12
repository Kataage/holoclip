"""Time-local, ASR-to-ASR *review hints* for already verified edited clips.

No acoustic fingerprint, word-level forced alignment, transcript truth, or
editorial scoring. All text stays in memory; only times and evidence IDs leave.
"""
from __future__ import annotations

import difflib
import math

from holoclip.models import TranscriptSegment

VERSION = "holoclip-three-way-edited-source-asr-temporal-hints-v1"
_MAX_SOURCE = 8
_MAX_HEARD = 25
_MAX_SECONDS = 8.0
_MAX_SKEW = 1.25
_NEAR_SECONDS = 0.65
_MIN_TEXT_LEN = 4
_MIN_SIMILARITY = 0.78


def _similarity(reference: str, observed: str) -> float:
    if not reference or not observed:
        return 0.0
    # A transcriber can coalesce two source utterances into a single segment.
    # Exact contained phrases are stronger evidence than whole-utterance ratio.
    if reference in observed:
        return 1.0
    if observed in reference and len(observed) >= 6:
        return round(len(observed) / len(reference), 4)
    return difflib.SequenceMatcher(
        None, reference, observed, autojunk=False,
    ).ratio()


def _observed_windows(
    heard: list[TranscriptSegment], *, output_start_seconds: float,
    normalize,
) -> tuple[list[dict], list[dict]]:
    if not isinstance(heard, list) or len(heard) > _MAX_HEARD:
        raise ValueError("LOCAL_ASR_ALIGNMENT_SEGMENT_LIMIT")
    points = []
    last_start = -1.0
    for i, row in enumerate(heard):
        if not isinstance(row, TranscriptSegment):
            raise ValueError("LOCAL_ASR_ALIGNMENT_SEGMENT_INVALID")
        begin = row.start + output_start_seconds
        end = row.end + output_start_seconds
        if (not all(math.isfinite(x) for x in (begin, end))
                or not output_start_seconds <= begin < end
                or begin < last_start):
            raise ValueError("LOCAL_ASR_ALIGNMENT_CLOCK_INVALID")
        last_start = begin
        points.append({
            "observed_segment_index": i + 1,
            "output_start_seconds": round(begin, 3),
            "output_end_seconds": round(end, 3),
            "normalized_text": normalize(row.text),
        })
    windows = []
    for i, item in enumerate(points):
        windows.append({
            "indices": [i + 1],
            "start": item["output_start_seconds"],
            "end": item["output_end_seconds"],
            "text": item["normalized_text"],
        })
        if i + 1 < len(points):
            nxt = points[i + 1]
            if nxt["output_start_seconds"] <= item["output_end_seconds"] + 0.25:
                windows.append({
                    "indices": [i + 1, i + 2],
                    "start": item["output_start_seconds"],
                    "end": nxt["output_end_seconds"],
                    "text": item["normalized_text"] + nxt["normalized_text"],
                })
    return points, windows


def temporal_asr_review(
    *, expected: list[dict], heard: list[TranscriptSegment],
    output_focus_start_seconds: float, output_focus_end_seconds: float,
    normalize,
) -> dict:
    """Locate ASR transcript agreement/disagreement at actual *output* clocks.

    Candidate windows may span two adjacent observed ASR segments to avoid
    misclassifying transcriber segmentation as omitted speech. A source
    utterance is not forced into a unique match; ambiguity is exposed.
    """
    lo, hi = output_focus_start_seconds, output_focus_end_seconds
    if (not isinstance(lo, (float, int)) or not isinstance(hi, (float, int))
            or not all(math.isfinite(x) for x in (lo, hi))
            or not 0 <= lo < hi <= 180
            or not .3 <= hi - lo <= _MAX_SECONDS + .01
            or not isinstance(expected, list) or len(expected) > _MAX_SOURCE):
        raise ValueError("LOCAL_ASR_ALIGNMENT_FOCUS_INVALID")
    points, windows = _observed_windows(
        heard, output_start_seconds=lo, normalize=normalize,
    )
    if any(p["output_end_seconds"] > hi + .75 for p in points):
        raise ValueError("LOCAL_ASR_ALIGNMENT_OBSERVED_OUT_OF_BOUNDS")

    results = []
    matched_indices = set()
    for item in sorted(expected, key=lambda x: (
        x["output_start_seconds"], x["output_end_seconds"], x["id"],
    )):
        identifier = item.get("id")
        begin = item.get("output_start_seconds")
        end = item.get("output_end_seconds")
        source_text = item.get("text")
        if (not isinstance(identifier, str) or len(identifier) > 24
                or not identifier or not isinstance(source_text, str)
                or not 0 < len(source_text) <= 700
                or not isinstance(begin, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(begin) or not math.isfinite(end)
                or not lo - .01 <= begin < end <= hi + .01):
            raise ValueError("LOCAL_ASR_ALIGNMENT_REFERENCE_INVALID")
        target_center = (begin + end) / 2
        potential = []
        for window in windows:
            score = _similarity(source_text, window["text"])
            if score < _MIN_SIMILARITY:
                continue
            center = (window["start"] + window["end"]) / 2
            difference = abs(center - target_center)
            potential.append((difference, -score, len(window["indices"]), window))
        potential.sort(key=lambda p: (
            p[0], p[1], p[2], p[3]["start"],
        ))
        nearby = [
            p for p in points
            if p["output_start_seconds"] <= end + _NEAR_SECONDS
            and p["output_end_seconds"] >= begin - _NEAR_SECONDS
        ]
        chosen = potential[0] if potential else None
        if len(source_text) < _MIN_TEXT_LEN:
            status = "SHORT_TEXT_AMBIGUOUS_REVIEW"
        elif chosen is None:
            status = ("LOW_ASR_TEXT_OVERLAP_AT_EXPECTED_TIME"
                      if nearby else "NO_EDITED_ASR_NEAR_EXPECTED_TIME")
        elif chosen[0] > _MAX_SKEW:
            status = "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK"
        elif source_text not in chosen[3]["text"]:
            # A near-match containing only a prefix/suffix is a possible
            # clipped utterance, not positive evidence of complete speech.
            status = "PARTIAL_ASR_TEXT_OVERLAP_REVIEW"
        else:
            # Multiple plausible positions can be source repetitions,
            # recognizer duplication, or unrelated similar conversation.
            separated = [
                v for v in potential
                if set(v[3]["indices"]).isdisjoint(chosen[3]["indices"])
                and abs(v[3]["start"] - chosen[3]["start"]) > .5
            ]
            status = ("MULTIPLE_PLAUSIBLE_TRANSCRIPT_OCCURRENCES"
                      if separated else "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED")
        if chosen is not None:
            win = chosen[3]
            matched_indices.update(win["indices"])
        results.append({
            "source_evidence_id": identifier,
            "expected_output_start_seconds": round(begin, 3),
            "expected_output_end_seconds": round(end, 3),
            "observed_segment_indices": chosen[3]["indices"] if chosen else [],
            "observed_output_start_seconds":
                chosen[3]["start"] if chosen else None,
            "observed_output_end_seconds":
                chosen[3]["end"] if chosen else None,
            "observed_text_similarity_proxy":
                round(-chosen[1], 3) if chosen else 0.0,
            "center_clock_skew_seconds":
                round(chosen[0], 3) if chosen else None,
            "plausible_transcript_window_count": len(potential),
            "status": status,
            "word_alignment_verified": False,
            "missing_speech_proven": False,
            "repetition_proven": False,
        })
    unmatched = [
        {
            "observed_segment_index": p["observed_segment_index"],
            "output_start_seconds": p["output_start_seconds"],
            "output_end_seconds": p["output_end_seconds"],
            "may_be_unsampled_source_context_or_asr_error": True,
        }
        for p in points if p["observed_segment_index"] not in matched_indices
    ]
    warnings = {
        "SHORT_TEXT_AMBIGUOUS_REVIEW",
        "PARTIAL_ASR_TEXT_OVERLAP_REVIEW",
        "LOW_ASR_TEXT_OVERLAP_AT_EXPECTED_TIME",
        "NO_EDITED_ASR_NEAR_EXPECTED_TIME",
        "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK",
        "MULTIPLE_PLAUSIBLE_TRANSCRIPT_OCCURRENCES",
    }
    return {
        "version": VERSION,
        "status": "LOCAL_ASR_TIME_ANCHORED_REVIEW_ONLY",
        "expected_utterance_count": len(results),
        "observed_segment_count": len(points),
        "time_anchor_warning_count": sum(
            row["status"] in warnings for row in results
        ),
        "observed_segment_without_matched_sampled_reference_count":
            len(unmatched),
        "source_utterance_alignment": results,
        "observed_outside_sampled_references": unmatched,
        "asr_segment_clock_is_not_word_clock": True,
        "ambiguous_text_is_not_missing_speech": True,
        "duplicate_text_is_not_proven_repetition": True,
        "source_asr_not_ground_truth": True,
        "edited_asr_not_ground_truth": True,
        "human_audio_review_completed": False,
        "editorial_quality_improvement": None,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
    }
