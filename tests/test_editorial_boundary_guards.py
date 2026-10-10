"""Measured editorial plan safeguards for deterministic edit timelines.

LLM payloads and persisted plans are untrusted; no cloud/media calls.
"""
from __future__ import annotations

import copy

import pytest

from holoclip import editorial_planning
from holoclip.domain_adapters import candidate_to_clip_timeline
from holoclip.editorial_profile import editorial_profile_prompt_payload
from holoclip.editorial_planning import plan_candidate_boundaries, validate_edit_plan
from holoclip.models import ClipCandidate, TranscriptSegment


def _rows():
    return [
        {"id": "s0", "start": 10.0, "end": 20.0},
        {"id": "s1", "start": 30.0, "end": 50.0},
        {"id": "s2", "start": 70.0, "end": 80.0},
    ]


def _payload(profile):
    return {
        "pattern_ids": [editorial_profile_prompt_payload(profile)["patterns"][0]["id"]],
        "spans": [
            {"start_segment_id": "s0", "end_segment_id": "s0", "purpose": "setup"},
            {"start_segment_id": "s1", "end_segment_id": "s1", "purpose": "payoff"},
        ],
        "reason": "observed setup and payoff",
    }


def _candidate(*, plan=None, start=10, end=50):
    return ClipCandidate(
        start=start, end=end, working_title="test", setup="setup",
        development="development", payoff="payoff",
        why_interesting="connected event", editorial_edit_plan=plan,
    )


def test_valid_measured_plan_preserves_cut_order_and_provenance(learned_ready_profile):
    profile = learned_ready_profile
    plan = validate_edit_plan(
        _payload(profile), rows=_rows(), profile=profile, candidate_range=(10, 50),
    )
    assert plan is not None
    assert plan.selected_seconds == pytest.approx(30)
    assert plan.omitted_seconds == pytest.approx(10)
    assert [(s.start_segment_id, s.end_segment_id) for s in plan.spans] == [
        ("s0", "s0"), ("s1", "s1"),
    ]
    timeline = candidate_to_clip_timeline("source-asset", _candidate(plan=plan))
    assert [(s.source_start, s.source_end, s.purpose) for s in timeline.segments] == [
        (10, 20, "setup"), (30, 50, "payoff"),
    ]
    assert timeline.estimated_duration == pytest.approx(30)


@pytest.mark.parametrize("change", [
    lambda p: p.update(spans="not-a-list"),
    lambda p: p.update(pattern_ids="not-a-list"),
    lambda p: p.update(spans=[]),
    lambda p: p.update(spans=[["s0", "s1"]]),
    lambda p: p.update(spans=[{"start_segment_id": "fake", "end_segment_id": "s1"}]),
    lambda p: p.update(spans=[{"start_segment_id": "s2", "end_segment_id": "s0"}]),
    lambda p: p.update(spans=[{"start_segment_id": [], "end_segment_id": "s0"}]),
    lambda p: p.update(spans=[
        {"start_segment_id": "s1", "end_segment_id": "s1"},
        {"start_segment_id": "s0", "end_segment_id": "s0"},
    ]),
    lambda p: p.update(spans=[
        {"start_segment_id": "s0", "end_segment_id": "s1"},
        {"start_segment_id": "s1", "end_segment_id": "s2"},
    ]),
    lambda p: p.update(spans=[
        {"start_segment_id": "s0", "end_segment_id": "s1", "purpose": ["invalid"]}
    ]),
    lambda p: p.update(pattern_ids=[{"unhashable": "invalid"}]),
    lambda p: p.update(reason={"wrong": "type"}),
    lambda p: p.update(reason="x" * 2001),
    lambda p: p.update(spans=[
        {"start_segment_id": "s0", "end_segment_id": "s0"} for _ in range(33)
    ]),
])
def test_bad_llm_json_cannot_form_timeline(learned_ready_profile, change):
    payload = copy.deepcopy(_payload(learned_ready_profile))
    change(payload)
    assert validate_edit_plan(
        payload, rows=_rows(), profile=learned_ready_profile,
        candidate_range=(10, 50),
    ) is None


