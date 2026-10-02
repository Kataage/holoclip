import pytest

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.models import ClipCandidate


class FakeLLM:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0

    async def chat_json(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls += 1
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
