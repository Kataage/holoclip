from holoclip.evaluation import (
    EvaluationCandidate,
    EvaluationRun,
    FeedbackRecord,
    KnownClip,
    compute_metrics,
    known_clip_hit,
)
from holoclip.models import ClipCandidate, ReviewDecision


def candidate(start: float, end: float, title: str) -> ClipCandidate:
    return ClipCandidate(
        start=start,
        end=end,
        working_title=title,
        setup="setup",
        development="development",
        payoff="payoff",
        why_interesting="reason",
    )


def test_metrics_use_latest_feedback_and_rank() -> None:
    run = EvaluationRun(
        stream_id="stream-a",
        transcript_path="a.json",
        candidates=[
            EvaluationCandidate(id="a", rank=1, candidate=candidate(10, 30, "a")),
            EvaluationCandidate(id="b", rank=2, candidate=candidate(50, 80, "b")),
            EvaluationCandidate(id="c", rank=6, candidate=candidate(100, 120, "c")),
        ],
    )
    feedback = [
        FeedbackRecord(
            stream_id="stream-a",
            candidate_id="a",
            decision=ReviewDecision.REJECT,
        ),
        FeedbackRecord(
            stream_id="stream-a",
            candidate_id="a",
            decision=ReviewDecision.ACCEPT,
        ),
        FeedbackRecord(
            stream_id="stream-a",
            candidate_id="b",
            decision=ReviewDecision.MAYBE,
        ),
        FeedbackRecord(
            stream_id="stream-a",
            candidate_id="c",
            decision=ReviewDecision.ACCEPT,
        ),
    ]

    metrics = compute_metrics([run], feedback)

    assert metrics.accepted == 2
    assert metrics.maybe == 1
    assert metrics.rejected == 0
    assert metrics.accept_at_3_mean == 1
    assert metrics.accept_at_5_mean == 1
    assert metrics.usable_at_5_mean == 2
    assert metrics.candidates_per_accept == 1.5


def test_known_clip_hit_requires_overlap() -> None:
    known = KnownClip(id="known", stream_id="s", start=100, end=140)
    candidates = [
        EvaluationCandidate(
            id="candidate",
            rank=1,
            candidate=candidate(110, 135, "candidate"),
        )
    ]

    assert known_clip_hit(known, candidates)


def test_known_clip_recall() -> None:
    run = EvaluationRun(
        stream_id="s",
        transcript_path="s.json",
        candidates=[
            EvaluationCandidate(
                id="candidate",
                rank=1,
                candidate=candidate(110, 135, "candidate"),
            )
        ],
    )
    known = [
        KnownClip(id="hit", stream_id="s", start=100, end=140),
        KnownClip(id="miss", stream_id="s", start=200, end=240),
    ]

    metrics = compute_metrics([run], [], known)

    assert metrics.known_clips == 2
    assert metrics.known_clip_recall == 0.5
