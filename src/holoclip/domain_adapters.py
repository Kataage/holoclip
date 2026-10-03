from __future__ import annotations

from holoclip.alignment import ClipSourceAlignment
from holoclip.domain import (
    CanonicalTranscriptSegment,
    ClipTimeline,
    SourceAsset,
    SourceMoment,
    TimelinePurpose,
    TimelineSegment,
    make_clip_timeline_id,
    make_source_asset_id,
    make_source_moment_id,
    make_timeline_segment_id,
    make_transcript_segment_id,
)
from holoclip.examples import HumanClipExample
from holoclip.models import ClipCandidate, TranscriptSegment


def local_source_asset(
    *,
    external_id: str,
    title: str | None = None,
    canonical_url: str | None = None,
) -> SourceAsset:
    source_id = make_source_asset_id("local", external_id)
    return SourceAsset(
        id=source_id,
        platform="local",
        external_id=external_id,
        canonical_url=canonical_url,
        title=title,
        access_class="local",
        source_type="local_media",
        availability="available",
    )


def canonicalize_transcript_segments(
    source_asset_id: str,
    segments: list[TranscriptSegment],
) -> list[CanonicalTranscriptSegment]:
    return [
        CanonicalTranscriptSegment(
            id=make_transcript_segment_id(
                source_asset_id,
                segment.start,
                segment.end,
                segment.text,
            ),
            source_asset_id=source_asset_id,
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker=segment.speaker,
        )
        for segment in segments
    ]


def candidate_to_source_moment(
    source_asset_id: str,
    candidate: ClipCandidate,
    *,
    transcript_segment_ids: list[str] | None = None,
) -> SourceMoment:
    summary = candidate.why_interesting or candidate.working_title
    return SourceMoment(
        id=make_source_moment_id(
            source_asset_id,
            candidate.start,
            candidate.end,
            summary,
        ),
        source_asset_id=source_asset_id,
        transcript_segment_ids=transcript_segment_ids or [],
        start=candidate.start,
        end=candidate.end,
        moment_type="legacy_clip_candidate",
        summary=summary,
        traits=list(candidate.traits),
        confidence=candidate.confidence,
    )


def candidate_to_clip_timeline(source_asset_id: str, candidate: ClipCandidate) -> ClipTimeline:
    plan = candidate.editorial_edit_plan
    ranges = [(s.source_start, s.source_end) for s in plan.spans] if plan else [(candidate.start, candidate.end)]
    segments = []
    for i, (start, end) in enumerate(ranges):
        try:
            purpose = TimelinePurpose(plan.spans[i].purpose) if plan else TimelinePurpose.OTHER
        except ValueError:
            purpose = TimelinePurpose.OTHER
        segments.append(TimelineSegment(id=make_timeline_segment_id(source_asset_id, start, end, i),
            source_asset_id=source_asset_id, source_start=start, source_end=end, output_order=i,
            purpose=purpose, confidence=candidate.confidence))
    return ClipTimeline(id=make_clip_timeline_id([s.id for s in segments]), segments=segments,
                        estimated_duration=sum(end - start for start, end in ranges))


def alignment_to_clip_timeline(
    source_asset_id: str,
    alignment: ClipSourceAlignment,
) -> ClipTimeline:
    timeline_segments: list[TimelineSegment] = []
    for index, segment in enumerate(alignment.segments):
        timeline_segments.append(
            TimelineSegment(
                id=make_timeline_segment_id(
                    source_asset_id,
                    segment.source_start,
                    segment.source_end,
                    index,
                ),
                source_asset_id=source_asset_id,
                source_start=segment.source_start,
                source_end=segment.source_end,
                output_order=index,
                purpose=TimelinePurpose.OTHER,
                confidence=segment.confidence,
            )
        )

    if not timeline_segments:
        timeline_segments = [
            TimelineSegment(
                id=make_timeline_segment_id(
                    source_asset_id,
                    alignment.source_start,
                    alignment.source_end,
                    0,
                ),
                source_asset_id=source_asset_id,
                source_start=alignment.source_start,
                source_end=alignment.source_end,
                output_order=0,
                confidence=alignment.confidence,
            )
        ]

    duration = sum(
        segment.source_end - segment.source_start for segment in timeline_segments
    )
    return ClipTimeline(
        id=make_clip_timeline_id([segment.id for segment in timeline_segments]),
        segments=timeline_segments,
        estimated_duration=duration,
    )


def human_example_to_clip_timeline(
    example: HumanClipExample,
    *,
    source_asset_id: str,
) -> ClipTimeline | None:
    if example.alignment_segments:
        segments = [
            TimelineSegment(
                id=make_timeline_segment_id(
                    source_asset_id,
                    segment.source_start,
                    segment.source_end,
                    index,
                ),
                source_asset_id=source_asset_id,
                source_start=segment.source_start,
                source_end=segment.source_end,
                output_order=index,
                confidence=segment.confidence,
            )
            for index, segment in enumerate(example.alignment_segments)
        ]
    elif example.source_start is not None and example.source_end is not None:
        segments = [
            TimelineSegment(
                id=make_timeline_segment_id(
                    source_asset_id,
                    example.source_start,
                    example.source_end,
                    0,
                ),
                source_asset_id=source_asset_id,
                source_start=example.source_start,
                source_end=example.source_end,
                output_order=0,
                confidence=example.alignment_confidence or 0.5,
            )
        ]
    else:
        return None

    duration = sum(segment.source_end - segment.source_start for segment in segments)
    return ClipTimeline(
        id=make_clip_timeline_id([segment.id for segment in segments]),
        segments=segments,
        estimated_duration=duration,
    )
