"""VLM interpretation references measured IDs; geometry/time never enter its schema."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from holoclip.domain import EvidenceItem, EvidenceKind, stable_id
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import Capability
from holoclip.providers.mesh import CapabilityRouter, ExecutionMode, ModeRequirement, StageContract


class MeasuredFrame(BaseModel):
    id: str
    shot_id: str
    pts_seconds: float = Field(ge=0)
    locator: str
    checksum: str


class MeasuredVideoWindow(BaseModel):
    id: str
    source_asset_id: str
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    video_path: str
    media_sha256: str
    frames: tuple[MeasuredFrame, ...]
    contact_sheet_paths: tuple[str, ...]
    transcript: dict[str, str] = Field(default_factory=dict)
    measured_events: dict[str, dict] = Field(default_factory=dict)
    extraction_version: str

    @model_validator(mode="after")
    def valid(self):
        if self.source_end <= self.source_start or self.source_end - self.source_start > 120:
            raise ValueError("Video window must be bounded to 120 seconds")
        ids = [f.id for f in self.frames]
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError("Measured frame IDs must be present and unique")
        if any(f.pts_seconds >= self.source_end - self.source_start + 0.1 for f in self.frames):
            raise ValueError("Frame outside measured window")
        return self


class SemanticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    description: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    focus_candidates: tuple[str, ...] = ()
    read_text: str | None = None
    confidence: float = Field(ge=0, le=1)
    uncertainty: str | None = None


class SemanticObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_id: str
    events: tuple[SemanticEvent, ...]
    unknowns: tuple[str, ...] = ()


def validate_observation(payload: dict, window: MeasuredVideoWindow) -> dict:
    observation = SemanticObservation.model_validate(payload)
    if observation.window_id != window.id:
        raise ValueError("Invented media-window ID")
    frame_ids = {f.id for f in window.frames}
    allowed = frame_ids | {f.shot_id for f in window.frames} | set(window.transcript) | set(window.measured_events)
    for event in observation.events:
        if not set(event.evidence_ids) <= allowed:
            raise ValueError("Invented measurement IDs")
        if not set(event.evidence_ids) & (frame_ids | {f.shot_id for f in window.frames}):
            raise ValueError("Visual semantic event needs supplied visual evidence")
    return observation.model_dump(mode="json")


class VisualTargetProvider(Protocol):
    """Measurement/tracking owns exact regions; semantic focus strings are search hints."""
    def locate(self, window: MeasuredVideoWindow, semantic_target: str) -> list[dict]: ...


class VideoUnderstandingProvider:
    def __init__(self, router: CapabilityRouter, adapters: CloudAdapters):
        self.router = router
        self.adapters = adapters

    async def understand(self, window: MeasuredVideoWindow, *, diagnostic_identity=None,
                         diagnostic_mode=None, dimensions=frozenset({"japanese", "avatar", "game_ui", "reaction", "ocr"})):
        alternatives = tuple(ModeRequirement(mode=m, required={cap, Capability.JSON}) for m, cap in [
            (ExecutionMode.NATIVE, Capability.VIDEO_NATIVE),
            (ExecutionMode.MULTIFRAME, Capability.VIDEO_MULTIFRAME),
            (ExecutionMode.CONTACT_SHEET, Capability.VIDEO_MULTIFRAME),
            (ExecutionMode.FUSION, Capability.VIDEO_MULTIFRAME),
        ])
        contract = StageContract(stage="provider_benchmark_video" if diagnostic_identity else "video_semantics",
            alternatives=alternatives, dimensions=dimensions, public_data=True,
            quota_estimate={"requests": 1}, timeout_seconds=120)
        payload = {"measurement": window.model_dump(mode="json"), "prompt_version": "grounded-video-v1"}
        if diagnostic_identity:
            payload.update(diagnostic_identity=diagnostic_identity, diagnostic_mode=diagnostic_mode)

        async def invoke(route, _payload):
            frames = list(window.frames)
            if route.mode == ExecutionMode.NATIVE:
                groups = [(frames, [], Path(window.video_path))]
            elif route.mode == ExecutionMode.CONTACT_SHEET:
                groups = []
                for p in window.contact_sheet_paths:
                    ids = {r["frame_id"] for r in json.loads(Path(p).with_suffix(".json").read_text(encoding="utf-8"))}
                    groups.append(([f for f in frames if f.id in ids], [Path(p)], None))
            else:
                width = max(1, route.entry.max_images)
                if route.mode == ExecutionMode.FUSION:
                    width = 1  # Inspect individual images then fuse supported observations deterministically.
                groups = [(frames[i:i + width], [Path(f.locator) for f in frames[i:i + width]], None)
                          for i in range(0, len(frames), width)]
            if not groups:
                raise ValueError("Execution mode has no measured visual input")
            events, unknowns = [], []
            for selected, images, video in groups:
                context = {"window_id": window.id,
                    "frames": [{"id": f.id, "shot_id": f.shot_id} for f in selected],
                    "transcript": window.transcript, "measured_events": window.measured_events,
                    "mode": route.mode.value}
                prompt = (
                    "Inspect public Japanese Hololive footage and return only a JSON object matching "
                    "this schema: {window_id, events:[{kind,description,evidence_ids:[supplied IDs],"
                    "focus_candidates:[semantic subject descriptions],read_text:string|null,"
                    "confidence:0..1,uncertainty:string|null}],unknowns:[string]}. "
                    "Describe key event/payoff, important subject, avatar/facial reaction, game/UI, "
                    "small UI event, visual gag, object and source-switch meaning when supported. "
                    "Read Japanese on-screen text faithfully; uncertainty is allowed. Use measured IDs. "
                    "Never return timestamps, crop coordinates, bounding boxes or tracking paths. "
                    "Contact-sheet tile labels are measurement IDs, not scene text.\n" +
                    json.dumps(context, ensure_ascii=False))
                output = await self.adapters.call_json(route, prompt, images=images, video=video,
                    public=True, cache_identity={"window": window.media_sha256,
                        "duration_seconds": window.source_end - window.source_start,
                        "frame_ids": [f.id for f in selected]})
                # A batch may reference only the frames actually sent, plus their shots/context.
                local = window.model_copy(update={"frames": tuple(selected)})
                parsed = SemanticObservation.model_validate(validate_observation(output, local))
                events.extend(parsed.events)
                unknowns.extend(parsed.unknowns)
            unique = {e.model_dump_json(): e for e in events}
            return SemanticObservation(window_id=window.id, events=tuple(unique.values()),
                                       unknowns=tuple(sorted(set(unknowns)))).model_dump(mode="json")

        return await self.router.run(contract, payload, invoke,
            validate=lambda raw: validate_observation(raw, window), diagnostic=bool(diagnostic_identity))

    async def observe_timeline(self, timeline, windows: list[MeasuredVideoWindow]) -> dict:
        """Runtime bridge for prepared, bounded TimelinePlan/ClipTimeline ranges."""
        results = {}
        for window in windows:
            if not any(s.source_asset_id == window.source_asset_id and
                       s.source_start <= window.source_start < window.source_end <= s.source_end
                       for s in timeline.segments):
                raise ValueError("Measured video window is outside selected timeline")
            result = await self.understand(window)
            results[window.id] = {"observation": result, "evidence": [
                e.model_dump(mode="json") for e in semantic_evidence(window, result)]}
        return {"timeline_id": timeline.id, "windows": results}

    async def observe_training_pair(self, *, example_id: str, finished: MeasuredVideoWindow,
                                    aligned_sources: list[MeasuredVideoWindow]) -> dict:
        """Editing Learning bridge: preserve source and finished interpretations separately."""
        if not aligned_sources:
            raise ValueError("Editing supervision requires aligned source windows")
        finished_result = await self.understand(finished)
        sources = {}
        for source in aligned_sources:
            result = await self.understand(source)
            sources[source.id] = {"observation": result, "evidence": [
                e.model_dump(mode="json") for e in semantic_evidence(source, result)]}
        return {"example_id": example_id, "finished": finished_result, "aligned_sources": sources,
                "editing_profile_ready": False,
                "unknowns": ["measured_source_to_finished_transforms_required",
                             "editing_mechanisms_not_induced_by_video_provider"]}


def semantic_evidence(window: MeasuredVideoWindow, result: dict) -> list[EvidenceItem]:
    """Training/runtime share this bridge; no invented coordinates or exact event times."""
    raw = validate_observation(result["output"], window)
    if result["provenance"].get("diagnostic_only"):
        raise ValueError("Benchmark diagnostics cannot become production editing evidence")
    return [EvidenceItem(id=stable_id("vsem", window.id, event), source_asset_id=window.source_asset_id,
        kind=EvidenceKind.OTHER, start=window.source_start, end=window.source_end,
        payload={"semantic_event": event, "window_id": window.id, "provenance": result["provenance"],
                 "time_basis": "measured_window_only", "pixel_target": "NOT_MEASURED"},
        confidence=event["confidence"], producer="capability_video_semantics") for event in raw["events"]]
