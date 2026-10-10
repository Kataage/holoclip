"""Measured boundary plans from a ready editorial model, ready for ClipTimeline."""

from __future__ import annotations

import math

from holoclip.editorial_profile import (
    EditorialProfile,
    _cached_call,
    _messages,
    editorial_profile_prompt_payload,
)
from holoclip.models import EditorialEditPlan, EditorialEditSpan, TranscriptSegment


def validate_edit_plan(
    payload: dict, *, rows: list[dict], profile: EditorialProfile,
    candidate_range: tuple[float, float] | None = None,
) -> EditorialEditPlan | None:
    """Resolve only real, ordered transcript IDs; reject unsafe LLM plans.

    The caller-supplied source rows are authoritative. Invalid or extraneous
    model output preserves the unedited candidate instead of guessing times.
    """
    if not isinstance(payload, dict):
        return None
    raw_patterns = payload.get("pattern_ids")
    raw_spans = payload.get("spans")
    if (not isinstance(raw_patterns, list) or len(raw_patterns) > 50
            or not isinstance(raw_spans, list) or not 1 <= len(raw_spans) <= 32
            or not isinstance(rows, list) or not rows):
        return None
    # Duplicate IDs or out-of-order source rows make source index resolution
    # ambiguous. The planner cannot invent which occurrence an ID refers to.
    by_id = {}
    previous_start = -1.0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return None
        rid = row.get("id")
        start = row.get("start")
        end = row.get("end")
        if (not isinstance(rid, str) or not rid or rid in by_id
                or type(start) not in (int, float)
                or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start or start < previous_start):
            return None
        by_id[rid] = (index, row)
        previous_start = start
    prior = editorial_profile_prompt_payload(profile)
    if prior is None:
        return None
    valid_patterns = {p["id"] for p in prior.get("patterns", [])}
    pattern_ids = sorted({
        value for value in raw_patterns
        if isinstance(value, str) and value in valid_patterns
    })
    if not pattern_ids:
        return None
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 2000:
        return None
    spans = []
    last_index = -1
    for item in raw_spans:
        if not isinstance(item, dict):
            return None
        sid = item.get("start_segment_id")
        eid = item.get("end_segment_id")
        if (not isinstance(sid, str) or not isinstance(eid, str)
                or sid not in by_id or eid not in by_id):
            return None
        start_index, start_row = by_id[sid]
        end_index, end_row = by_id[eid]
        if start_index > end_index or start_index <= last_index:
            return None
        start_time = start_row["start"]
        end_time = end_row["end"]
        if end_time <= start_time or (
            spans and start_time < spans[-1].source_end
        ):
            return None
        purpose = item.get("purpose", "other")
        if not isinstance(purpose, str) or len(purpose) > 64:
            return None
        try:
            spans.append(
                EditorialEditSpan(
                    source_start=start_time,
                    source_end=end_time,
                    start_segment_id=sid,
                    end_segment_id=eid,
                    purpose=purpose,
                )
            )
        except ValueError:
            return None
        last_index = end_index
    if candidate_range is not None:
        try:
            c_start, c_end = candidate_range
            if (not math.isfinite(c_start) or not math.isfinite(c_end)
                    or c_start < 0 or c_end <= c_start):
                return None
        except (TypeError, ValueError):
            return None
        # A plan containing *only* unrelated context is not an edit of
        # the proposed event, even if its IDs exist in the ±60s prompt.
        if not any(
            span.source_start < c_end and span.source_end > c_start
            for span in spans
        ):
            return None
    duration = sum(span.source_end - span.source_start for span in spans)
    return EditorialEditPlan(
        profile_fingerprint=profile.fingerprint,
        pattern_ids=pattern_ids,
        spans=spans,
        reason=reason,
        selected_seconds=duration,
        omitted_seconds=max(0.0, spans[-1].source_end - spans[0].source_start - duration),
    )

async def plan_candidate_boundaries(
    llm,
    candidates,
    segments: list[TranscriptSegment],
    *,
    profile,
    model,
    reasoning_effort="high",
    stage_cache=None,
):
    prior = editorial_profile_prompt_payload(profile) if profile else None
    if prior is None:
        return candidates
    planned = []
    for candidate in candidates:
        rows = [
            {"id": f"source:{i}", **s.model_dump(mode="json")}
            for i, s in enumerate(segments)
            if s.end > max(0, candidate.start - 60) and s.start < candidate.end + 60
        ]
        if not rows:
            planned.append(candidate)
            continue
        messages = _messages(
            "Plan measured source cuts using the learned model's required setup, payoff and reaction, "
            "avoiding contextual redundancy. Spans must be chronological and non-overlapping. "
            "Return {pattern_ids:[matching core IDs], spans:[{start_segment_id, end_segment_id, purpose}], "
            "reason:string}. Use supplied transcript IDs; never emit guessed seconds. If no pattern fits, "
            "return empty arrays and preserve the original candidate.",
            {
                "editorial_profile": prior,
                "candidate": candidate.model_dump(mode="json"),
                "transcript": rows,
            },
        )
        # Very long stories keep their original boundaries until a paged planner can cover them.
        if sum(len(m["content"]) for m in messages) > 65000:
            planned.append(candidate)
            continue
        payload = await _cached_call(
            llm,
            messages,
            stage="editorial_boundary_plan",
            model=model,
            reasoning_effort=reasoning_effort,
            stage_cache=stage_cache,
            cache_provider="nvidia_nim",
            prompt_version="measured-editorial-boundaries-v2",
        )
        plan = validate_edit_plan(
            payload, rows=rows, profile=profile,
            candidate_range=(candidate.start, candidate.end),
        )
        if plan:
            candidate = candidate.model_copy(
                update={
                    "editorial_edit_plan": plan,
                    "start": plan.spans[0].source_start,
                    "end": plan.spans[-1].source_end,
                }
            )
        planned.append(candidate)
    return planned
