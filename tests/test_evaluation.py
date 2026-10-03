from holoclip.evaluation import (
    EvaluationCandidate,
    EvaluationRun,
    FeedbackRecord,
    KnownClip,
    build_blind_ab_run,
    build_run,
    candidate_pool_fingerprint,
    compare_evaluation_metrics,
    compute_metrics,
    known_clip_hit,
)
from holoclip.models import (
    CandidateRetrievalHit,
    CandidateRetrievalTrace,
    ClipCandidate,
    ReviewDecision,
)


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


def test_build_run_can_store_media_path() -> None:
    run = build_run(
        stream_id="stream",
        transcript_path="data/transcripts/stream.json",
        media_path="D:/video/stream.mkv",
        candidates=[candidate(10, 20, "candidate")],
    )

    assert run.media_path == "D:/video/stream.mkv"


def test_evaluation_run_is_backward_compatible_without_media_path() -> None:
    run = EvaluationRun.model_validate(
        {
            "stream_id": "legacy",
            "transcript_path": "legacy.json",
            "candidates": [],
        }
    )

    assert run.media_path is None



def test_candidate_pool_fingerprint_is_stable_and_order_sensitive() -> None:
    left = candidate(10, 20, "a")
    right = candidate(30, 40, "b")

    first = candidate_pool_fingerprint([left, right])
    second = candidate_pool_fingerprint([left.model_copy(), right.model_copy()])
    reversed_order = candidate_pool_fingerprint([right, left])

    assert first == second
    assert first != reversed_order


def test_build_run_stores_backward_compatible_metadata() -> None:
    run = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[candidate(10, 20, "candidate")],
        metadata={"variant": "baseline", "retrieval_enabled": False},
    )

    assert run.metadata["variant"] == "baseline"
    assert run.metadata["retrieval_enabled"] is False

    legacy = EvaluationRun.model_validate(
        {
            "stream_id": "legacy-metadata",
            "transcript_path": "legacy.json",
            "candidates": [],
        }
    )
    assert legacy.metadata == {}


def test_compare_evaluation_metrics_returns_retrieval_minus_baseline() -> None:
    baseline = compute_metrics(
        [
            EvaluationRun(
                stream_id="s",
                transcript_path="s.json",
                candidates=[
                    EvaluationCandidate(
                        id="a",
                        rank=1,
                        candidate=candidate(0, 10, "a"),
                    )
                ],
            )
        ],
        [
            FeedbackRecord(
                stream_id="s",
                candidate_id="a",
                decision=ReviewDecision.REJECT,
            )
        ],
    )
    retrieval = compute_metrics(
        [
            EvaluationRun(
                stream_id="s",
                transcript_path="s.json",
                candidates=[
                    EvaluationCandidate(
                        id="b",
                        rank=1,
                        candidate=candidate(20, 30, "b"),
                    )
                ],
            )
        ],
        [
            FeedbackRecord(
                stream_id="s",
                candidate_id="b",
                decision=ReviewDecision.ACCEPT,
            )
        ],
    )

    delta = compare_evaluation_metrics(baseline, retrieval)

    assert delta["accept_at_3_mean"] == 1.0
    assert delta["accept_at_5_mean"] == 1.0
    assert delta["usable_at_5_mean"] == 1.0



def test_blind_ab_run_unions_candidates_and_hides_variant_evidence() -> None:
    shared = candidate(10, 20, "shared")
    baseline_only = candidate(30, 40, "baseline-only")
    retrieval_only = candidate(50, 60, "retrieval-only")
    retrieval_only.retrieval_trace = CandidateRetrievalTrace(
        embedding_model="embed",
        embedding_version="v1",
        positives=[
            CandidateRetrievalHit(
                document_id="doc",
                example_id="example",
                score=0.9,
            )
        ],
    )
    retrieval_only.confidence = 0.99

    baseline = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[shared, baseline_only],
        metadata={"variant": "baseline"},
    )
    retrieval = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[retrieval_only, shared],
        metadata={"variant": "retrieval"},
    )

    blind = build_blind_ab_run(baseline, retrieval)

    expected_ids = {
        *(item.id for item in baseline.candidates),
        *(item.id for item in retrieval.candidates),
    }
    assert {item.id for item in blind.candidates} == expected_ids
    assert len(blind.candidates) == 3
    assert [item.rank for item in blind.candidates] == [1, 2, 3]
    assert all(item.candidate.retrieval_trace is None for item in blind.candidates)
    assert all(item.candidate.confidence == 0.5 for item in blind.candidates)
    assert blind.metadata == {
        "experiment": "retrieval-ab-blind-v1",
        "blinded": True,
    }


def test_blind_ab_order_is_independent_of_variant_rank() -> None:
    a = candidate(10, 20, "a")
    b = candidate(30, 40, "b")
    first_baseline = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[a, b],
    )
    first_retrieval = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[b, a],
    )
    second_baseline = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[b, a],
    )
    second_retrieval = build_run(
        stream_id="stream",
        transcript_path="stream.json",
        candidates=[a, b],
    )

    first = build_blind_ab_run(first_baseline, first_retrieval)
    second = build_blind_ab_run(second_baseline, second_retrieval)

    assert [item.id for item in first.candidates] == [
        item.id for item in second.candidates
    ]
