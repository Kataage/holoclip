from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class TranscriptSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> TranscriptSegment:
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        return self


class TranscriptChunk(BaseModel):
    id: str
    start: float
    end: float
    text: str
    segment_indices: list[int]


class CandidateWarning(StrEnum):
    CONTEXT_RISK = "context_risk"
    UNCLEAR_SPEAKER = "unclear_speaker"
    WEAK_PAYOFF = "weak_payoff"
    POSSIBLE_MISINTERPRETATION = "possible_misinterpretation"


class ClipCandidate(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    working_title: str
    setup: str
    development: str
    payoff: str
    why_interesting: str
    traits: list[str] = Field(default_factory=list)
    context_requirement: str = "medium"
    confidence: float = Field(default=0.5, ge=0, le=1)
    source_chunk_ids: list[str] = Field(default_factory=list)
    warnings: list[CandidateWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range(self) -> ClipCandidate:
        if self.end <= self.start:
            raise ValueError("candidate end must be greater than start")
        return self


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    MAYBE = "maybe"
    REJECT = "reject"


class CandidateFeedback(BaseModel):
    candidate_id: str
    decision: ReviewDecision
    reason: str | None = None
