"""In-sample human narrative reference and source-interval position evidence.

Clip-level textual annotations are NOT source-time labels. Opening/closing/
intermediate refer only to human aligned segment order, not story roles.
Neither observed coverage nor annotations establish the creative best edit.
"""
from __future__ import annotations

import hashlib
import math
import unicodedata

from holoclip.examples import HumanClipExample, human_clip_identity

_FIELDS = ("setup", "development", "payoff", "why_selected")
_MAX_SEGMENTS = 200
_MAX_TEXT_CHARS = 280


def _text(value: str) -> str | None:
    text = " ".join(value.split())
    if (not text or len(text) > _MAX_TEXT_CHARS
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                   for char in text)):
        return None
    return text


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted(spans)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if (not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= 86400):
            raise ValueError("LOCAL_NARRATIVE_SOURCE_INTERVAL_INVALID")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in intervals)


def _shared(
    left: list[tuple[float, float]], right: list[tuple[float, float]],
) -> float:
    i = j = 0
    seconds = 0.0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        seconds += max(0.0, min(b, d) - max(a, c))
        if b <= d:
            i += 1
        else:
            j += 1
    return seconds


def summarize_human_reference(
    human: HumanClipExample,
    generated: list[tuple[float, float]],
) -> dict:
    """Coarse aligned-span coverage and original clip-level descriptions."""
    ordered = sorted(
        human.alignment_segments,
        key=lambda a: (a.clip_start, a.clip_end),
    )
    if not 1 <= len(ordered) <= _MAX_SEGMENTS:
        raise ValueError("LOCAL_NARRATIVE_ALIGNMENT_COUNT_INVALID")
    generated_union = _union(generated)
    if not generated_union:
        raise ValueError("LOCAL_NARRATIVE_GENERATED_EMPTY")
    annotations = {name: _text(getattr(human, name)) for name in _FIELDS}
    if any(value is None for value in annotations.values()):
        return {
            "status": "NARRATIVE_ANNOTATIONS_UNSAFE_OR_UNAVAILABLE",
            "clip_level_story_labels_time_anchored": False,
            "creative_quality_label": None,
        }

    first = [ordered[0]]
    middle = ordered[1:-1]
    last = [ordered[-1]] if len(ordered) > 1 else []
    groups = (("first_aligned", first),
              ("intermediate_aligned", middle),
              ("last_aligned", last))
    coverage: dict[str, dict] = {}
    for name, examples in groups:
        intervals = _union([
            (segment.source_start, segment.source_end)
            for segment in examples
        ])
        total = _duration(intervals)
        shared = _shared(intervals, generated_union)
        coverage[name] = {
            "aligned_segment_count": len(examples),
            "human_aligned_source_seconds": round(total, 3),
            "generated_shared_source_seconds": round(shared, 3),
            "generated_coverage_of_human_group_fraction": (
                round(shared / total, 6) if total > 0 else None
            ),
        }
    return {
        "status": "IN_SAMPLE_CLIP_LEVEL_NARRATIVE_REFERENCE",
        "human_clip_identity_sha256": hashlib.sha256(
            human_clip_identity(human.clip_url).encode("utf-8")
        ).hexdigest(),
        "human_source_alignment_confidence": human.alignment_confidence,
        "human_clip_level_annotations_unverified": annotations,
        "human_aligned_segment_count": len(ordered),
        "human_segment_order_coverage": coverage,
        "group_names_are_positions_not_story_roles": True,
        "clip_level_story_labels_time_anchored": False,
        "text_matches_source_asr_verified": False,
        "setup_development_payoff_in_generated_clip_verified": False,
        "human_training_reference_not_independent_holdout": True,
        "creative_quality_label": None,
        "auto_edit_allowed": False,
        "ready": False,
    }


def attach_narrative_reference(
    inspection: dict, *, examples_by_stream: dict[str, list[HumanClipExample]],
    stream_by_job: dict[str, str],
    ordered_cuts_by_job: dict[str, list[tuple[float, float]]],
) -> None:
    """Attach only to already-screened, ranked inspection items."""
    lookup: dict[tuple[str, str], HumanClipExample] = {}
    for stream, examples in examples_by_stream.items():
        for example in examples:
            digest = hashlib.sha256(
                human_clip_identity(example.clip_url).encode("utf-8")
            ).hexdigest()
            key = stream, digest
            if key in lookup:
                raise ValueError("LOCAL_NARRATIVE_DUPLICATE_REFERENCE")
            lookup[key] = example

    for entry in inspection["results"]:
        stream = stream_by_job.get(entry["job_id"])
        cuts = ordered_cuts_by_job.get(entry["job_id"])
        reference = lookup.get((stream, entry["human_clip_identity_sha256"]))
        if stream is None or cuts is None or reference is None:
            entry["narrative_reference"] = {
                "status": "NARRATIVE_SOURCE_REFERENCE_NOT_VERIFIED",
                "clip_level_story_labels_time_anchored": False,
            }
            continue
        try:
            entry["narrative_reference"] = summarize_human_reference(
                reference, cuts,
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            entry["narrative_reference"] = {
                "status": "NARRATIVE_ALIGNMENT_UNTRUSTED",
                "clip_level_story_labels_time_anchored": False,
            }
