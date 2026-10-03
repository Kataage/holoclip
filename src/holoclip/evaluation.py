from __future__ import annotations

import hashlib
import json
from typing import Any
from pathlib import Path

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
    candidate_pool_count: int
    candidate_pool_fingerprint: str
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


def load_runs(directory: Path) -> list[EvaluationRun]:
    if not directory.exists():
        return []
    return [
        load_run(path)
        for path in sorted(directory.glob("*.json"))
        if path.is_file()
    ]
