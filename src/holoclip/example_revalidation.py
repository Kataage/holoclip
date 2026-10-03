from __future__ import annotations

from pydantic import BaseModel, Field

from holoclip.alignment import ALIGNMENT_METHOD, align_clip_to_source
from holoclip.examples import HumanClipExample
from holoclip.models import TranscriptSegment


class HumanExampleRealignmentResult(BaseModel):
    example: HumanClipExample
    accepted: bool
    alignment_changed: bool
    alignment_confidence: float = Field(ge=0.0, le=1.0)
    alignment_segments: int = Field(ge=0)


def _alignment_signature(example: HumanClipExample) -> tuple:
    return (
        example.source_start,
        example.source_end,
        example.alignment_confidence,
        example.alignment_method,
        example.alignment_matched_anchors,
        example.alignment_total_anchors,
        tuple(
            (
                item.clip_start,
                item.clip_end,
                item.source_start,
                item.source_end,
                item.confidence,
                item.matched_anchors,
            )
            for item in example.alignment_segments
        ),
    )


def realign_human_clip_example(
    example: HumanClipExample,
    clip_segments: list[TranscriptSegment],
    source_segments: list[TranscriptSegment],
    *,
    min_confidence: float = 0.55,
) -> HumanExampleRealignmentResult:
    try:
        alignment = align_clip_to_source(clip_segments, source_segments)
    except ValueError:
        alignment = None

    accepted = (
        alignment is not None
        and alignment.confidence >= min_confidence
    )

    data = example.model_dump(mode="python")
    data.update(
        {
            "source_start": alignment.source_start if accepted else None,
            "source_end": alignment.source_end if accepted else None,
            "alignment_confidence": alignment.confidence if alignment is not None else 0.0,
            "alignment_method": ALIGNMENT_METHOD,
            "alignment_matched_anchors": (
                alignment.matched_anchors if alignment is not None else 0
            ),
            "alignment_total_anchors": (
                alignment.total_anchors if alignment is not None else 0
            ),
            "alignment_segments": (
                [
                    {
                        "clip_start": segment.clip_start,
                        "clip_end": segment.clip_end,
                        "source_start": segment.source_start,
                        "source_end": segment.source_end,
                        "confidence": segment.confidence,
                        "matched_anchors": segment.matched_anchors,
                    }
                    for segment in alignment.segments
                ]
                if accepted and alignment is not None
                else []
            ),
            # Existing weak negatives were derived from the old alignment.
            # Keep them for audit history, but make them ineligible for retrieval
            # until coverage-aware regeneration succeeds.
            "weak_negative_method": None,
        }
    )
    updated = HumanClipExample.model_validate(data)

    return HumanExampleRealignmentResult(
        example=updated,
        accepted=accepted,
        alignment_changed=(
            _alignment_signature(example) != _alignment_signature(updated)
        ),
        alignment_confidence=(
            alignment.confidence if alignment is not None else 0.0
        ),
        alignment_segments=(
            len(alignment.segments)
            if accepted and alignment is not None
            else 0
        ),
    )
