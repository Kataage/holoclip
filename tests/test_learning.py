from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holoclip.examples import AlignmentSegmentExample, HumanClipExample
from holoclip.execution import StageArtifactCache
from holoclip.learning import build_adjacent_weak_negatives, enrich_human_clip_example
from holoclip.models import TranscriptSegment
from holoclip.repository import SQLiteCanonicalRepository


class FakeLLM:
    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=0.2,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> str:
        raise AssertionError("chat should not be called")

    async def chat_json(
        self,
        messages,
        *,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> dict[str, Any]:
        assert model == "strong-model"
        assert reasoning_effort == "high"
        assert "Clip transcript" in messages[1]["content"]
        return {
            "setup": "貯金への不安を語る",
            "development": "給料が入らなかった時期を告白",
            "payoff": "もやし生活なので困らなかったと笑う",
            "why_selected": "意外な告白から明確なオチまである",
            "traits": ["unexpected_confession", "setup_payoff"],
            "members": ["Su"],
            "topics": ["お金", "新人時代"],
            "relationship_context": "",
            "context_requirement": "low",
        }


def seg(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text)


def example() -> HumanClipExample:
    return HumanClipExample(
        id="clip",
        clip_url="https://example.invalid/clip",
        clip_title="給料の話",
        source_start=100,
        source_end=160,
        transcript_excerpt="二ヶ月くらい給料が入らなかった。もやし生活だった。",
    )


def test_build_adjacent_weak_negatives() -> None:
    source = [
        seg(0, 20, "前の話"),
        seg(30, 50, "まだ前の話"),
        seg(100, 120, "切り抜き本体"),
        seg(145, 160, "切り抜き本体の続き"),
        seg(180, 200, "後の話"),
        seg(220, 240, "さらに後の話"),
    ]

    negatives = build_adjacent_weak_negatives(
        example(),
        source,
        window_seconds=60,
        gap_seconds=10,
    )

    assert len(negatives) == 2
    assert negatives[0].start == pytest.approx(30)
    assert negatives[0].end == pytest.approx(90)
    assert "前の話" in negatives[0].transcript_excerpt
    assert negatives[1].start == pytest.approx(170)
    assert negatives[1].end == pytest.approx(230)
    assert "後の話" in negatives[1].transcript_excerpt


@pytest.mark.asyncio
async def test_enrich_human_clip_example_sets_structure_and_negatives() -> None:
    source = [
        seg(20, 40, "前の雑談"),
        seg(100, 120, "給料が入らなかった"),
        seg(130, 150, "貯金で生活した"),
        seg(180, 200, "次の話題"),
    ]

    enriched = await enrich_human_clip_example(
        FakeLLM(),
        example(),
        source,
        model="strong-model",
        reasoning_effort="high",
        weak_negative_window_seconds=60,
    )

    assert enriched.setup == "貯金への不安を語る"
    assert enriched.payoff == "もやし生活なので困らなかったと笑う"
    assert enriched.context_requirement == "low"
    assert enriched.traits == ["setup_payoff", "unexpected_confession"]
    assert enriched.structure_method == "glm-structure-v1"
    assert enriched.structure_model == "strong-model"
    assert len(enriched.weak_negatives) == 2


def test_multicut_gap_becomes_weak_negative() -> None:
    source = [
        seg(80, 90, "切り抜き前の話"),
        seg(100, 120, "採用区間その1"),
        seg(140, 160, "人間が削った中間部分"),
        seg(180, 200, "採用区間その2"),
        seg(220, 240, "切り抜き後の話"),
    ]
    item = HumanClipExample(
        id="multicut",
        clip_url="https://example.invalid/multicut",
        clip_title="複数カット",
        source_start=100,
        source_end=200,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=20,
                source_start=100,
                source_end=120,
                confidence=0.95,
                matched_anchors=3,
            ),
            AlignmentSegmentExample(
                clip_start=20,
                clip_end=40,
                source_start=180,
                source_end=200,
                confidence=0.95,
                matched_anchors=3,
            ),
        ],
    )

    negatives = build_adjacent_weak_negatives(
        item,
        source,
        window_seconds=60,
        gap_seconds=10,
    )

    reasons = {negative.reason for negative in negatives}
    assert "removed_between_selected_segments" in reasons
    middle = next(
        negative
        for negative in negatives
        if negative.reason == "removed_between_selected_segments"
    )
    assert middle.start == pytest.approx(130)
    assert middle.end == pytest.approx(170)
    assert "人間が削った中間部分" in middle.transcript_excerpt



def test_unmapped_clip_gap_does_not_become_removed_weak_negative() -> None:
    source = [
        seg(80, 90, "切り抜き前の話"),
        seg(100, 120, "採用区間その1"),
        seg(140, 160, "alignerが見失った可能性のある区間"),
        seg(180, 200, "採用区間その2"),
        seg(220, 240, "切り抜き後の話"),
    ]
    item = HumanClipExample(
        id="sparse-multicut",
        clip_url="https://example.invalid/sparse-multicut",
        clip_title="疎な複数カット",
        source_start=100,
        source_end=200,
        alignment_confidence=0.9,
        alignment_method="transcript-anchor-v5",
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=20,
                source_start=100,
                source_end=120,
                confidence=0.95,
                matched_anchors=3,
            ),
            AlignmentSegmentExample(
                clip_start=80,
                clip_end=100,
                source_start=180,
                source_end=200,
                confidence=0.95,
                matched_anchors=3,
            ),
        ],
    )

    negatives = build_adjacent_weak_negatives(
        item,
        source,
        window_seconds=60,
        gap_seconds=10,
    )

    assert all(
        negative.reason != "removed_between_selected_segments"
        for negative in negatives
    )


class CountingLLM(FakeLLM):
    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(self, *args, **kwargs) -> dict[str, Any]:
        self.calls += 1
        return await super().chat_json(*args, **kwargs)


@pytest.mark.asyncio
async def test_enrichment_reuses_successful_stage_artifact(tmp_path: Path) -> None:
    source = [
        seg(20, 40, "前の雑談"),
        seg(100, 120, "給料が入らなかった"),
        seg(130, 150, "貯金で生活した"),
        seg(180, 200, "次の話題"),
    ]
    llm = CountingLLM()
    database = tmp_path / "holoclip.db"

    with SQLiteCanonicalRepository(database) as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")
        first = await enrich_human_clip_example(
            llm,
            example(),
            source,
            model="strong-model",
            reasoning_effort="high",
            weak_negative_window_seconds=60,
            stage_cache=cache,
            cache_provider="nvidia_nim",
        )
        second = await enrich_human_clip_example(
            llm,
            example(),
            source,
            model="strong-model",
            reasoning_effort="high",
            weak_negative_window_seconds=60,
            stage_cache=cache,
            cache_provider="nvidia_nim",
        )

    assert llm.calls == 1
    assert second.setup == first.setup
    assert second.why_selected == first.why_selected
    assert second.weak_negatives == first.weak_negatives
