"""Deterministic, auditable observations of human selection and edit geometry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from holoclip.example_quality import LearningQuality, evaluate_human_example_learning_quality
from holoclip.examples import WEAK_NEGATIVE_METHOD, HumanClipExample, human_clip_identity
from holoclip.execution import make_input_fingerprint
from holoclip.transcript import load_transcript

CARD_VERSION = "editorial-learning-card-v2"


class EditorialLearningCard(BaseModel):
    version: str = CARD_VERSION
    example_id: str
    stream_id: str | None = None
    setup: str
    development: str
    payoff: str
    why_selected: str
    traits: list[str] = Field(default_factory=list)
    context_requirement: str = "medium"
    clip_duration_seconds: float | None = None
    selected_source_seconds: float | None = None
    source_span_seconds: float | None = None
    compression_ratio: float | None = None
    alignment_segments: int = 0
    selected_ranges: list[dict[str, Any]] = Field(default_factory=list)
    removed_ranges: list[dict[str, Any]] = Field(default_factory=list)
    uncertain_gaps: list[dict[str, Any]] = Field(default_factory=list)
    removed_source_gap_seconds: list[float] = Field(default_factory=list)
    observed_cut_style: str = "unknown"
    weak_negative_refs: list[str] = Field(default_factory=list)
    weak_negative_reasons: list[str] = Field(default_factory=list)
    weak_negative_excerpts: list[str] = Field(default_factory=list)
    contrast_evidence: list[dict[str, Any]] = Field(default_factory=list)
    clip_transcript: list[dict[str, Any]] = Field(default_factory=list)
    selected_content: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    unknowns: list[str] = Field(default_factory=list)


def _read_segments(path: str | None, root: Path) -> list:
    if not path:
        return []
    target = Path(path.replace("\\", "/"))
    if not target.is_absolute():
        target = root / target
    return load_transcript(target) if target.exists() else []


def _excerpt(segments: list, start: float, end: float) -> str:
    return "\n".join(
        f"[{s.start:.3f}-{s.end:.3f}] {s.text}" for s in segments if s.end > start and s.start < end
    )


def _union_seconds(ranges: list[tuple[float, float]]) -> float:
    end = 0.0
    total = 0.0
    for left, right in sorted(ranges):
        total += max(0.0, right - max(left, end))
        end = max(end, right)
    return round(total, 3)


def build_editorial_learning_cards(
    examples: list[HumanClipExample],
    *,
    transcript_root: Path = Path("."),
) -> list[EditorialLearningCard]:
    by_id: dict[str, HumanClipExample] = {}
    for example in examples:
        if example.id in by_id:
            raise ValueError(f"duplicate human example id: {example.id}")
        by_id[example.id] = example
    cards = []
    clip_identities = set()
    for example in sorted(by_id.values(), key=lambda x: x.id):
        quality = evaluate_human_example_learning_quality(example)
        if quality.decision is not LearningQuality.TRUSTED:
            continue
        identity = human_clip_identity(example.clip_url)
        if identity in clip_identities:
            raise ValueError(f"duplicate trusted human video identity: {identity}")
        clip_identities.add(identity)
        source = _read_segments(example.source_transcript_path, transcript_root)
        clip = _read_segments(example.clip_transcript_path, transcript_root)
        ordered = sorted(example.alignment_segments, key=lambda s: (s.clip_start, s.clip_end))
        selected = []
        for s in ordered:
            selected.append(
                {
                    **s.model_dump(mode="json"),
                    "source_excerpt": _excerpt(source, s.source_start, s.source_end),
                }
            )
        removed, uncertain = [], []
        for left, right in zip(ordered, ordered[1:]):
            source_gap = right.source_start - left.source_end
            if source_gap <= 0:
                continue
            item = {
                "start": left.source_end,
                "end": right.source_start,
                "clip_gap_seconds": max(0.0, right.clip_start - left.clip_end),
                "excerpt": _excerpt(source, left.source_end, right.source_start),
            }
            # Match the existing coverage-aware weak-negative safety contract.
            reliable = all(s.matched_anchors >= 2 or s.confidence >= 0.75 for s in (left, right))
            (removed if reliable and item["clip_gap_seconds"] <= 2 else uncertain).append(item)
        negatives = (
            example.weak_negatives if example.weak_negative_method == WEAK_NEGATIVE_METHOD else []
        )
        contrasts = [
            {
                "id": f"{example.id}:weak:{i}",
                "positive_example_id": example.id,
                **n.model_dump(mode="json"),
                "interpretation": "contextual_omission",
            }
            for i, n in enumerate(negatives)
        ]
        ranges = [(s.source_start, s.source_end) for s in ordered]
        selected_seconds = _union_seconds(ranges) if ranges else None
        span = (
            max(s.source_end for s in ordered) - min(s.source_start for s in ordered)
            if ordered
            else None
        )
        unknowns = ["editor_intent_is_inferred", "unaligned_clip_material_is_not_an_editorial_cut"]
        if not clip:
            unknowns.append("setup_and_payoff_timing_unavailable")
        if not source:
            unknowns.append("source_context_unavailable")
        cards.append(
            EditorialLearningCard(
                example_id=example.id,
                stream_id=example.stream_id,
                setup=example.setup,
                development=example.development,
                payoff=example.payoff,
                why_selected=example.why_selected,
                traits=sorted(set(example.traits)),
                context_requirement=example.context_requirement,
                clip_duration_seconds=example.clip_duration_seconds,
                selected_source_seconds=selected_seconds,
                source_span_seconds=span,
                compression_ratio=(
                    example.clip_duration_seconds / span
                    if example.clip_duration_seconds is not None and span
                    else None
                ),
                alignment_segments=len(ordered),
                selected_ranges=selected,
                removed_ranges=removed,
                uncertain_gaps=uncertain,
                removed_source_gap_seconds=[round(r["end"] - r["start"], 3) for r in removed],
                observed_cut_style="multi_cut"
                if removed
                else "unknown"
                if uncertain
                else "single_span",
                weak_negative_refs=[n["id"] for n in contrasts],
                weak_negative_reasons=[n["reason"] for n in contrasts],
                weak_negative_excerpts=[n["transcript_excerpt"] for n in contrasts],
                contrast_evidence=contrasts,
                clip_transcript=[
                    {"id": f"clip:{i}", **s.model_dump(mode="json")} for i, s in enumerate(clip)
                ],
                selected_content=example.transcript_excerpt or "",
                provenance={
                    "clip_url": example.clip_url,
                    "source_url": example.source_url,
                    "alignment_method": example.alignment_method,
                    "alignment_confidence": example.alignment_confidence,
                    "structure_method": example.structure_method,
                    "structure_model": example.structure_model,
                    "weak_negative_method": example.weak_negative_method,
                    "quality": quality.model_dump(mode="json"),
                    "metadata": {
                        "members": sorted(example.members),
                        "topics": sorted(example.topics),
                        "clip_channel": example.clip_channel,
                    },
                    "source_transcript_fingerprint": make_input_fingerprint(
                        [s.model_dump(mode="json") for s in source]
                    ),
                    "clip_transcript_fingerprint": make_input_fingerprint(
                        [s.model_dump(mode="json") for s in clip]
                    ),
                },
                unknowns=unknowns,
            )
        )
    return cards


def card_fingerprint(cards: list[EditorialLearningCard]) -> str:
    return make_input_fingerprint(
        [c.model_dump(mode="json") for c in sorted(cards, key=lambda x: x.example_id)]
    )


def boundary_measurements(card: EditorialLearningCard, payload: dict) -> dict[str, Any]:
    """LLM supplies transcript IDs; code owns timing. Unknown beats stay unknown."""
    by_id = {s["id"]: s for s in card.clip_transcript}
    result: dict[str, Any] = {
        "setup_lead_seconds": None,
        "post_payoff_seconds": None,
        "timing_basis": "unknown",
    }
    setup = by_id.get(payload.get("development_start_segment_id"))
    payoff = by_id.get(payload.get("payoff_end_segment_id"))
    duration = card.clip_duration_seconds
    if setup and payoff and duration and 0 <= setup["start"] < payoff["end"] <= duration:
        result.update(
            setup_lead_seconds=round(setup["start"], 3),
            post_payoff_seconds=round(duration - payoff["end"], 3),
            timing_basis="model_identified_measured_transcript_segments",
            development_start_segment_id=setup["id"],
            payoff_end_segment_id=payoff["id"],
        )
    return result
