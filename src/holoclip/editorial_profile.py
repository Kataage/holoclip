from __future__ import annotations

import json
import statistics
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from holoclip.domain import stable_id
from holoclip.example_quality import (
    LearningQuality,
    evaluate_human_example_learning_quality,
)
from holoclip.examples import HumanClipExample
from holoclip.execution import StageArtifactCache, StageStatus, make_input_fingerprint
from holoclip.providers.llm.base import LLMProvider


EDITORIAL_PROFILE_VERSION = "editorial-profile-v1"
EDITORIAL_PROFILE_METHOD = "corpus-pattern-induction-v1"
PARTIAL_PROMPT_VERSION = "editorial-pattern-partial-v1"
CONSOLIDATE_PROMPT_VERSION = "editorial-pattern-consolidate-v1"

MIN_READY_TRUSTED_EXAMPLES = 30
MIN_READY_UNIQUE_STREAMS = 15
MIN_READY_CORE_PATTERNS = 5
MIN_CORE_SUPPORT_EXAMPLES = 3
MIN_CORE_SUPPORT_STREAMS = 2
MIN_READY_CORE_COVERAGE = 0.80


class PatternMaturity(StrEnum):
    EMERGING = "emerging"
    CORE = "core"


class EditorialLearningCard(BaseModel):
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
    alignment_segments: int = 0
    removed_source_gap_seconds: list[float] = Field(default_factory=list)
    weak_negative_refs: list[str] = Field(default_factory=list)
    weak_negative_reasons: list[str] = Field(default_factory=list)
    weak_negative_excerpts: list[str] = Field(default_factory=list)


class EditorialPattern(BaseModel):
    id: str
    name: str
    mechanism: str
    observable_cues: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    boundary_strategy: list[str] = Field(default_factory=list)
    context_requirement: str = "medium"
    typical_duration_min_seconds: float | None = Field(default=None, ge=0)
    typical_duration_max_seconds: float | None = Field(default=None, ge=0)
    cut_style: str = "mixed"
    support_example_ids: list[str] = Field(default_factory=list)
    support_stream_ids: list[str] = Field(default_factory=list)
    weak_negative_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    maturity: PatternMaturity = PatternMaturity.EMERGING


class LearningReadiness(BaseModel):
    ready: bool
    trusted_examples: int
    unique_streams: int
    core_patterns: int
    core_coverage_ratio: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class EditorialProfile(BaseModel):
    version: str = EDITORIAL_PROFILE_VERSION
    method: str = EDITORIAL_PROFILE_METHOD
    model: str
    corpus_fingerprint: str
    trusted_example_ids: list[str]
    trusted_examples: int
    unique_streams: int
    global_principles: list[str] = Field(default_factory=list)
    rejection_principles: list[str] = Field(default_factory=list)
    boundary_principles: list[str] = Field(default_factory=list)
    patterns: list[EditorialPattern] = Field(default_factory=list)
    readiness: LearningReadiness


def _clean_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            str(item).strip()
            for item in value
            if str(item).strip()
        }
    )


def _trusted_examples(
    examples: list[HumanClipExample],
) -> list[HumanClipExample]:
    return [
        example
        for example in examples
        if evaluate_human_example_learning_quality(example).decision
        is LearningQuality.TRUSTED
    ]


def _alignment_geometry(
    example: HumanClipExample,
) -> tuple[float | None, float | None, list[float]]:
    if not example.alignment_segments:
        if example.source_start is None or example.source_end is None:
            return None, None, []
        span = example.source_end - example.source_start
        return span, span, []

    ordered = sorted(
        example.alignment_segments,
        key=lambda item: (item.clip_start, item.clip_end),
    )
    selected = sum(
        segment.source_end - segment.source_start
        for segment in ordered
    )
    source_span = ordered[-1].source_end - ordered[0].source_start
    gaps = [
        max(0.0, right.source_start - left.source_end)
        for left, right in zip(ordered, ordered[1:], strict=False)
        if right.source_start > left.source_end
    ]
    return selected, source_span, gaps


def build_editorial_learning_cards(
    examples: list[HumanClipExample],
) -> list[EditorialLearningCard]:
    cards: list[EditorialLearningCard] = []
    for example in _trusted_examples(examples):
        selected, source_span, gaps = _alignment_geometry(example)
        weak_refs = [
            f"{example.id}:weak:{index}"
            for index, _negative in enumerate(example.weak_negatives)
        ]
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
                selected_source_seconds=selected,
                source_span_seconds=source_span,
                alignment_segments=len(example.alignment_segments),
                removed_source_gap_seconds=[
                    round(value, 3) for value in gaps
                ],
                weak_negative_refs=weak_refs,
                weak_negative_reasons=[
                    negative.reason for negative in example.weak_negatives
                ],
                weak_negative_excerpts=[
                    negative.transcript_excerpt[:500]
                    for negative in example.weak_negatives
                ],
            )
        )
    return sorted(cards, key=lambda item: item.example_id)


