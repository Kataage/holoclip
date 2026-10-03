import asyncio

import pytest

from holoclip.analysis.candidate_detector import (
    CandidateDetector,
    dedupe_candidates,
    merge_guided_candidates,
)
from holoclip.evaluation import candidate_pool_fingerprint
from holoclip.models import ClipCandidate, TranscriptChunk


class FakeLLM:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    async def chat_json(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.kwargs.append(dict(kwargs))
        return self.response


def make_candidate(index: int) -> ClipCandidate:
    start = float(index * 100)
    return ClipCandidate(
        start=start,
        end=start + 30,
        working_title=f"candidate-{index}",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=0.5,
    )


@pytest.mark.asyncio
async def test_final_select_can_reject_everything() -> None:
    llm = FakeLLM({"ranked": []})
    detector = CandidateDetector(llm, fast_model="fast", strong_model="strong")

    result = await detector.final_select([make_candidate(0), make_candidate(1)], top_k=5)

    assert result == []
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_final_select_uses_strong_model_even_below_top_k() -> None:
    llm = FakeLLM(
        {
            "ranked": [
                {"index": 1, "confidence": 0.91, "reason": "clearer payoff"},
                {"index": 0, "confidence": 0.72, "reason": "usable"},
            ]
        }
    )
    detector = CandidateDetector(llm, fast_model="fast", strong_model="strong")

    result = await detector.final_select([make_candidate(0), make_candidate(1)], top_k=5)

    assert [item.working_title for item in result] == ["candidate-1", "candidate-0"]
    assert result[0].confidence == 0.91
    assert llm.calls == 1



@pytest.mark.asyncio
async def test_final_select_uses_configured_temperature() -> None:
    llm = FakeLLM({"ranked": [{"index": 0}]})
    detector = CandidateDetector(
        llm,
        fast_model="fast",
        strong_model="strong",
        final_temperature=0.0,
    )

    await detector.final_select([make_candidate(0)], top_k=1)

    assert llm.kwargs[-1]["temperature"] == 0.0



def test_dedupe_equal_confidence_is_input_order_independent() -> None:
    left = ClipCandidate(
        start=10,
        end=50,
        working_title="A",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=0.8,
        source_chunk_ids=["chunk-0000"],
    )
    right = ClipCandidate(
        start=12,
        end=48,
        working_title="B",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=0.8,
        source_chunk_ids=["chunk-0001"],
    )

    forward = dedupe_candidates([left.model_copy(deep=True), right.model_copy(deep=True)])
    reverse = dedupe_candidates([right.model_copy(deep=True), left.model_copy(deep=True)])

    assert [item.working_title for item in forward] == ["A"]
    assert [item.working_title for item in reverse] == ["A"]
    assert candidate_pool_fingerprint(forward) == candidate_pool_fingerprint(reverse)
    assert forward[0].source_chunk_ids == ["chunk-0000", "chunk-0001"]


@pytest.mark.asyncio
async def test_discover_broad_ignores_async_completion_order() -> None:
    chunks = [
        TranscriptChunk(
            id="chunk-0000",
            start=0,
            end=100,
            text="first",
            segment_indices=[0],
        ),
        TranscriptChunk(
            id="chunk-0001",
            start=80,
            end=180,
            text="second",
            segment_indices=[1],
        ),
    ]

    class DelayedDetector(CandidateDetector):
        def __init__(self, delays: dict[str, float]) -> None:
            super().__init__(FakeLLM({}), fast_model="fast", strong_model="strong")
            self.delays = delays

        async def _discover_chunk(
            self,
            chunk: TranscriptChunk,
        ) -> list[ClipCandidate]:
            await asyncio.sleep(self.delays[chunk.id])
            if chunk.id == "chunk-0000":
                return [
                    ClipCandidate(
                        start=90,
                        end=130,
                        working_title="A",
                        setup="setup",
                        development="development",
                        payoff="payoff",
                        why_interesting="reason",
                        confidence=0.8,
                        source_chunk_ids=[chunk.id],
                    )
                ]
            return [
                ClipCandidate(
                    start=92,
                    end=128,
                    working_title="B",
                    setup="setup",
                    development="development",
                    payoff="payoff",
                    why_interesting="reason",
                    confidence=0.8,
                    source_chunk_ids=[chunk.id],
                )
            ]

    first = await DelayedDetector(
        {"chunk-0000": 0.02, "chunk-0001": 0.0}
    ).discover_broad(chunks)
    second = await DelayedDetector(
        {"chunk-0000": 0.0, "chunk-0001": 0.02}
    ).discover_broad(chunks)

    assert [item.working_title for item in first] == ["A"]
    assert [item.working_title for item in second] == ["A"]
    assert candidate_pool_fingerprint(first) == candidate_pool_fingerprint(second)



def test_merge_guided_candidates_preserves_baseline_and_adds_new_moment() -> None:
    baseline = [
        ClipCandidate(
            start=100,
            end=140,
            working_title="baseline",
            setup="setup",
            development="development",
            payoff="payoff",
            why_interesting="reason",
            confidence=0.6,
            source_chunk_ids=["chunk-0000"],
        )
    ]
    overlapping_guided = ClipCandidate(
        start=102,
        end=138,
        working_title="guided duplicate",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=0.99,
        source_chunk_ids=["chunk-0000"],
    )
    new_guided = ClipCandidate(
        start=200,
        end=240,
        working_title="new guided",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=0.7,
        source_chunk_ids=["chunk-0000"],
    )

    merged = merge_guided_candidates(
        baseline,
        [overlapping_guided, new_guided],
    )

    assert [item.working_title for item in merged] == [
        "baseline",
        "new guided",
    ]
    assert merged[0].confidence == 0.6


def test_merge_guided_candidates_zero_new_keeps_baseline_unchanged() -> None:
    baseline = [make_candidate(0), make_candidate(1)]

    merged = merge_guided_candidates(baseline, [])

    assert [item.model_dump() for item in merged] == [
        item.model_dump() for item in baseline
    ]
