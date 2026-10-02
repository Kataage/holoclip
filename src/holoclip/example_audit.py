from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from holoclip.examples import HumanClipExample, WeakNegativeExample, load_human_clip_store
from holoclip.models import TranscriptSegment
from holoclip.retrieval import RetrievalDocument
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
        source_gap = (
            max(0.0, segment.source_start - previous_source_end)
            if previous_source_end is not None
            else None
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

    clip_selected_seconds = sum(
        item.clip_end - item.clip_start for item in example.alignment_segments
    )
    source_selected_seconds = sum(
        item.source_end - item.source_start for item in example.alignment_segments
    )
    source_bounding_seconds = (
        example.source_end - example.source_start
        if example.source_start is not None and example.source_end is not None
        else 0.0
    )
    source_gap_seconds = max(0.0, source_bounding_seconds - source_selected_seconds)

    retrieval = [
        RetrievalAuditDocument(
            id=document.id,
            kind=document.kind.value,
            embedding_model=document.embedding_model,
            embedding_version=document.embedding_version,
            text=document.text,
        )
        for document in (retrieval_documents or [])
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
        f"clip selected: {report.clip_selected_seconds:.1f}s",
        f"source selected: {report.source_selected_seconds:.1f}s",
        f"removed/gap inside bounding range: {report.source_gap_seconds:.1f}s",
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
        if segment.source_gap_from_previous is not None:
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