def _card_payload(card: EditorialLearningCard) -> dict[str, Any]:
    return card.model_dump(mode="json")


def _partial_messages(
    cards: list[EditorialLearningCard],
) -> list[dict[str, str]]:
    schema = {
        "global_principles": ["reusable editorial selection principle"],
        "rejection_principles": ["what adjacent/omitted material teaches us not to select"],
        "boundary_principles": ["how a human editor decides starts, ends and internal cuts"],
        "patterns": [
            {
                "name": "short reusable name",
                "mechanism": "why this type of moment becomes a clip",
                "observable_cues": ["signals visible in transcript/content"],
                "must_have": ["evidence required before using this pattern"],
                "anti_patterns": ["near-misses that should not qualify"],
                "boundary_strategy": ["how to choose clip boundaries"],
                "context_requirement": "low|medium|high",
                "typical_duration_min_seconds": 20,
                "typical_duration_max_seconds": 120,
                "cut_style": "single_span|multi_cut|mixed",
                "support_example_ids": ["example id"],
                "weak_negative_refs": ["example:weak:index"],
                "confidence": 0.0,
            }
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are learning the editorial selection behavior of human VTuber "
                "clip editors from aligned human-made clips. Infer reusable mechanisms "
                "from the evidence. Do not summarize topics or member identities. "
                "Learn what makes a moment worth clipping, what editors remove, and "
                "how they bound a clip. Weak negatives are contextual evidence about "
                "omitted material, not universal negative labels. Never invent support "
                "example IDs. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Learning cards:\n"
                + json.dumps(
                    [_card_payload(card) for card in cards],
                    ensure_ascii=False,
                )
                + "\n\nInduce reusable editorial patterns. Prefer mechanisms that "
                "generalize across examples. A pattern may cite one example at this "
                "partial stage, but do not claim corpus-wide maturity. Return:\n"
                + json.dumps(schema, ensure_ascii=False)
            ),
        },
    ]


def _consolidation_messages(
    cards: list[EditorialLearningCard],
    partials: list[dict[str, Any]],
) -> list[dict[str, str]]:
    schema = {
        "global_principles": ["deduplicated principle"],
        "rejection_principles": ["deduplicated rejection principle"],
        "boundary_principles": ["deduplicated boundary principle"],
        "patterns": [
            {
                "name": "canonical reusable name",
                "mechanism": "cross-example mechanism",
                "observable_cues": ["signals"],
                "must_have": ["required evidence"],
                "anti_patterns": ["near-miss"],
                "boundary_strategy": ["boundary rule"],
                "context_requirement": "low|medium|high",
                "typical_duration_min_seconds": 20,
                "typical_duration_max_seconds": 120,
                "cut_style": "single_span|multi_cut|mixed",
                "support_example_ids": ["only ids actually supporting it"],
                "weak_negative_refs": ["only valid refs"],
                "confidence": 0.0,
            }
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are consolidating a learned editorial model from multiple batches "
                "of human-made VTuber clips. Merge duplicate mechanisms, separate "
                "genuinely different mechanisms, and keep provenance exact. The final "
                "model must describe selection behavior, not topics or personalities. "
                "Do not invent example IDs or weak-negative references. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Canonical learning cards for provenance:\n"
                + json.dumps(
                    [_card_payload(card) for card in cards],
                    ensure_ascii=False,
                )
                + "\n\nPartial inductions:\n"
                + json.dumps(partials, ensure_ascii=False)
                + "\n\nReturn one consolidated profile payload:\n"
                + json.dumps(schema, ensure_ascii=False)
            ),
        },
    ]


def _normalize_context_requirement(value: Any) -> str:
    cleaned = str(value or "medium").strip().lower()
    return cleaned if cleaned in {"low", "medium", "high"} else "medium"


def _normalize_cut_style(value: Any) -> str:
    cleaned = str(value or "mixed").strip().lower()
    return cleaned if cleaned in {"single_span", "multi_cut", "mixed"} else "mixed"


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, number)