@pytest.mark.parametrize("rows", [
    [{"id": "s0", "start": 10, "end": 20}, {"id": "s0", "start": 30, "end": 50}],
    [{"id": "s1", "start": 30, "end": 50}, {"id": "s0", "start": 10, "end": 20}],
    [{"id": "s0", "start": 10, "end": 10}, {"id": "s1", "start": 30, "end": 50}],
    [{"id": "s0", "start": 10, "end": float("nan")}, {"id": "s1", "start": 30, "end": 50}],
    [{"id": "s0", "start": True, "end": 20}, {"id": "s1", "start": 30, "end": 50}],
    [{"id": "s0", "start": 10, "end": 20}, {"id": "s1", "start": float("inf"), "end": 50}],
])
def test_bad_measured_source_rows_never_resolve_as_plan(learned_ready_profile, rows):
    assert validate_edit_plan(
        _payload(learned_ready_profile), rows=rows,
        profile=learned_ready_profile,
    ) is None


def test_context_only_plan_rejected_even_with_valid_source_ids(learned_ready_profile):
    payload = _payload(learned_ready_profile)
    payload["spans"] = [{"start_segment_id": "s2", "end_segment_id": "s2"}]
    # A ±60s context segment is not necessarily part of the proposed event.
    assert validate_edit_plan(
        payload, rows=_rows(), profile=learned_ready_profile,
        candidate_range=(10, 50),
    ) is None


@pytest.mark.asyncio
async def test_invalid_plan_keeps_original_candidate_and_does_not_fabricate_boundaries(
    learned_ready_profile, monkeypatch,
):
    profile = learned_ready_profile
    candidate = _candidate()
    segments = [
        TranscriptSegment(start=10, end=20, text="setup"),
        TranscriptSegment(start=30, end=50, text="payoff"),
        TranscriptSegment(start=70, end=80, text="unrelated"),
    ]
    async def bad_planner(*args, **kwargs):
        return {
            "pattern_ids": _payload(profile)["pattern_ids"],
            "spans": [
                {"start_segment_id": "source:2", "end_segment_id": "source:2"},
            ],
        }
    monkeypatch.setattr(editorial_planning, "_cached_call", bad_planner)
    outcomes = await plan_candidate_boundaries(
        object(), [candidate], segments, profile=profile, model="fake-provider",
    )
    assert outcomes == [candidate]
    assert outcomes[0].editorial_edit_plan is None
    assert (outcomes[0].start, outcomes[0].end) == (10, 50)


def test_persisted_edit_plan_duration_tampering_rejected(learned_ready_profile):
    plan = validate_edit_plan(
        _payload(learned_ready_profile), rows=_rows(),
        profile=learned_ready_profile,
    )
    assert plan is not None
    damaged = plan.model_copy(update={"selected_seconds": 999})
    with pytest.raises(ValueError, match="measured candidate timeline"):
        candidate_to_clip_timeline("asset", _candidate(plan=damaged))


def test_persisted_edit_plan_omitted_duration_tampering_rejected(learned_ready_profile):
    plan = validate_edit_plan(
        _payload(learned_ready_profile), rows=_rows(), profile=learned_ready_profile,
    )
    assert plan is not None
    damaged = plan.model_copy(update={"omitted_seconds": 0})
    with pytest.raises(ValueError, match="measured candidate timeline"):
        candidate_to_clip_timeline("asset", _candidate(plan=damaged))


def test_persisted_edit_plan_mismatched_candidate_envelope_rejected(learned_ready_profile):
    plan = validate_edit_plan(
        _payload(learned_ready_profile), rows=_rows(),
        profile=learned_ready_profile,
    )
    assert plan is not None
    with pytest.raises(ValueError, match="measured candidate timeline"):
        candidate_to_clip_timeline("asset", _candidate(plan=plan, start=0, end=50))


def test_persisted_edit_plan_reordered_or_overlapping_spans_rejected(learned_ready_profile):
    plan = validate_edit_plan(
        _payload(learned_ready_profile), rows=_rows(),
        profile=learned_ready_profile,
    )
    assert plan is not None
    damaged = plan.model_copy(update={"spans": list(reversed(plan.spans))})
    with pytest.raises(ValueError, match="chronological measured IDs"):
        candidate_to_clip_timeline("asset", _candidate(plan=damaged))


def test_no_edit_plan_preserves_legacy_continuous_candidate():
    timeline = candidate_to_clip_timeline("asset", _candidate())
    assert len(timeline.segments) == 1
    assert timeline.estimated_duration == pytest.approx(40)
