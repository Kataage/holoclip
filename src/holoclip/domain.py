from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


def stable_id(namespace: str, *parts: object, length: int = 20) -> str:
    payload = json.dumps(
        [namespace, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{namespace}_{digest}"


class SourcePlatform(StrEnum):
    YOUTUBE = "youtube"
    LOCAL = "local"
    OTHER = "other"


class SourceType(StrEnum):
    STREAM = "stream"
    CLIP = "clip"
    VIDEO = "video"
    LOCAL_MEDIA = "local_media"


class AccessClass(StrEnum):
    PUBLIC = "public"
    AUTHORIZED = "authorized"
    LOCAL = "local"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class SourceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SourceAsset(BaseModel):
    id: str
    platform: SourcePlatform
    external_id: str
    canonical_url: str | None = None
    title: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    published_at: str | None = None
    duration: float | None = Field(default=None, gt=0)
    availability: SourceAvailability = SourceAvailability.UNKNOWN
    access_class: AccessClass = AccessClass.UNKNOWN
    source_type: SourceType = SourceType.VIDEO
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    content_fingerprint: str | None = None


def make_source_asset_id(platform: SourcePlatform | str, external_id: str) -> str:
    return stable_id("src", str(platform), external_id.strip())


class CanonicalTranscriptSegment(BaseModel):
    id: str
    source_asset_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)
    speaker: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    word_ids: list[str] = Field(default_factory=list)
    transcription_artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> CanonicalTranscriptSegment:
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        return self


def make_transcript_segment_id(
    source_asset_id: str,
    start: float,
    end: float,
    text: str,
) -> str:
    normalized = " ".join(text.split())
    return stable_id(
        "seg",
        source_asset_id,
        round(start, 3),
        round(end, 3),
        normalized,
    )


class EvidenceKind(StrEnum):
    TRANSCRIPT_SEMANTIC = "transcript_semantic"
    CHAT_BURST = "chat_burst"
    CHAT_REACTION = "chat_reaction"
    LAUGHTER = "laughter"
    SHOUT = "shout"
    SILENCE = "silence"
    PROSODY_SHIFT = "prosody_shift"
    GAME_EVENT = "game_event"
    SHOT_CHANGE = "shot_change"
    FACE_REACTION = "face_reaction"
    ON_SCREEN_TEXT = "on_screen_text"
    TOPIC_CHANGE = "topic_change"
    HISTORICAL_MATCH = "historical_match"
    OTHER = "other"


class EvidenceItem(BaseModel):
    id: str
    source_asset_id: str
    kind: EvidenceKind
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0, le=1)
    producer: str
    artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceItem:
        if self.end <= self.start:
            raise ValueError("evidence end must be greater than start")
        return self


class SourceMoment(BaseModel):
    id: str
    source_asset_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    transcript_segment_ids: list[str] = Field(default_factory=list)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    moment_type: str = "candidate"
    summary: str
    traits: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_range(self) -> SourceMoment:
        if self.end <= self.start:
            raise ValueError("moment end must be greater than start")
        return self


def make_source_moment_id(
    source_asset_id: str,
    start: float,
    end: float,
    summary: str,
) -> str:
    return stable_id(
        "mom",
        source_asset_id,
        round(start, 3),
        round(end, 3),
        " ".join(summary.split()),
    )


class TimelinePurpose(StrEnum):
    HOOK = "hook"
    SETUP = "setup"
    CONTEXT = "context"
    DEVELOPMENT = "development"
    CONTRAST = "contrast"
    CALLBACK = "callback"
    REACTION = "reaction"
    PAYOFF = "payoff"
    EPILOGUE = "epilogue"
    OTHER = "other"


class TimelineSegment(BaseModel):
    id: str
    source_asset_id: str
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    source_segment_ids: list[str] = Field(default_factory=list)
    output_order: int = Field(ge=0)
    purpose: TimelinePurpose = TimelinePurpose.OTHER
    confidence: float = Field(default=0.5, ge=0, le=1)
    transition_hint: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> TimelineSegment:
        if self.source_end <= self.source_start:
            raise ValueError("timeline source_end must be greater than source_start")
        return self


def make_timeline_segment_id(
    source_asset_id: str,
    source_start: float,
    source_end: float,
    output_order: int,
) -> str:
    return stable_id(
        "tls",
        source_asset_id,
        round(source_start, 3),
        round(source_end, 3),
        output_order,
    )