def _pattern_from_payload(
    payload: dict[str, Any],
    *,
    card_by_id: dict[str, EditorialLearningCard],
    valid_weak_refs: set[str],
) -> EditorialPattern | None:
    name = str(payload.get("name", "")).strip()
    mechanism = str(payload.get("mechanism", "")).strip()
    if not name or not mechanism:
        return None

    support_ids = [
        item
        for item in _clean_strings(payload.get("support_example_ids", []))
        if item in card_by_id
    ]
    support_ids = sorted(set(support_ids))
    support_streams = sorted(
        {
            card_by_id[item].stream_id
            for item in support_ids
            if card_by_id[item].stream_id
        }
    )
    weak_refs = [
        item
        for item in _clean_strings(payload.get("weak_negative_refs", []))
        if item in valid_weak_refs
    ]

    maturity = (
        PatternMaturity.CORE
        if len(support_ids) >= MIN_CORE_SUPPORT_EXAMPLES
        and len(support_streams) >= MIN_CORE_SUPPORT_STREAMS
        else PatternMaturity.EMERGING
    )
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    minimum = _number_or_none(payload.get("typical_duration_min_seconds"))
    maximum = _number_or_none(payload.get("typical_duration_max_seconds"))
    if minimum is not None and maximum is not None and maximum < minimum:
        minimum, maximum = maximum, minimum

    return EditorialPattern(
        id=stable_id(
            "epat",
            EDITORIAL_PROFILE_VERSION,
            name.lower(),
            mechanism.lower(),
        ),
        name=name,
        mechanism=mechanism,
        observable_cues=_clean_strings(payload.get("observable_cues", [])),
        must_have=_clean_strings(payload.get("must_have", [])),
        anti_patterns=_clean_strings(payload.get("anti_patterns", [])),
        boundary_strategy=_clean_strings(payload.get("boundary_strategy", [])),
        context_requirement=_normalize_context_requirement(
            payload.get("context_requirement")
        ),
        typical_duration_min_seconds=minimum,
        typical_duration_max_seconds=maximum,
        cut_style=_normalize_cut_style(payload.get("cut_style")),
        support_example_ids=support_ids,
        support_stream_ids=support_streams,
        weak_negative_refs=sorted(set(weak_refs)),
        confidence=confidence,
        maturity=maturity,
    )


def _compute_readiness(
    cards: list[EditorialLearningCard],
    patterns: list[EditorialPattern],
) -> LearningReadiness:
    trusted = len(cards)
    streams = len({card.stream_id for card in cards if card.stream_id})
    core = [
        pattern
        for pattern in patterns
        if pattern.maturity is PatternMaturity.CORE
    ]
    covered = {
        example_id
        for pattern in core
        for example_id in pattern.support_example_ids
    }
    coverage = len(covered) / trusted if trusted else 0.0

    reasons: list[str] = []
    if trusted < MIN_READY_TRUSTED_EXAMPLES:
        reasons.append(
            f"trusted_examples {trusted} < {MIN_READY_TRUSTED_EXAMPLES}"
        )
    if streams < MIN_READY_UNIQUE_STREAMS:
        reasons.append(
            f"unique_streams {streams} < {MIN_READY_UNIQUE_STREAMS}"
        )
    if len(core) < MIN_READY_CORE_PATTERNS:
        reasons.append(
            f"core_patterns {len(core)} < {MIN_READY_CORE_PATTERNS}"
        )
    if coverage < MIN_READY_CORE_COVERAGE:
        reasons.append(
            "core_coverage_ratio "
            f"{coverage:.3f} < {MIN_READY_CORE_COVERAGE:.3f}"
        )

    return LearningReadiness(
        ready=not reasons,
        trusted_examples=trusted,
        unique_streams=streams,
        core_patterns=len(core),
        core_coverage_ratio=coverage,
        reasons=reasons,
    )


