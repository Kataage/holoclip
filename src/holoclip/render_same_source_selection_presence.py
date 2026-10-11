"""Source-clock selection coverage for ASR citations, not narrative truth.

Cited speech comes from local ASR, and even fully selected source clock time
cannot establish that speech is audible, semantically related, or interesting.
"""
from __future__ import annotations

import math

from holoclip.render_same_source_narrative_reference import _shared, _union

_MAX_CUTS = 200


def source_span_coverage(
    start: float, end: float, generated: list[tuple[float, float]],
) -> dict:
    """Union-corrected overlap, with replay not double-counted."""
    if (
        type(start) not in (int, float)
        or type(end) not in (int, float)
        or not math.isfinite(start) or not math.isfinite(end)
        or not 0 <= start < end <= 86400
        or not isinstance(generated, list)
        or not 1 <= len(generated) <= _MAX_CUTS
    ):
        raise ValueError("LOCAL_SEMANTIC_SOURCE_SELECTION_INVALID")
    intervals = _union(generated)
    selected = _shared([(start, end)], intervals)
    length = end - start
    if selected <= 1e-6:
        status = "SOURCE_CLOCK_NOT_SELECTED"
    elif length - selected <= 1e-6:
        status = "SOURCE_CLOCK_FULLY_SELECTED"
    else:
        status = "SOURCE_CLOCK_PARTIALLY_SELECTED"
    return {
        "source_clock_selection_status": status,
        "source_clock_selected_seconds": round(selected, 3),
        "source_clock_span_seconds": round(length, 3),
        "source_clock_coverage_not_audible_speech_verification": True,
    }


def parse_generated_intervals(entry: object) -> list[tuple[float, float]]:
    if not isinstance(entry, list) or not 1 <= len(entry) <= _MAX_CUTS:
        raise ValueError("LOCAL_SEMANTIC_GENERATED_SOURCE_INTERVALS_INVALID")
    output = []
    for item in entry:
        if not isinstance(item, dict) or set(item) != {
            "source_start_seconds", "source_end_seconds"
        }:
            raise ValueError("LOCAL_SEMANTIC_SOURCE_INTERVAL_SHAPE_INVALID")
        start, end = item["source_start_seconds"], item["source_end_seconds"]
        if (
            type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end)
            or not 0 <= start < end <= 86400
        ):
            raise ValueError("LOCAL_SEMANTIC_SOURCE_INTERVAL_RANGE_INVALID")
        output.append((start, end))
    return output
