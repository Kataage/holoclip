from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from holoclip.models import ClipCandidate, ReviewDecision


class EvaluationCandidate(BaseModel):
    id: str
    rank: int = Field(ge=1)
    candidate: ClipCandidate


class EvaluationRun(BaseModel):
    stream_id: str
    transcript_path: str
    media_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    candidates: list[EvaluationCandidate]


class RetrievalABManifest(BaseModel):
    experiment_version: str = "retrieval-ab-v1"
    stream_id: str
    transcript_path: str
    baseline_run_path: str
    retrieval_run_path: str
    blind_run_path: str | None = None
    guided_only_run_path: str | None = None
    candidate_pool_count: int
    candidate_pool_fingerprint: str
    retrieval_candidate_pool_count: int | None = None
    retrieval_candidate_pool_fingerprint: str | None = None
    guided_added_candidates: int = Field(default=0, ge=0)
    retrieval_document_count: int
    retrieval_corpus_fingerprint: str
    retrieval_version: str
    embedding_model: str
    positive_k: int = Field(ge=0)
    negative_k: int = Field(ge=0)
    top_k: int = Field(ge=1)
    window_minutes: float = Field(gt=0)
    overlap_minutes: float = Field(ge=0)
    fast_model: str
    strong_model: str
    final_temperature: float = Field(ge=0)
    guided_discovery_enabled: bool = False
    final_retrieval_enabled: bool = False


class FeedbackRecord(BaseModel):
    stream_id: str
    candidate_id: str
    decision: ReviewDecision
    reason: str | None = None


class KnownClip(BaseModel):
    id: str
    stream_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str | None = None


class EvaluationMetrics(BaseModel):
    streams: int
    labeled_candidates: int
    accepted: int
    maybe: int
    rejected: int
    accept_at_3_mean: float
    accept_at_5_mean: float
    usable_at_5_mean: float
    candidates_per_accept: float | None
    known_clips: int
    known_clip_recall: float | None


class GuidedDiscoveryMetrics(BaseModel):
    streams: int
    guided_only_candidates: int
    labeled_candidates: int
    accepted: int
    maybe: int
    rejected: int
    accept_rate: float | None
    usable_rate: float | None
    accepted_not_selected: int
    usable_not_selected: int


class RetrievalABDiffCandidate(BaseModel):
    candidate_id: str
    working_title: str
    start: float
    end: float
    decision: ReviewDecision | None = None
    baseline_rank: int | None = None
    retrieval_rank: int | None = None
    rank_delta: int | None = None
    baseline_confidence: float | None = None
    retrieval_confidence: float | None = None
    confidence_delta: float | None = None
    retrieval_selector_reason: str | None = None
    positive_examples: list[dict[str, float | str]] = Field(default_factory=list)
    weak_negative_examples: list[dict[str, float | str]] = Field(default_factory=list)


class RetrievalABDiff(BaseModel):
    stream_id: str
    baseline_count: int
    retrieval_count: int
    shared_count: int
    baseline_only_ids: list[str] = Field(default_factory=list)
    retrieval_only_ids: list[str] = Field(default_factory=list)
    set_jaccard: float
    exact_order_match: bool
    candidates: list[RetrievalABDiffCandidate]