async def induce_editorial_profile(
    llm: LLMProvider,
    examples: list[HumanClipExample],
    *,
    model: str,
    reasoning_effort: str = "high",
    batch_size: int = 12,
    stage_cache: StageArtifactCache | None = None,
    cache_provider: str | None = None,
) -> EditorialProfile:
    if batch_size < 2:
        raise ValueError("editorial profile batch_size must be >= 2")

    cards = build_editorial_learning_cards(examples)
    if not cards:
        raise ValueError("no trusted human examples are available for profile learning")

    corpus_fingerprint = make_input_fingerprint(
        [_card_payload(card) for card in cards]
    )

    partials: list[dict[str, Any]] = []
    for batch_index, start in enumerate(range(0, len(cards), batch_size)):
        batch = cards[start : start + batch_size]
        batch_fingerprint = make_input_fingerprint(
            [_card_payload(card) for card in batch]
        )
        payload = None
        run = None
        if stage_cache is not None:
            payload = stage_cache.reusable_json(
                stage="editorial_profile_partial",
                subject_type="human_clip_corpus",
                subject_id=f"{corpus_fingerprint}:{batch_index}",
                input_fingerprint=batch_fingerprint,
                stage_version="1",
                provider=cache_provider,
                model=model,
                prompt_version=PARTIAL_PROMPT_VERSION,
            )
        if payload is None:
            if stage_cache is not None:
                run = stage_cache.start(
                    stage="editorial_profile_partial",
                    subject_type="human_clip_corpus",
                    subject_id=f"{corpus_fingerprint}:{batch_index}",
                    input_fingerprint=batch_fingerprint,
                    stage_version="1",
                    provider=cache_provider,
                    model=model,
                    prompt_version=PARTIAL_PROMPT_VERSION,
                )
            try:
                payload = await llm.chat_json(
                    _partial_messages(batch),
                    model=model,
                    temperature=0.0,
                    max_tokens=4500,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                if stage_cache is not None and run is not None:
                    stage_cache.fail(
                        run,
                        status=StageStatus.FAILED_RETRYABLE,
                        error=exc,
                    )
                raise
            if stage_cache is not None and run is not None:
                stage_cache.succeed_json(
                    run,
                    payload,
                    kind="editorial_profile_partial",
                )
        partials.append(payload)

    consolidation_input = make_input_fingerprint(
        {
            "corpus_fingerprint": corpus_fingerprint,
            "partials": partials,
        }
    )
    consolidated = None
    run = None
    if stage_cache is not None:
        consolidated = stage_cache.reusable_json(
            stage="editorial_profile_consolidate",
            subject_type="human_clip_corpus",
            subject_id=corpus_fingerprint,
            input_fingerprint=consolidation_input,
            stage_version="1",
            provider=cache_provider,
            model=model,
            prompt_version=CONSOLIDATE_PROMPT_VERSION,
        )
    if consolidated is None:
        if stage_cache is not None:
            run = stage_cache.start(
                stage="editorial_profile_consolidate",
                subject_type="human_clip_corpus",
                subject_id=corpus_fingerprint,
                input_fingerprint=consolidation_input,
                stage_version="1",
                provider=cache_provider,
                model=model,
                prompt_version=CONSOLIDATE_PROMPT_VERSION,
            )
        try:
            consolidated = await llm.chat_json(
                _consolidation_messages(cards, partials),
                model=model,
                temperature=0.0,
                max_tokens=6500,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:
            if stage_cache is not None and run is not None:
                stage_cache.fail(
                    run,
                    status=StageStatus.FAILED_RETRYABLE,
                    error=exc,
                )
            raise
        if stage_cache is not None and run is not None:
            stage_cache.succeed_json(
                run,
                consolidated,
                kind="editorial_profile",
            )

    card_by_id = {card.example_id: card for card in cards}
    valid_weak_refs = {
        ref for card in cards for ref in card.weak_negative_refs
    }
    raw_patterns = consolidated.get("patterns", [])
    patterns: list[EditorialPattern] = []
    if isinstance(raw_patterns, list):
        for item in raw_patterns:
            if not isinstance(item, dict):
                continue
            pattern = _pattern_from_payload(
                item,
                card_by_id=card_by_id,
                valid_weak_refs=valid_weak_refs,
            )
            if pattern is not None:
                patterns.append(pattern)

    patterns.sort(
        key=lambda item: (
            item.maturity is not PatternMaturity.CORE,
            -len(item.support_example_ids),
            -item.confidence,
            item.name,
        )
    )
    readiness = _compute_readiness(cards, patterns)
    streams = {card.stream_id for card in cards if card.stream_id}

    return EditorialProfile(
        model=model,
        corpus_fingerprint=corpus_fingerprint,
        trusted_example_ids=[card.example_id for card in cards],
        trusted_examples=len(cards),
        unique_streams=len(streams),
        global_principles=_clean_strings(
            consolidated.get("global_principles", [])
        ),
        rejection_principles=_clean_strings(
            consolidated.get("rejection_principles", [])
        ),
        boundary_principles=_clean_strings(
            consolidated.get("boundary_principles", [])
        ),
        patterns=patterns,
        readiness=readiness,
    )


def save_editorial_profile(profile: EditorialProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_editorial_profile(path: Path) -> EditorialProfile:
    return EditorialProfile.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def editorial_profile_prompt_payload(
    profile: EditorialProfile,
    *,
    include_emerging: bool = False,
) -> dict[str, Any]:
    patterns = [
        pattern
        for pattern in profile.patterns
        if include_emerging or pattern.maturity is PatternMaturity.CORE
    ]
    return {
        "version": profile.version,
        "global_principles": profile.global_principles,
        "rejection_principles": profile.rejection_principles,
        "boundary_principles": profile.boundary_principles,
        "patterns": [
            {
                "id": pattern.id,
                "name": pattern.name,
                "mechanism": pattern.mechanism,
                "observable_cues": pattern.observable_cues,
                "must_have": pattern.must_have,
                "anti_patterns": pattern.anti_patterns,
                "boundary_strategy": pattern.boundary_strategy,
                "context_requirement": pattern.context_requirement,
                "typical_duration_min_seconds": (
                    pattern.typical_duration_min_seconds
                ),
                "typical_duration_max_seconds": (
                    pattern.typical_duration_max_seconds
                ),
                "cut_style": pattern.cut_style,
                "confidence": pattern.confidence,
            }
            for pattern in patterns
        ],
    }
