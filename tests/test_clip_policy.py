from __future__ import annotations

from pathlib import Path

from holoclip.clip_policy import (
    ClipPolicyRule,
    PolicyDecision,
    PolicyOperation,
    append_policy_rule,
    evaluate_source_metadata,
    evaluate_source_ranges,
    load_policy_rules,
)
from holoclip.models import TranscriptSegment
from holoclip.providers.source.base import SourceMetadata


def metadata(
    *,
    title: str = "normal stream",
    description: str | None = None,
    availability: str | None = "public",
    live_status: str | None = "was_live",
) -> SourceMetadata:
    return SourceMetadata(
        provider="test",
        url="https://www.youtube.com/watch?v=source",
        webpage_url="https://www.youtube.com/watch?v=source",
        external_id="source",
        title=title,
        channel="Talent Name",
        channel_id="UCtalent",
        description=description,
        availability=availability,
        live_status=live_status,
    )


def test_explicit_no_clip_title_is_hard_block() -> None:
    result = evaluate_source_metadata(
        metadata(title="雑談 ※切り抜き禁止※"),
        operation=PolicyOperation.PUBLISH,
    )

    assert result.decision is PolicyDecision.BLOCK
    assert any(item.source == "source_metadata.title" for item in result.findings)


def test_paid_or_restricted_source_is_hard_block() -> None:
    result = evaluate_source_metadata(
        metadata(availability="subscriber_only"),
    )

    assert result.decision is PolicyDecision.BLOCK
    assert "subscriber_only" in result.findings[0].reason


def test_live_source_requires_review() -> None:
    result = evaluate_source_metadata(
        metadata(live_status="is_live"),
        operation=PolicyOperation.PUBLISH,
    )

    assert result.decision is PolicyDecision.REVIEW


def test_range_rule_only_applies_when_selected_range_overlaps() -> None:
    rule = ClipPolicyRule(
        id="no-clip-sensitive-part",
        decision=PolicyDecision.BLOCK,
        reason="talent requested this section not be clipped",
        source_id="source",
        start=100,
        end=150,
        evidence_url="https://example.invalid/evidence",
    )

    safe = evaluate_source_ranges(
        metadata(),
        ranges=[(10, 50)],
        rules=[rule],
    )
    blocked = evaluate_source_ranges(
        metadata(),
        ranges=[(120, 180)],
        rules=[rule],
    )

    assert safe.decision is PolicyDecision.ALLOW
    assert blocked.decision is PolicyDecision.BLOCK
    assert blocked.findings[0].rule_id == rule.id


def test_spoken_no_clip_marker_requires_review_near_selected_range() -> None:
    transcript = [
        TranscriptSegment(
            start=200,
            end=205,
            text="ここは切り抜かないでください",
        )
    ]

    result = evaluate_source_ranges(
        metadata(),
        ranges=[(250, 270)],
        transcript=transcript,
        spoken_marker_margin_seconds=120,
    )

    assert result.decision is PolicyDecision.REVIEW
    assert result.findings[0].source == "source_transcript"


def test_spoken_marker_does_not_affect_distant_range() -> None:
    transcript = [
        TranscriptSegment(
            start=200,
            end=205,
            text="ここは切り抜かないでください",
        )
    ]

    result = evaluate_source_ranges(
        metadata(),
        ranges=[(1000, 1050)],
        transcript=transcript,
    )

    assert result.decision is PolicyDecision.ALLOW


def test_policy_registry_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "rules.jsonl"
    rule = ClipPolicyRule(
        id="source-ban",
        decision=PolicyDecision.BLOCK,
        reason="explicit source-level request",
        source_id="source",
        evidence_url="https://example.invalid/request",
    )

    append_policy_rule(path, rule)
    loaded = load_policy_rules(path)

    assert loaded == [rule]
