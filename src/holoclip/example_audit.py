from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from holoclip.alignment import WEAK_SINGLETON_MAX_CONFIDENCE
from holoclip.example_quality import (
    AlignmentQualityMetrics,
    LearningQuality,
    LearningQualityReason,
    evaluate_human_example_learning_quality,
)
from holoclip.examples import (
    WEAK_NEGATIVE_METHOD,
    HumanClipExample,
    WeakNegativeExample,
    load_human_clip_store,
)
from holoclip.models import TranscriptSegment
from holoclip.retrieval import RetrievalDocument, build_retrieval_document_drafts
from holoclip.structure_validation import (
    StructureWarning,
    validate_editorial_structure,
)
from holoclip.transcript import load_transcript


class AlignmentAuditSegment(BaseModel):
    index: int
    clip_start: float
    clip_end: float
    source_start: float
    source_end: float
    confidence: float
    matched_anchors: int
    source_gap_from_previous: float | None = None
    source_delta_from_previous: float | None = None
    clip_excerpt: str = ""
    source_excerpt: str = ""


class RetrievalAuditDocument(BaseModel):
    id: str
    kind: str
    embedding_model: str
    embedding_version: str
    text: str


class HumanExampleAuditReport(BaseModel):
    example_id: str
    clip_title: str
    clip_url: str
    clip_channel: str | None = None
    source_title: str | None = None
    source_url: str | None = None
    stream_id: str | None = None

    alignment_confidence: float | None = None
    alignment_method: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    alignment_segments: list[AlignmentAuditSegment] = Field(default_factory=list)
    clip_selected_seconds: float = 0.0
    source_selected_seconds: float = 0.0
    source_bounding_seconds: float = 0.0
    source_gap_seconds: float = 0.0
    alignment_quality: AlignmentQualityMetrics = Field(
        default_factory=AlignmentQualityMetrics
    )
    learning_quality: LearningQuality = LearningQuality.REVIEW
    learning_quality_reasons: list[LearningQualityReason] = Field(default_factory=list)
    positive_retrieval_eligible: bool = False
    weak_negatives_eligible: int = 0
    weak_negatives_suppressed: int = 0

    declared_source_url: str | None = None
    declared_source_start_hint: float | None = None
    first_alignment_delta_from_hint: float | None = None
    source_resolution_reason: str | None = None
    source_resolution_score: float | None = None
    source_auto_detected: bool | None = None

    setup: str = ""
    development: str = ""
    payoff: str = ""
    why_selected: str = ""
    members: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    relationship_context: str | None = None
    context_requirement: str = "medium"
    structure_warnings: list[StructureWarning] = Field(default_factory=list)

    weak_negatives: list[WeakNegativeExample] = Field(default_factory=list)
    retrieval_documents: list[RetrievalAuditDocument] = Field(default_factory=list)
    acquisition_provenance: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def find_human_example(store: Path, example_id: str) -> HumanClipExample:
    for example in load_human_clip_store(store):
        if example.id == example_id:
            return example
    raise KeyError(f"human clip example not found: {example_id}")


def _excerpt(
    segments: list[TranscriptSegment],
    start: float,
    end: float,
    *,
    max_chars: int = 500,
) -> str:
    parts = [
        item.text.strip()
        for item in segments
        if item.end > start and item.start < end and item.text.strip()
    ]
    text = " ".join(parts)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _load_optional_transcript(
    raw_path: str | None,
    *,
    label: str,
    warnings: list[str],
) -> list[TranscriptSegment]:
    if not raw_path:
        warnings.append(f"{label} transcript path is missing")
        return []
    path = Path(raw_path)
    if not path.exists():
        warnings.append(f"{label} transcript does not exist: {path}")
        return []
    try:
        return load_transcript(path)
    except (OSError, TypeError, ValueError) as exc:
        warnings.append(f"{label} transcript could not be loaded: {exc}")
        return []


def parse_youtube_time_hint(url: str | None) -> float | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    values = parse_qs(parsed.query).get("t", [])
    if not values and parsed.fragment:
        values = parse_qs(parsed.fragment).get("t", [])
    if not values:
        return None

    raw = values[0].strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)

    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", raw):
        matched = True
        amount = float(value)
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        else:
            total += amount
    return total if matched else None