def make_candidate_id(stream_id: str, candidate: ClipCandidate) -> str:
    raw = (
        f"{stream_id}|{candidate.start:.3f}|{candidate.end:.3f}|"
        f"{candidate.working_title}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_run(
    *,
    stream_id: str,
    transcript_path: str,
    candidates: list[ClipCandidate],
    media_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvaluationRun:
    return EvaluationRun(
        stream_id=stream_id,
        transcript_path=transcript_path,
        media_path=media_path,
        metadata=metadata or {},
        candidates=[
            EvaluationCandidate(
                id=make_candidate_id(stream_id, candidate),
                rank=index + 1,
                candidate=candidate,
            )
            for index, candidate in enumerate(candidates)
        ],
    )


def build_blind_ab_run(
    baseline: EvaluationRun,
    retrieval: EvaluationRun,
) -> EvaluationRun:
    if baseline.stream_id != retrieval.stream_id:
        raise ValueError("paired runs must have the same stream_id")
    if baseline.transcript_path != retrieval.transcript_path:
        raise ValueError("paired runs must have the same transcript_path")

    by_id: dict[str, EvaluationCandidate] = {}
    for run in (baseline, retrieval):
        for item in run.candidates:
            if item.id in by_id:
                continue
            candidate = item.candidate.model_copy(deep=True)
            candidate.retrieval_trace = None
            candidate.confidence = 0.5
            by_id[item.id] = EvaluationCandidate(
                id=item.id,
                rank=1,
                candidate=candidate,
            )

    def blind_key(item: EvaluationCandidate) -> str:
        raw = (
            f"retrieval-ab-blind-v1|{baseline.stream_id}|{item.id}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    ordered = sorted(by_id.values(), key=blind_key)
    candidates = [
        item.model_copy(update={"rank": index + 1})
        for index, item in enumerate(ordered)
    ]
    return EvaluationRun(
        stream_id=baseline.stream_id,
        transcript_path=baseline.transcript_path,
        media_path=baseline.media_path or retrieval.media_path,
        metadata={
            "experiment": "retrieval-ab-blind-v1",
            "blinded": True,
        },
        candidates=candidates,
    )


def build_guided_only_run(
    *,
    stream_id: str,
    transcript_path: str,
    baseline_pool: list[ClipCandidate],
    guided_pool: list[ClipCandidate],
    media_path: str | None = None,
) -> EvaluationRun:
    baseline_ids = {
        make_candidate_id(stream_id, candidate)
        for candidate in baseline_pool
    }
    by_id: dict[str, EvaluationCandidate] = {}
    for candidate in guided_pool:
        candidate_id = make_candidate_id(stream_id, candidate)
        if candidate_id in baseline_ids or candidate_id in by_id:
            continue
        blinded = candidate.model_copy(deep=True)
        blinded.retrieval_trace = None
        blinded.confidence = 0.5
        by_id[candidate_id] = EvaluationCandidate(
            id=candidate_id,
            rank=1,
            candidate=blinded,
        )

    def blind_key(item: EvaluationCandidate) -> str:
        raw = (
            f"guided-only-blind-v1|{stream_id}|{item.id}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    ordered = sorted(by_id.values(), key=blind_key)
    return EvaluationRun(
        stream_id=stream_id,
        transcript_path=transcript_path,
        media_path=media_path,
        metadata={
            "experiment": "guided-only-blind-v1",
            "blinded": True,
        },
        candidates=[
            item.model_copy(update={"rank": index + 1})
            for index, item in enumerate(ordered)
        ],
    )


def compute_guided_discovery_metrics(
    guided_only_runs: list[EvaluationRun],
    treatment_runs: list[EvaluationRun],
    feedback: list[FeedbackRecord],
) -> GuidedDiscoveryMetrics:
    feedback_index = latest_feedback_index(feedback)
    treatment_ids = {
        (run.stream_id, item.id)
        for run in treatment_runs
        for item in run.candidates
    }

    guided_only_candidates = 0
    labeled = 0
    accepted = 0
    maybe = 0
    rejected = 0
    accepted_not_selected = 0
    usable_not_selected = 0

    for run in guided_only_runs:
        for item in run.candidates:
            guided_only_candidates += 1
            record = feedback_index.get((run.stream_id, item.id))
            if record is None:
                continue

            labeled += 1
            selected = (run.stream_id, item.id) in treatment_ids
            if record.decision is ReviewDecision.ACCEPT:
                accepted += 1
                if not selected:
                    accepted_not_selected += 1
                    usable_not_selected += 1
            elif record.decision is ReviewDecision.MAYBE:
                maybe += 1
                if not selected:
                    usable_not_selected += 1
            else:
                rejected += 1

    usable = accepted + maybe
    return GuidedDiscoveryMetrics(
        streams=len({run.stream_id for run in guided_only_runs}),
        guided_only_candidates=guided_only_candidates,
        labeled_candidates=labeled,
        accepted=accepted,
        maybe=maybe,
        rejected=rejected,
        accept_rate=(accepted / labeled if labeled else None),
        usable_rate=(usable / labeled if labeled else None),
        accepted_not_selected=accepted_not_selected,
        usable_not_selected=usable_not_selected,
    )


def candidate_pool_fingerprint(candidates: list[ClipCandidate]) -> str:
    payload = [
        {
            "start": round(candidate.start, 3),
            "end": round(candidate.end, 3),
            "working_title": candidate.working_title,
            "setup": candidate.setup,
            "development": candidate.development,
            "payoff": candidate.payoff,
            "why_interesting": candidate.why_interesting,
            "traits": candidate.traits,
            "context_requirement": candidate.context_requirement,
            "confidence": candidate.confidence,
            "source_chunk_ids": candidate.source_chunk_ids,
            "warnings": [warning.value for warning in candidate.warnings],
        }
        for candidate in candidates
    ]
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def save_retrieval_ab_manifest(
    manifest: RetrievalABManifest,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_run(run: EvaluationRun, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_run(path: Path) -> EvaluationRun:
    return EvaluationRun.model_validate_json(path.read_text(encoding="utf-8"))


def append_feedback(record: FeedbackRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
        handle.write("\n")


def load_feedback(path: Path) -> list[FeedbackRecord]:
    if not path.exists():
        return []

    records: list[FeedbackRecord] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(FeedbackRecord.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"invalid feedback at {path}:{line_number}") from exc
    return records


def latest_feedback_index(
    feedback: list[FeedbackRecord],
) -> dict[tuple[str, str], FeedbackRecord]:
    latest: dict[tuple[str, str], FeedbackRecord] = {}
    for record in feedback:
        latest[(record.stream_id, record.candidate_id)] = record
    return latest


def load_known_clips(path: Path | None) -> list[KnownClip]:
    if path is None or not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("clips")
    if not isinstance(raw, list):
        raise TypeError("known-clips JSON must be a list or an object with a clips list")
    return [KnownClip.model_validate(item) for item in raw]


def _overlap_seconds(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def known_clip_hit(
    known: KnownClip,
    candidates: list[EvaluationCandidate],
    *,
    min_overlap_ratio: float = 0.5,
) -> bool:
    known_duration = known.end - known.start
    if known_duration <= 0:
        return False

    for item in candidates:
        candidate = item.candidate
        overlap = _overlap_seconds(known.start, known.end, candidate.start, candidate.end)
        if overlap / known_duration >= min_overlap_ratio:
            return True
    return False


def compute_metrics(
    runs: list[EvaluationRun],
    feedback: list[FeedbackRecord],
    known_clips: list[KnownClip] | None = None,
) -> EvaluationMetrics:
    known_clips = known_clips or []
    latest_feedback = latest_feedback_index(feedback)

    accepted = 0
    maybe = 0
    rejected = 0
    labeled = 0
    accept_at_3: list[int] = []
    accept_at_5: list[int] = []
    usable_at_5: list[int] = []

    for run in runs:
        top3_accept = 0
        top5_accept = 0
        top5_usable = 0

        for item in run.candidates:
            record = latest_feedback.get((run.stream_id, item.id))
            if record is None:
                continue

            labeled += 1
            if record.decision is ReviewDecision.ACCEPT:
                accepted += 1
                if item.rank <= 3:
                    top3_accept += 1
                if item.rank <= 5:
                    top5_accept += 1
                    top5_usable += 1
            elif record.decision is ReviewDecision.MAYBE:
                maybe += 1
                if item.rank <= 5:
                    top5_usable += 1
            else:
                rejected += 1

        accept_at_3.append(top3_accept)
        accept_at_5.append(top5_accept)
        usable_at_5.append(top5_usable)

    known_by_stream: dict[str, list[KnownClip]] = {}
    for known in known_clips:
        known_by_stream.setdefault(known.stream_id, []).append(known)

    known_hits = 0
    known_total = 0
    runs_by_stream = {run.stream_id: run for run in runs}
    for stream_id, clips in known_by_stream.items():
        run = runs_by_stream.get(stream_id)
        if run is None:
            continue
        for known in clips:
            known_total += 1
            if known_clip_hit(known, run.candidates):
                known_hits += 1

    stream_count = len(runs)
    return EvaluationMetrics(
        streams=stream_count,
        labeled_candidates=labeled,
        accepted=accepted,
        maybe=maybe,
        rejected=rejected,
        accept_at_3_mean=(sum(accept_at_3) / stream_count) if stream_count else 0.0,
        accept_at_5_mean=(sum(accept_at_5) / stream_count) if stream_count else 0.0,
        usable_at_5_mean=(sum(usable_at_5) / stream_count) if stream_count else 0.0,
        candidates_per_accept=(labeled / accepted) if accepted else None,
        known_clips=known_total,
        known_clip_recall=(known_hits / known_total) if known_total else None,
    )


def build_retrieval_ab_diff(
    baseline: EvaluationRun,
    retrieval: EvaluationRun,
    feedback: list[FeedbackRecord] | None = None,
) -> RetrievalABDiff:
    if baseline.stream_id != retrieval.stream_id:
        raise ValueError("paired runs must have the same stream_id")

    feedback_index = latest_feedback_index(feedback or [])
    baseline_by_id = {item.id: item for item in baseline.candidates}
    retrieval_by_id = {item.id: item for item in retrieval.candidates}
    baseline_ids = list(baseline_by_id)
    retrieval_ids = list(retrieval_by_id)
    baseline_set = set(baseline_ids)
    retrieval_set = set(retrieval_ids)
    union = baseline_set | retrieval_set

    ordered_ids = sorted(
        union,
        key=lambda candidate_id: (
            min(
                baseline_by_id.get(candidate_id).rank
                if candidate_id in baseline_by_id
                else 10**9,
                retrieval_by_id.get(candidate_id).rank
                if candidate_id in retrieval_by_id
                else 10**9,
            ),
            candidate_id,
        ),
    )

    rows: list[RetrievalABDiffCandidate] = []
    for candidate_id in ordered_ids:
        baseline_item = baseline_by_id.get(candidate_id)
        retrieval_item = retrieval_by_id.get(candidate_id)
        source_item = retrieval_item or baseline_item
        assert source_item is not None

        baseline_rank = baseline_item.rank if baseline_item is not None else None
        retrieval_rank = retrieval_item.rank if retrieval_item is not None else None
        baseline_confidence = (
            baseline_item.candidate.confidence
            if baseline_item is not None
            else None
        )
        retrieval_confidence = (
            retrieval_item.candidate.confidence
            if retrieval_item is not None
            else None
        )

        trace = (
            retrieval_item.candidate.retrieval_trace
            if retrieval_item is not None
            else None
        )
        positives = []
        weak_negatives = []
        selector_reason = None
        if trace is not None:
            selector_reason = trace.selector_reason
            positives = [
                {
                    "example_id": hit.example_id,
                    "score": hit.score,
                }
                for hit in trace.positives
            ]
            weak_negatives = [
                {
                    "example_id": hit.example_id,
                    "score": hit.score,
                }
                for hit in trace.weak_negatives
            ]

        record = feedback_index.get((baseline.stream_id, candidate_id))
        rows.append(
            RetrievalABDiffCandidate(
                candidate_id=candidate_id,
                working_title=source_item.candidate.working_title,
                start=source_item.candidate.start,
                end=source_item.candidate.end,
                decision=record.decision if record is not None else None,
                baseline_rank=baseline_rank,
                retrieval_rank=retrieval_rank,
                rank_delta=(
                    retrieval_rank - baseline_rank
                    if baseline_rank is not None and retrieval_rank is not None
                    else None
                ),
                baseline_confidence=baseline_confidence,
                retrieval_confidence=retrieval_confidence,
                confidence_delta=(
                    retrieval_confidence - baseline_confidence
                    if baseline_confidence is not None
                    and retrieval_confidence is not None
                    else None
                ),
                retrieval_selector_reason=selector_reason,
                positive_examples=positives,
                weak_negative_examples=weak_negatives,
            )
        )

    return RetrievalABDiff(
        stream_id=baseline.stream_id,
        baseline_count=len(baseline.candidates),
        retrieval_count=len(retrieval.candidates),
        shared_count=len(baseline_set & retrieval_set),
        baseline_only_ids=sorted(baseline_set - retrieval_set),
        retrieval_only_ids=sorted(retrieval_set - baseline_set),
        set_jaccard=(
            len(baseline_set & retrieval_set) / len(union)
            if union
            else 1.0
        ),
        exact_order_match=baseline_ids == retrieval_ids,
        candidates=rows,
    )


def compare_evaluation_metrics(
    baseline: EvaluationMetrics,
    retrieval: EvaluationMetrics,
) -> dict[str, float | None]:
    def delta(left: float | None, right: float | None) -> float | None:
        if left is None or right is None:
            return None
        return right - left

    return {
        "accept_at_3_mean": retrieval.accept_at_3_mean - baseline.accept_at_3_mean,
        "accept_at_5_mean": retrieval.accept_at_5_mean - baseline.accept_at_5_mean,
        "usable_at_5_mean": retrieval.usable_at_5_mean - baseline.usable_at_5_mean,
        "candidates_per_accept": delta(
            baseline.candidates_per_accept,
            retrieval.candidates_per_accept,
        ),
        "known_clip_recall": delta(
            baseline.known_clip_recall,
            retrieval.known_clip_recall,
        ),
    }


def load_runs(directory: Path) -> list[EvaluationRun]:
    if not directory.exists():
        return []
    return [
        load_run(path)
        for path in sorted(directory.glob("*.json"))
        if path.is_file()
    ]
