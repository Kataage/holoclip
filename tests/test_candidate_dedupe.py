from holoclip.analysis.candidate_detector import dedupe_candidates
from holoclip.models import ClipCandidate


def candidate(start: float, end: float, confidence: float) -> ClipCandidate:
    return ClipCandidate(
        start=start,
        end=end,
        working_title="test",
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
        confidence=confidence,
        source_chunk_ids=["chunk"],
    )


def test_dedupe_prefers_higher_confidence_overlap() -> None:
    low = candidate(100, 160, 0.6)
    high = candidate(105, 158, 0.9)

    result = dedupe_candidates([low, high])

    assert len(result) == 1
    assert result[0].confidence == 0.9