def build_human_example_audit(
    example: HumanClipExample,
    *,
    retrieval_documents: list[RetrievalDocument] | None = None,
) -> HumanExampleAuditReport:
    warnings: list[str] = []
    clip_transcript = _load_optional_transcript(
        example.clip_transcript_path,
        label="clip",
        warnings=warnings,
    )
    source_transcript = _load_optional_transcript(
        example.source_transcript_path,
        label="source",
        warnings=warnings,
    )

    audit_segments: list[AlignmentAuditSegment] = []
    previous_source_end: float | None = None
    for index, segment in enumerate(example.alignment_segments, start=1):
        source_delta = (
            segment.source_start - previous_source_end
            if previous_source_end is not None
            else None
        )
        source_gap = (
            max(0.0, source_delta)
            if source_delta is not None
            else None
        )
        if source_delta is not None and source_delta < -3.0:
            warnings.append(
                "non-monotonic alignment: "
                f"segment #{index} starts {abs(source_delta):.1f}s before "
                "the previous aligned source range ended"
            )
        if (
            segment.matched_anchors == 1
            and segment.confidence < WEAK_SINGLETON_MAX_CONFIDENCE
        ):
            warnings.append(
                "weak isolated alignment: "
                f"segment #{index} has one anchor and confidence "
                f"{segment.confidence:.3f}"
            )
        audit_segments.append(
            AlignmentAuditSegment(
                index=index,
                clip_start=segment.clip_start,
                clip_end=segment.clip_end,
                source_start=segment.source_start,
                source_end=segment.source_end,
                confidence=segment.confidence,
                matched_anchors=segment.matched_anchors,
                source_gap_from_previous=source_gap,
                source_delta_from_previous=source_delta,
                clip_excerpt=_excerpt(
                    clip_transcript,
                    segment.clip_start,
                    segment.clip_end,
                ),
                source_excerpt=_excerpt(
                    source_transcript,
                    segment.source_start,
                    segment.source_end,
                ),
            )
        )
        previous_source_end = segment.source_end

    provenance = dict(example.acquisition_provenance)
    declared_source_url = (
        provenance.get("resolved_source_url")
        or provenance.get("requested_source_url")
        or None
    )
    declared_source_start_hint = parse_youtube_time_hint(
        str(declared_source_url) if declared_source_url else None
    )
    first_alignment_delta = None
    if declared_source_start_hint is not None and audit_segments:
        first_alignment_delta = (
            audit_segments[0].source_start - declared_source_start_hint
        )

    learning_quality = evaluate_human_example_learning_quality(example)
    structure_validation = validate_editorial_structure(example)
    alignment_quality = learning_quality.metrics
    clip_selected_seconds = alignment_quality.aligned_clip_seconds
    source_selected_seconds = alignment_quality.aligned_source_seconds
    source_bounding_seconds = alignment_quality.source_bounding_seconds
    source_gap_seconds = alignment_quality.source_gap_seconds

    retrieval_source = retrieval_documents or []
    expected_drafts, _ = build_retrieval_document_drafts([example])
    expected_retrieval = {
        draft.id: draft.text_fingerprint for draft in expected_drafts
    }
    actual_retrieval = {
        document.id: document.text_fingerprint for document in retrieval_source
    }
    if actual_retrieval != expected_retrieval:
        warnings.append(
            "retrieval index does not match the current human example; "
            "run examples-index after the example is successfully enriched"
        )

    retrieval = [
        RetrievalAuditDocument(
            id=document.id,
            kind=document.kind.value,
            embedding_model=document.embedding_model,
            embedding_version=document.embedding_version,
            text=document.text,
        )
        for document in retrieval_source
    ]

    score_raw = provenance.get("source_resolution_score")
    try:
        source_resolution_score = float(score_raw) if score_raw is not None else None
    except (TypeError, ValueError):
        source_resolution_score = None

    auto_raw = provenance.get("source_auto_detected")
    source_auto_detected = auto_raw if isinstance(auto_raw, bool) else None

    return HumanExampleAuditReport(
        example_id=example.id,
        clip_title=example.clip_title,
        clip_url=example.clip_url,
        clip_channel=example.clip_channel,
        source_title=example.source_title,
        source_url=example.source_url,
        stream_id=example.stream_id,
        alignment_confidence=example.alignment_confidence,
        alignment_method=example.alignment_method,
        source_start=example.source_start,
        source_end=example.source_end,
        alignment_segments=audit_segments,
        clip_selected_seconds=clip_selected_seconds,
        source_selected_seconds=source_selected_seconds,
        source_bounding_seconds=source_bounding_seconds,
        source_gap_seconds=source_gap_seconds,
        alignment_quality=alignment_quality,
        learning_quality=learning_quality.decision,
        learning_quality_reasons=learning_quality.reasons,
        positive_retrieval_eligible=(
            learning_quality.decision is LearningQuality.TRUSTED
        ),
        weak_negatives_eligible=(
            len(example.weak_negatives)
            if learning_quality.decision is LearningQuality.TRUSTED
            and example.weak_negative_method == WEAK_NEGATIVE_METHOD
            else 0
        ),
        weak_negatives_suppressed=(
            len(example.weak_negatives)
            if learning_quality.decision is not LearningQuality.TRUSTED
            or example.weak_negative_method != WEAK_NEGATIVE_METHOD
            else 0
        ),
        declared_source_url=str(declared_source_url) if declared_source_url else None,
        declared_source_start_hint=declared_source_start_hint,
        first_alignment_delta_from_hint=first_alignment_delta,
        source_resolution_reason=provenance.get("source_resolution_reason"),
        source_resolution_score=source_resolution_score,
        source_auto_detected=source_auto_detected,
        setup=example.setup,
        development=example.development,
        payoff=example.payoff,
        why_selected=example.why_selected,
        members=example.members,
        topics=example.topics,
        traits=example.traits,
        relationship_context=example.relationship_context,
        context_requirement=example.context_requirement,
        structure_warnings=structure_validation.warnings,
        weak_negatives=example.weak_negatives,
        retrieval_documents=retrieval,
        acquisition_provenance=provenance,
        warnings=warnings,
    )


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _short(text: str, limit: int = 360) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def format_human_example_audit(report: HumanExampleAuditReport) -> str:
    lines = [
        "=== Human Clip Example Audit ===",
        f"id: {report.example_id}",
        f"clip: {report.clip_title}",
        f"clip URL: {report.clip_url}",
        f"source: {report.source_title or '-'}",
        f"source URL: {report.source_url or '-'}",
        "",
        "=== Alignment ===",
        f"confidence: {report.alignment_confidence if report.alignment_confidence is not None else '-'}",
        f"method: {report.alignment_method or '-'}",
        f"segments: {len(report.alignment_segments)}",
        f"source bounding range: {_clock(report.source_start)} - {_clock(report.source_end)}",
        f"clip timeline: {report.alignment_quality.clip_timeline_seconds:.1f}s",
        f"clip selected: {report.clip_selected_seconds:.1f}s",
        f"clip coverage: {report.alignment_quality.clip_coverage_ratio:.1%}",
        f"clip uncovered: {report.alignment_quality.uncovered_clip_seconds:.1f}s",
        (
            "largest uncovered clip gap: "
            f"{report.alignment_quality.largest_uncovered_clip_gap:.1f}s"
        ),
        f"source selected: {report.source_selected_seconds:.1f}s",
        f"removed/gap inside bounding range: {report.source_gap_seconds:.1f}s",
        (
            "anchors: "
            f"{report.alignment_quality.matched_anchors}/"
            f"{report.alignment_quality.total_anchors if report.alignment_quality.total_anchors is not None else '?'}"
        ),
        (
            "anchor coverage: "
            f"{report.alignment_quality.anchor_coverage_ratio:.1%}"
            if report.alignment_quality.anchor_coverage_ratio is not None
            else "anchor coverage: -"
        ),
        (
            "non-monotonic transitions: "
            f"{report.alignment_quality.non_monotonic_transition_count}"
        ),
        (
            "largest backward jump: "
            f"{report.alignment_quality.largest_backward_jump_seconds:.1f}s"
        ),
        f"weak singleton segments: {report.alignment_quality.weak_singleton_count}",
        "",
        "=== Learning quality ===",
        f"decision: {report.learning_quality.value}",
        (
            "reasons: "
            + (
                ", ".join(reason.value for reason in report.learning_quality_reasons)
                if report.learning_quality_reasons
                else "-"
            )
        ),
        f"positive retrieval eligible: {report.positive_retrieval_eligible}",
        f"weak negatives eligible: {report.weak_negatives_eligible}",
        f"weak negatives suppressed: {report.weak_negatives_suppressed}",
    ]

    if report.declared_source_url:
        lines.extend(
            [
                "",
                "=== Source-resolution provenance ===",
                f"declared source URL: {report.declared_source_url}",
                f"auto detected: {report.source_auto_detected}",
                f"reason: {report.source_resolution_reason or '-'}",
                f"score: {report.source_resolution_score if report.source_resolution_score is not None else '-'}",
            ]
        )
        if report.declared_source_start_hint is not None:
            lines.append(
                f"declared start hint: {_clock(report.declared_source_start_hint)}"
            )
        if report.first_alignment_delta_from_hint is not None:
            lines.append(
                "first alignment - declared hint: "
                f"{report.first_alignment_delta_from_hint:+.1f}s"
            )

    for segment in report.alignment_segments:
        lines.extend(
            [
                "",
                f"--- Alignment #{segment.index} ---",
                (
                    f"clip {_clock(segment.clip_start)} - {_clock(segment.clip_end)}"
                    f"  ->  source {_clock(segment.source_start)} - {_clock(segment.source_end)}"
                ),
                (
                    f"confidence={segment.confidence:.3f} "
                    f"anchors={segment.matched_anchors}"
                ),
            ]
        )
        if segment.source_delta_from_previous is not None:
            lines.append(
                "source delta from previous: "
                f"{segment.source_delta_from_previous:+.1f}s"
            )
        if (
            segment.source_gap_from_previous is not None
            and segment.source_gap_from_previous > 0
        ):
            lines.append(
                f"source gap from previous: {segment.source_gap_from_previous:.1f}s"
            )
        lines.append(f"clip text: {_short(segment.clip_excerpt) or '(none)'}")
        lines.append(f"source text: {_short(segment.source_excerpt) or '(none)'}")

    lines.extend(
        [
            "",
            "=== Editorial structure ===",
            f"setup: {report.setup or '-'}",
            f"development: {report.development or '-'}",
            f"payoff: {report.payoff or '-'}",
            f"why selected: {report.why_selected or '-'}",
            f"members: {', '.join(report.members) if report.members else '-'}",
            f"topics: {', '.join(report.topics) if report.topics else '-'}",
            f"traits: {', '.join(report.traits) if report.traits else '-'}",
            f"context requirement: {report.context_requirement}",
        ]
    )
    if report.relationship_context:
        lines.append(f"relationship context: {report.relationship_context}")
    if report.structure_warnings:
        lines.append("structure warnings:")
        for warning in report.structure_warnings:
            lines.append(f"  - {warning.code.value}: {warning.message}")

    lines.extend(["", f"=== Weak negatives ({len(report.weak_negatives)}) ==="])
    for index, negative in enumerate(report.weak_negatives, start=1):
        lines.append(
            f"#{index} {_clock(negative.start)} - {_clock(negative.end)} "
            f"[{negative.reason}]"
        )
        lines.append(f"  {_short(negative.transcript_excerpt, 260)}")

    lines.extend(
        [
            "",
            f"=== Retrieval documents ({len(report.retrieval_documents)}) ===",
        ]
    )
    for document in report.retrieval_documents:
        lines.append(
            f"[{document.kind}] {document.id} "
            f"model={document.embedding_model} version={document.embedding_version}"
        )
        lines.append(f"  {_short(document.text, 420)}")

    if report.acquisition_provenance:
        lines.extend(["", "=== Acquisition provenance ==="])
        for key in sorted(report.acquisition_provenance):
            lines.append(f"{key}: {report.acquisition_provenance[key]}")

    if report.warnings:
        lines.extend(["", "=== Warnings ==="])
        lines.extend(f"- {warning}" for warning in report.warnings)

    return "\n".join(lines)
