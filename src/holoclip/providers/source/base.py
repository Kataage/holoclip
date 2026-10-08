from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class AcquisitionProfile(StrEnum):
    TRAINING_AUDIO = "training_audio"
    EDITING_VIDEO = "editing_video"


class SourceMetadata(BaseModel):
    provider: str
    url: str
    webpage_url: str | None = None
    external_id: str
    title: str
    channel: str | None = None
    channel_id: str | None = None
    description: str | None = None
    duration: float | None = Field(default=None, ge=0)
    upload_date: str | None = None
    live_status: str | None = None
    availability: str | None = None
    extractor: str | None = None
    provider_version: str | None = None
    provider_channel: str | None = None
    js_runtime: str | None = None


class SourceCandidate(BaseModel):
    url: str
    score: float = Field(ge=0, le=1)
    context: str | None = None
    reason: str


class AcquiredSource(BaseModel):
    metadata: SourceMetadata
    profile: AcquisitionProfile
    media_path: Path
    info_json_path: Path | None = None
    reused: bool = False

class AcquiredVideoRange(BaseModel):
    """A bounded download, whose absolute source timestamp is only a request."""
    metadata: SourceMetadata
    media_path: Path
    requested_start: float = Field(ge=0)
    requested_end: float = Field(gt=0)
    reused: bool = False

    @property
    def requested_duration(self) -> float:
        return self.requested_end - self.requested_start


class SourceProvider(Protocol):
    def probe(self, url: str) -> SourceMetadata: ...

    def source_candidates(
        self,
        metadata: SourceMetadata,
    ) -> list[SourceCandidate]: ...

    def acquire(
        self,
        url: str,
        output_root: Path,
        *,
        profile: AcquisitionProfile = AcquisitionProfile.TRAINING_AUDIO,
    ) -> AcquiredSource: ...

    def acquire_video_range(
        self, url: str, output_root: Path, *, start: float, end: float
    ) -> AcquiredVideoRange: ...
