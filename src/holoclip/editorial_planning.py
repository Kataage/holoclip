"""Measured boundary plans from a ready editorial model, ready for ClipTimeline."""

from __future__ import annotations

from holoclip.editorial_profile import (
    EditorialProfile,
    _cached_call,
    _messages,
    editorial_profile_prompt_payload,
)
from holoclip.models import EditorialEditPlan, EditorialEditSpan, TranscriptSegment


def validate_edit_plan(
    payload: dict, *, rows: list[dict], profile: EditorialProfile
) -> EditorialEditPlan | None:
    by_id = {r["id"]: r for r in rows}
    valid_patterns = {
        p["id"] for p in (editorial_profile_prompt_payload(profile) or {}).get("patterns", [])
    }
    pattern_ids = sorted(
        {p for p in payload.get("pattern_ids", []) if isinstance(p, str) and p in valid_patterns}
    )
    if not pattern_ids:
        return None
    spans = []
    for raw in payload.get("spans", []):
        if not isinstance(raw, dict):
            return None
        start = by_id.get(raw.get("start_segment_id"))
        end = by_id.get(raw.get("end_segment_id"))
        if not start or not end or end["end"] <= start["start"]:
            return None
        if spans and start["start"] < spans[-1].source_end:
            return None
        spans.append(
            EditorialEditSpan(
                source_start=start["start"],
                source_end=end["end"],
                start_segment_id=start["id"],
                end_segment_id=end["id"],
                purpose=str(raw.get("purpose") or "other"),
            )
        )
    if not spans:
        return None
    duration = sum(s.source_end - s.source_start for s in spans)
    return EditorialEditPlan(
        profile_fingerprint=profile.fingerprint,
        pattern_ids=pattern_ids,
        spans=spans,
        reason=str(payload.get("reason") or ""),
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
            prompt_version="measured-editorial-boundaries-v1",
        )
        plan = validate_edit_plan(payload, rows=rows, profile=profile)
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