class ClipTimeline(BaseModel):
    id: str
    segments: list[TimelineSegment]
    estimated_duration: float = Field(ge=0)
    timeline_version: int = Field(default=1, ge=1)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_segments(self) -> ClipTimeline:
        if not self.segments:
            raise ValueError("clip timeline requires at least one segment")

        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("clip timeline segment IDs must be unique")

        orders = [segment.output_order for segment in self.segments]
        if len(orders) != len(set(orders)):
            raise ValueError("clip timeline output_order values must be unique")
        if sorted(orders) != list(range(len(orders))):
            raise ValueError("clip timeline output_order must be contiguous from zero")

        measured = sum(segment.source_end - segment.source_start for segment in self.segments)
        if abs(self.estimated_duration - measured) > 0.01:
            raise ValueError("estimated_duration must equal summed source segment durations")
        return self


def make_clip_timeline_id(segment_ids: list[str]) -> str:
    return stable_id("tl", segment_ids)


class EditorialArchetype(StrEnum):
    MICRO_MOMENT = "micro_moment"
    SINGLE_TOPIC_STORY = "single_topic_story"
    SAME_STREAM_MULTICUT = "same_stream_multicut"
    HISTORICAL_CALLBACK = "historical_callback"
    MULTI_MEMBER_REACTION = "multi_member_reaction"
    RELATIONSHIP_STORY = "relationship_story"
    CONTRAST_COMPARISON = "contrast_comparison"
    CHRONOLOGY_EVOLUTION = "chronology_evolution"
    EVENT_COMPILATION = "event_compilation"
    CONTEXTUAL_EXPLAINER = "contextual_explainer"
    EXPLORATORY = "exploratory"


class IdeaHorizon(StrEnum):
    STREAM_LOCAL = "stream_local"
    RECENT_CROSS_STREAM = "recent_cross_stream"
    HISTORICAL = "historical"
    PERIODIC = "periodic"
    EXPLORATORY = "exploratory"


class EditorialIdeaStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class DurationRange(BaseModel):
    minimum_seconds: float = Field(ge=0)
    maximum_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> DurationRange:
        if self.maximum_seconds < self.minimum_seconds:
            raise ValueError("maximum_seconds must be >= minimum_seconds")
        return self


class NarrativeBeat(BaseModel):
    purpose: TimelinePurpose
    source_moment_ids: list[str] = Field(default_factory=list)
    note: str = ""


class EditorialIdea(BaseModel):
    id: str
    archetype: EditorialArchetype
    thesis: str
    viewer_promise: str
    hook: str
    target_duration_range: DurationRange
    required_source_asset_ids: list[str] = Field(default_factory=list)
    optional_source_asset_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_moment_ids: list[str] = Field(default_factory=list)
    narrative_beats: list[NarrativeBeat] = Field(default_factory=list)
    example_support_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)
    novelty_reason: str = ""
    novelty_score: float = Field(default=0.5, ge=0, le=1)
    duplicate_risk: float = Field(default=0.0, ge=0, le=1)
    policy_risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    generation_horizon: IdeaHorizon = IdeaHorizon.STREAM_LOCAL
    status: EditorialIdeaStatus = EditorialIdeaStatus.PROPOSED

    @model_validator(mode="after")
    def validate_sources(self) -> EditorialIdea:
        required = set(self.required_source_asset_ids)
        optional = set(self.optional_source_asset_ids)
        if required & optional:
            raise ValueError("required and optional source assets must not overlap")
        return self


def make_editorial_idea_id(
    archetype: EditorialArchetype | str,
    source_moment_ids: list[str],
    thesis: str,
) -> str:
    return stable_id(
        "idea",
        str(archetype),
        sorted(source_moment_ids),
        " ".join(thesis.split()),
    )


class ReviewTargetType(StrEnum):
    SOURCE_MOMENT = "source_moment"
    EDITORIAL_IDEA = "editorial_idea"
    PREVIEW = "preview"
    PUBLISHED_VIDEO = "published_video"


class ReviewAction(StrEnum):
    ACCEPT = "accept"
    MAYBE = "maybe"
    REJECT = "reject"
    REVISE = "revise"
    PUBLISH = "publish"


class ReviewRecord(BaseModel):
    id: str
    target_type: ReviewTargetType
    target_id: str
    decision: ReviewAction
    reasons: list[str] = Field(default_factory=list)
    freeform_note: str | None = None
    created_at: str


def make_review_record_id(
    target_type: ReviewTargetType | str,
    target_id: str,
    created_at: str,
) -> str:
    return stable_id("review", str(target_type), target_id, created_at)
