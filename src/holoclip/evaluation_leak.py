from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from holoclip.alignment import normalize_match_text
from holoclip.examples import HumanClipExample
from holoclip.models import TranscriptSegment
from holoclip.transcript import load_transcript


EXACT_SEGMENT_OVERLAP_THRESHOLD = 0.10
SHINGLE_JACCARD_THRESHOLD = 0.15
MIN_NORMALIZED_SEGMENT_CHARS = 12
SHINGLE_SIZE = 24
SHINGLE_STRIDE = 8


class TrainingTranscriptLeakComparison(BaseModel):
    stream_id: str | None = None
    transcript_path: str
    example_ids: list[str] = Field(default_factory=list)
    exists: bool
    segment_count: int = 0
    duration_seconds: float = 0.0
    exact_overlap_count: int = 0
    exact_overlap_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    shingle_jaccard: float = Field(default=0.0, ge=0.0, le=1.0)
    suspected_overlap: bool = False


class EvaluationLeakReport(BaseModel):
    candidate_transcript_path: str
    candidate_segment_count: int
    candidate_duration_seconds: float
    significant_candidate_segments: int
    suspected_overlap: bool
    missing_training_transcripts: int
    comparisons: list[TrainingTranscriptLeakComparison] = Field(default_factory=list)


def _duration(segments: list[TranscriptSegment]) -> float:
    return max((item.end for item in segments), default=0.0)


def _significant_normalized_segments(
    segments: list[TranscriptSegment],
) -> list[str]:
    normalized = [
        normalize_match_text(item.text)
        for item in segments
    ]
    return [
        text
        for text in normalized
        if len(text) >= MIN_NORMALIZED_SEGMENT_CHARS
    ]


def _character_shingles(texts: list[str]) -> set[str]:
    shingles: set[str] = set()
    for text in texts:
        if len(text) < SHINGLE_SIZE:
            if text:
                shingles.add(text)
            continue
        for start in range(0, len(text) - SHINGLE_SIZE + 1, SHINGLE_STRIDE):
            shingles.add(text[start : start + SHINGLE_SIZE])
        tail = text[-SHINGLE_SIZE:]
        if tail:
            shingles.add(tail)
    return shingles


def compare_transcripts_for_leakage(
    candidate: list[TranscriptSegment],
    training: list[TranscriptSegment],
) -> tuple[int, float, float, bool]:
    candidate_texts = _significant_normalized_segments(candidate)
    training_texts = _significant_normalized_segments(training)

    training_set = set(training_texts)
    exact_overlap_count = sum(
        1 for text in candidate_texts if text in training_set
    )
    exact_overlap_ratio = (
        exact_overlap_count / len(candidate_texts)
        if candidate_texts
        else 0.0
    )

    candidate_shingles = _character_shingles(candidate_texts)
    training_shingles = _character_shingles(training_texts)
    union = candidate_shingles | training_shingles
    shingle_jaccard = (
        len(candidate_shingles & training_shingles) / len(union)
        if union
        else 0.0
    )

    suspected_overlap = (
        exact_overlap_ratio >= EXACT_SEGMENT_OVERLAP_THRESHOLD
        or shingle_jaccard >= SHINGLE_JACCARD_THRESHOLD
    )
    return (
        exact_overlap_count,
        exact_overlap_ratio,
        shingle_jaccard,
        suspected_overlap,
    )


def build_evaluation_leak_report(
    candidate_path: Path,
    examples: list[HumanClipExample],
) -> EvaluationLeakReport:
    candidate = load_transcript(candidate_path)
    significant_candidate_segments = len(
        _significant_normalized_segments(candidate)
    )

    grouped: dict[tuple[str | None, str], list[str]] = {}
    for example in examples:
        if not example.source_transcript_path:
            continue
        key = (example.stream_id, example.source_transcript_path)
        grouped.setdefault(key, []).append(example.id)

    comparisons: list[TrainingTranscriptLeakComparison] = []
    missing_training_transcripts = 0

    for (stream_id, transcript_path), example_ids in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0] or "",
            item[0][1],
        ),
    ):
        path = Path(transcript_path)
        if not path.exists():
            missing_training_transcripts += 1
            comparisons.append(
                TrainingTranscriptLeakComparison(
                    stream_id=stream_id,
                    transcript_path=transcript_path,
                    example_ids=sorted(example_ids),
                    exists=False,
                )
            )
            continue

        training = load_transcript(path)
        (
            exact_overlap_count,
            exact_overlap_ratio,
            shingle_jaccard,
            suspected_overlap,
        ) = compare_transcripts_for_leakage(candidate, training)
        comparisons.append(
            TrainingTranscriptLeakComparison(
                stream_id=stream_id,
                transcript_path=transcript_path,
                example_ids=sorted(example_ids),
                exists=True,
                segment_count=len(training),
                duration_seconds=_duration(training),
                exact_overlap_count=exact_overlap_count,
                exact_overlap_ratio=exact_overlap_ratio,
                shingle_jaccard=shingle_jaccard,
                suspected_overlap=suspected_overlap,
            )
        )

    return EvaluationLeakReport(
        candidate_transcript_path=str(candidate_path),
        candidate_segment_count=len(candidate),
        candidate_duration_seconds=_duration(candidate),
        significant_candidate_segments=significant_candidate_segments,
        suspected_overlap=any(
            item.suspected_overlap for item in comparisons
        ),
        missing_training_transcripts=missing_training_transcripts,
        comparisons=comparisons,
    )
