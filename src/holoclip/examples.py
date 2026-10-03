from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


WEAK_NEGATIVE_METHOD = "coverage-aware-v2"


class ClipPerformance(BaseModel):
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    age_days: float | None = Field(default=None, ge=0)
    channel_baseline_ratio: float | None = Field(default=None, ge=0)
    normalized_score: float | None = None


class AlignmentSegmentExample(BaseModel):
    clip_start: float = Field(ge=0)
    clip_end: float = Field(gt=0)
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    matched_anchors: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> AlignmentSegmentExample:
        if self.clip_end <= self.clip_start:
            raise ValueError("clip_end must be greater than clip_start")
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class WeakNegativeExample(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    transcript_excerpt: str
    reason: str = "adjacent_unselected"

    @model_validator(mode="after")
    def validate_range(self) -> WeakNegativeExample:
        if self.end <= self.start:
            raise ValueError("negative end must be greater than start")
        return self


class HumanClipExample(BaseModel):
    id: str
    clip_url: str
    clip_title: str
    clip_channel: str | None = None

    source_url: str | None = None
    source_title: str | None = None
    stream_id: str | None = None
    source_start: float | None = Field(default=None, ge=0)
    source_end: float | None = Field(default=None, ge=0)
    source_event_id: str | None = None
    alignment_confidence: float | None = Field(default=None, ge=0, le=1)
    alignment_method: str | None = None
    alignment_matched_anchors: int | None = Field(default=None, ge=0)
    alignment_total_anchors: int | None = Field(default=None, ge=0)
    alignment_segments: list[AlignmentSegmentExample] = Field(default_factory=list)

    clip_transcript_path: str | None = None
    source_transcript_path: str | None = None
    transcript_excerpt: str | None = None

    members: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    relationship_context: str | None = None
    context_requirement: str = "medium"

    setup: str = ""
    development: str = ""
    payoff: str = ""
    why_selected: str = ""
    weak_negatives: list[WeakNegativeExample] = Field(default_factory=list)
    weak_negative_method: str | None = None
    structure_method: str | None = None
    structure_model: str | None = None

    performance: ClipPerformance | None = None
    clipped_by_count: int = Field(default=1, ge=1)
    notes: str | None = None
    acquisition_provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_range(self) -> HumanClipExample:
        one_missing = (self.source_start is None) != (self.source_end is None)
        if one_missing:
            raise ValueError("source_start and source_end must both be set or both be omitted")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end <= self.source_start
        ):
            raise ValueError("source_end must be greater than source_start")
        return self


class HumanClipExampleStats(BaseModel):
    total: int
    aligned: int
    unaligned: int
    with_story_structure: int
    trusted_for_learning: int
    needs_review: int
    rejected_for_learning: int
    unique_streams: int
    unique_members: int


def make_human_clip_example_id(clip_url: str) -> str:
    return hashlib.sha256(clip_url.strip().encode("utf-8")).hexdigest()[:16]


def _normalize_manifest_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    clip_url = str(normalized.get("clip_url", "")).strip()
    if not clip_url:
        raise ValueError("human clip example requires clip_url")
    normalized.setdefault("id", make_human_clip_example_id(clip_url))
    return normalized


def load_human_clip_manifest(path: Path) -> list[HumanClipExample]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("examples")
    if not isinstance(raw, list):
        raise TypeError("human clip manifest must be a list or an object with an examples list")

    examples: list[HumanClipExample] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"human clip manifest item {index} must be an object")
        examples.append(HumanClipExample.model_validate(_normalize_manifest_item(item)))
    return examples


def load_human_clip_store(path: Path) -> list[HumanClipExample]:
    if not path.exists():
        return []

    examples: list[HumanClipExample] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            examples.append(HumanClipExample.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"invalid human clip example at {path}:{line_number}") from exc
    return examples


def upsert_human_clip_examples(
    path: Path,
    examples: list[HumanClipExample],
) -> tuple[int, int, int]:
    existing = {example.id: example for example in load_human_clip_store(path)}
    added = 0
    updated = 0

    for example in examples:
        if example.id in existing:
            updated += 1
        else:
            added += 1
        existing[example.id] = example

    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(), key=lambda example: example.id)
    with path.open("w", encoding="utf-8") as handle:
        for example in ordered:
            handle.write(json.dumps(example.model_dump(mode="json"), ensure_ascii=False))
            handle.write("\n")

    return added, updated, len(ordered)


def compute_human_clip_stats(
    examples: list[HumanClipExample],
) -> HumanClipExampleStats:
    aligned = sum(
        1
        for example in examples
        if example.source_start is not None and example.source_end is not None
    )
    with_story_structure = sum(
        1
        for example in examples
        if bool(example.setup and example.development and example.payoff)
    )
    from holoclip.example_quality import (
        LearningQuality,
        evaluate_human_example_learning_quality,
    )

    quality = [
        evaluate_human_example_learning_quality(example).decision
        for example in examples
    ]
    streams = {example.stream_id for example in examples if example.stream_id}
    members = {member for example in examples for member in example.members}

    return HumanClipExampleStats(
        total=len(examples),
        aligned=aligned,
        unaligned=len(examples) - aligned,
        with_story_structure=with_story_structure,
        trusted_for_learning=sum(
            1 for decision in quality if decision is LearningQuality.TRUSTED
        ),
        needs_review=sum(
            1 for decision in quality if decision is LearningQuality.REVIEW
        ),
        rejected_for_learning=sum(
            1 for decision in quality if decision is LearningQuality.REJECTED
        ),
        unique_streams=len(streams),
        unique_members=len(members),
    )
