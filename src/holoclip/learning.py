from __future__ import annotations

from typing import Any

from holoclip.examples import HumanClipExample, WeakNegativeExample
from holoclip.models import TranscriptSegment
from holoclip.providers.llm.base import LLMProvider


def _excerpt(
    segments: list[TranscriptSegment],
    *,
    start: float,
    end: float,
    max_chars: int = 6000,
) -> str:
    lines: list[str] = []
    for segment in segments:
        if segment.end <= start or segment.start >= end:
            continue
        lines.append(f"[{segment.start:.1f}] {segment.text}")
    return "\n".join(lines)[:max_chars]


def build_adjacent_weak_negatives(
    example: HumanClipExample,
    source_segments: list[TranscriptSegment],
    *,
    window_seconds: float = 90.0,
    gap_seconds: float = 15.0,
) -> list[WeakNegativeExample]:
    if example.source_start is None or example.source_end is None:
        return []
    if not source_segments:
        return []

    source_end = max(segment.end for segment in source_segments)
    windows: list[tuple[float, float, str]] = []

    selected_ranges = (
        sorted(
            [
                (segment.source_start, segment.source_end)
                for segment in example.alignment_segments
            ],
            key=lambda item: item[0],
        )
        if example.alignment_segments
        else [(example.source_start, example.source_end)]
    )

    first_start = selected_ranges[0][0]
    last_end = selected_ranges[-1][1]

    before_end = max(0.0, first_start - gap_seconds)
    before_start = max(0.0, before_end - window_seconds)
    if before_end > before_start:
        windows.append((before_start, before_end, "adjacent_unselected_before"))

    for left, right in zip(selected_ranges, selected_ranges[1:], strict=False):
        gap_start = left[1] + gap_seconds
        gap_end = right[0] - gap_seconds
        if gap_end <= gap_start:
            continue
        window_end = min(gap_end, gap_start + window_seconds)
        windows.append((gap_start, window_end, "removed_between_selected_segments"))

    after_start = min(source_end, last_end + gap_seconds)
    after_end = min(source_end, after_start + window_seconds)
    if after_end > after_start:
        windows.append((after_start, after_end, "adjacent_unselected_after"))

    negatives: list[WeakNegativeExample] = []
    for start, end, reason in windows:
        excerpt = _excerpt(source_segments, start=start, end=end)
        if not excerpt.strip():
            continue
        negatives.append(
            WeakNegativeExample(
                start=start,
                end=end,
                transcript_excerpt=excerpt,
                reason=reason,
            )
        )
    return negatives


def _aligned_source_context(
    example: HumanClipExample,
    source_segments: list[TranscriptSegment],
) -> str:
    if example.alignment_segments:
        parts: list[str] = []
        for index, segment in enumerate(example.alignment_segments, start=1):
            excerpt = _excerpt(
                source_segments,
                start=max(0.0, segment.source_start - 30.0),
                end=segment.source_end + 30.0,
                max_chars=3500,
            )
            parts.append(
                f"--- aligned source segment {index} "
                f"{segment.source_start:.1f}-{segment.source_end:.1f} ---\n{excerpt}"
            )
        return "\n".join(parts)[:9000]

    if example.source_start is None or example.source_end is None:
        return ""
    return _excerpt(
        source_segments,
        start=max(0.0, example.source_start - 45.0),
        end=example.source_end + 45.0,
        max_chars=9000,
    )


def _structure_messages(
    example: HumanClipExample,
    source_context: str,
) -> list[dict[str, str]]:
    clip_text = example.transcript_excerpt or ""
    return [
        {
            "role": "system",
            "content": (
                "You are analyzing why a human clip editor selected a moment from a long "
                "Japanese VTuber stream. Extract the underlying editorial structure, not "
                "copywriting. Do not invent events that are absent from the transcript. "
                "Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Analyze this human-made clip as a weak-supervision example.\n\n"
                f"Clip title: {example.clip_title}\n"
                f"Clip transcript:\n{clip_text}\n\n"
                f"Aligned source context:\n{source_context}\n\n"
                "Return one JSON object with exactly these fields:\n"
                '{'
                '"setup":"concise setup",'
                '"development":"concise development",'
                '"payoff":"concise payoff or resolution",'
                '"why_selected":"why a human editor likely selected this event",'
                '"traits":["short structural tags"],'
                '"members":["named participants only when supported"],'
                '"topics":["short topic labels"],'
                '"relationship_context":"relationship dynamic when supported, otherwise empty",'
                '"context_requirement":"low|medium|high"'
                '}\n'
                "Traits should describe mechanisms such as unexpected_confession, "
                "character_gap, contradiction, escalation, callback, relationship_dynamics, "
                "setup_payoff, failure_after_confidence, or reveal. "
                "Prefer abstract reusable mechanisms over phrases copied from the clip."
            ),
        },
    ]


async def enrich_human_clip_example(
    llm: LLMProvider,
    example: HumanClipExample,
    source_segments: list[TranscriptSegment],
    *,
    model: str,
    reasoning_effort: str = "high",
    weak_negative_window_seconds: float = 90.0,
) -> HumanClipExample:
    if example.source_start is None or example.source_end is None:
        raise ValueError("example must be source-aligned before enrichment")

    source_context = _aligned_source_context(example, source_segments)
    payload = await llm.chat_json(
        _structure_messages(example, source_context),
        model=model,
        temperature=0.1,
        max_tokens=1800,
        reasoning_effort=reasoning_effort,
    )

    def strings(name: str) -> list[str]:
        raw: Any = payload.get(name, [])
        if not isinstance(raw, list):
            return []
        return sorted({str(value).strip() for value in raw if str(value).strip()})

    context_requirement = str(payload.get("context_requirement", "medium")).strip().lower()
    if context_requirement not in {"low", "medium", "high"}:
        context_requirement = "medium"

    updated = example.model_copy(deep=True)
    updated.setup = str(payload.get("setup", "")).strip()
    updated.development = str(payload.get("development", "")).strip()
    updated.payoff = str(payload.get("payoff", "")).strip()
    updated.why_selected = str(payload.get("why_selected", "")).strip()
    updated.traits = strings("traits")
    updated.members = strings("members")
    updated.topics = strings("topics")
    relationship_context = str(payload.get("relationship_context", "")).strip()
    updated.relationship_context = relationship_context or None
    updated.context_requirement = context_requirement
    updated.weak_negatives = build_adjacent_weak_negatives(
        updated,
        source_segments,
        window_seconds=weak_negative_window_seconds,
    )
    updated.structure_method = "glm-structure-v1"
    updated.structure_model = model
    return updated
