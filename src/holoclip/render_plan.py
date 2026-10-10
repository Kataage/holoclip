"""Deterministic, local-only multi-source ClipTimeline to MP4 preview renderer.

Not a publishing path or editing-quality certification. No network/yt-dlp/LLM,
shell commands, silent media downloads, task registration or file replacement.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from holoclip.domain import (
    AccessClass, ClipTimeline, SourceAsset, SourceAvailability, stable_id,
)

VERSION = "holoclip-local-render-v1"
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
VIDEO_FPS = 30
MAX_SEGMENTS = 32
MAX_SECONDS = 1800.0
MAX_SOURCE_BYTES = 50 * 1024**3
_ALLOWED_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm"})


class LocalRenderSource(BaseModel):
    """Explicit human/policy-controlled permission for *local preview only*."""
    asset: SourceAsset
    path: Path
    local_render_approved: bool = False


class RenderSourceSnapshot(BaseModel):
    asset_id: str
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    has_video: bool
    has_audio: bool


class RenderPlan(BaseModel):
    version: str = VERSION
    id: str
    timeline: ClipTimeline
    sources: list[RenderSourceSnapshot]
    output: Path
    video_width: int = VIDEO_WIDTH
    video_height: int = VIDEO_HEIGHT
    video_fps: int = VIDEO_FPS
    expected_duration: float = Field(gt=0)
    production_ready: bool = False
    ready: bool = False

    @model_validator(mode="after")
    def check_static_contract(self):
        if self.version != VERSION or self.ready or self.production_ready:
            raise ValueError("LOCAL_RENDER_UNTRUSTED_PLAN")
        if (self.video_width != VIDEO_WIDTH
                or self.video_height != VIDEO_HEIGHT
                or self.video_fps != VIDEO_FPS):
            raise ValueError("LOCAL_RENDER_UNSUPPORTED_PROFILE")
        return self


def _no_links(path: Path) -> Path:
    path = path.absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("LOCAL_RENDER_LINKED_PATH")
    return path


def _source_file(path: Path) -> Path:
    path = _no_links(path)
    if not path.is_file() or path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise ValueError("LOCAL_RENDER_SOURCE_NOT_LOCAL_MEDIA")
    stat = path.stat()
    if not 0 < stat.st_size <= MAX_SOURCE_BYTES:
        raise ValueError("LOCAL_RENDER_SOURCE_SIZE_UNSAFE")
    return path


def _target_file(path: Path, sources: list[Path]) -> Path:
    path = _no_links(path)
    if path.suffix.lower() != ".mp4" or not path.parent.is_dir() or path.exists():
        raise ValueError("LOCAL_RENDER_OUTPUT_UNSAFE_OR_EXISTS")
    if path in sources:
        raise ValueError("LOCAL_RENDER_OUTPUT_IS_SOURCE")
    return path


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while block := handle.read(2 * 1024 * 1024):
            total += len(block)
            if total > MAX_SOURCE_BYTES:
                raise ValueError("LOCAL_RENDER_SOURCE_SIZE_UNSAFE")
            digest.update(block)
    return digest.hexdigest(), total


def _call(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_RENDER_MEDIA_PROCESS_UNAVAILABLE") from exc


def _probe(path: Path) -> tuple[float, bool, bool]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ValueError("LOCAL_RENDER_FFPROBE_UNAVAILABLE")
    result = _call([
        executable, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-i", str(path), "-show_entries",
        "format=duration:stream=codec_type", "-of", "json",
    ], timeout=30)
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise ValueError("LOCAL_RENDER_MEDIA_PROBE_FAILED")
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        streams = payload["streams"]
        video = any(s.get("codec_type") == "video" for s in streams)
        audio = any(s.get("codec_type") == "audio" for s in streams)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration not finite")
        if not (video and audio):
            raise ValueError("missing audio/video stream")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("LOCAL_RENDER_AV_STREAMS_UNVERIFIED") from exc
    return duration, video, audio


def compile_local_render_plan(
    timeline: ClipTimeline,
    sources: list[LocalRenderSource],
    output: Path,
    *,
    video_width: int = VIDEO_WIDTH,
    video_height: int = VIDEO_HEIGHT,
) -> RenderPlan:
    """Read-only plan compilation against explicitly provided local media."""
    # Re-validate model_copy(update=...) instances too.
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    if (not 1 <= len(timeline.segments) <= MAX_SEGMENTS
            or not 0 < timeline.estimated_duration <= MAX_SECONDS
            or video_width != VIDEO_WIDTH or video_height != VIDEO_HEIGHT):
        raise ValueError("LOCAL_RENDER_TIMELINE_BOUNDS")
    used = {segment.source_asset_id for segment in timeline.segments}
    if len(sources) != len(used):
        raise ValueError("LOCAL_RENDER_SOURCE_MAP_INCOMPLETE")
    seen: set[str] = set()
    checked: list[RenderSourceSnapshot] = []
    paths: list[Path] = []
    for item in sources:
        asset = item.asset
        if asset.id not in used or asset.id in seen:
            raise ValueError("LOCAL_RENDER_SOURCE_MAP_INVALID")
        seen.add(asset.id)
        if (item.local_render_approved is not True
                or asset.availability != SourceAvailability.AVAILABLE
                or asset.access_class not in {
                    AccessClass.LOCAL, AccessClass.PUBLIC, AccessClass.AUTHORIZED,
                }):
            raise ValueError("LOCAL_RENDER_PERMISSION_UNVERIFIED")
        path = _source_file(item.path)
        if path in paths:
            raise ValueError("LOCAL_RENDER_SHARED_INPUT_UNVERIFIED")
        digest, size = _hash(path)
        duration, video, audio = _probe(path)
        checked.append(RenderSourceSnapshot(
            asset_id=asset.id, path=path, sha256=digest, size_bytes=size,
            duration_seconds=duration, has_video=video, has_audio=audio,
        ))
        paths.append(path)
    for segment in timeline.segments:
        found = next(s for s in checked if s.asset_id == segment.source_asset_id)
        if (not math.isfinite(segment.source_start)
                or not math.isfinite(segment.source_end)
                or segment.source_end > found.duration_seconds + 0.001
                or segment.source_start < 0 or segment.source_end <= segment.source_start):
            raise ValueError("LOCAL_RENDER_RANGE_OUTSIDE_MEASURED_MEDIA")
    target = _target_file(output, paths)
    # The render identity includes complete canonical timeline and media bytes,
    # and does not inherit arbitrary LLM planning artifacts as renderer flags.
    identity = stable_id(
        "localrender", VERSION, timeline.model_dump(mode="json"),
        sorted((source.asset_id, source.sha256, source.duration_seconds)
               for source in checked),
        VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS,
        length=32,
    )
    return RenderPlan(
        id=identity, timeline=timeline, sources=checked, output=target,
        expected_duration=timeline.estimated_duration,
    )


def _ffmpeg_command(plan: RenderPlan, target: Path) -> list[str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("LOCAL_RENDER_FFMPEG_UNAVAILABLE")
    inputs: list[str] = []
    source_index = {}
    for index, src in enumerate(plan.sources):
        source_index[src.asset_id] = index
        inputs.extend(["-protocol_whitelist", "file,pipe", "-i", str(src.path)])
    filters: list[str] = []
    labels: list[str] = []
    for index, seg in enumerate(sorted(plan.timeline.segments, key=lambda s: s.output_order)):
        k = source_index[seg.source_asset_id]
        start = f"{seg.source_start:.6f}"
        end = f"{seg.source_end:.6f}"
        filters.append(
            f"[{k}:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
            f"fps={VIDEO_FPS},scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,format=yuv420p[v{index}]"
        )
        filters.append(
            f"[{k}:a:0]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            f"[a{index}]"
        )
        labels.extend([f"[v{index}]", f"[a{index}]"])
    filters.append(
        "".join(labels) + f"concat=n={len(labels)//2}:v=1:a=1[outv][outa]"
    )
    return [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-n", *inputs, "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-map_metadata", "-1", str(target),
    ]


def render_local_preview(plan: RenderPlan) -> dict[str, object]:
    """Render, probe QA, then atomically publish only to a *new* local MP4.

    Hardlinks avoid time-of-check/time-of-use overwrites on Windows and Linux.
    Output is strictly a local technical preview, never a qualified publication.
    """
    plan = RenderPlan.model_validate(plan.model_dump(mode="json"))
    # Do not bypass explicit permissions at execution: validate the original
    # fixed identity, source bytes and duration against the compiled snapshot.
    for src in plan.sources:
        path = _source_file(src.path)
        digest, size = _hash(path)
        if digest != src.sha256 or size != src.size_bytes:
            raise ValueError("LOCAL_RENDER_SOURCE_CHANGED")
        measured, video, audio = _probe(path)
        if (abs(measured - src.duration_seconds) > 0.001
                or not video or not audio):
            raise ValueError("LOCAL_RENDER_SOURCE_PROBE_CHANGED")
    # Revalidate timeline and source ranges to prevent bypass via model_copy.
    timeline = ClipTimeline.model_validate(plan.timeline.model_dump(mode="json"))
    if (len(timeline.segments) > MAX_SEGMENTS
            or timeline.estimated_duration > MAX_SECONDS
            or abs(timeline.estimated_duration - plan.expected_duration) > 0.01):
        raise ValueError("LOCAL_RENDER_PLAN_CHANGED")
    ids = {item.asset_id for item in plan.sources}
    if len(ids) != len(plan.sources) or ids != {
        s.source_asset_id for s in timeline.segments
    }:
        raise ValueError("LOCAL_RENDER_PLAN_CHANGED")
    for segment in timeline.segments:
        found = next(s for s in plan.sources if s.asset_id == segment.source_asset_id)
        if segment.source_end > found.duration_seconds + 0.001:
            raise ValueError("LOCAL_RENDER_PLAN_CHANGED")
    expected_id = stable_id(
        "localrender", VERSION, timeline.model_dump(mode="json"),
        sorted((src.asset_id, src.sha256, src.duration_seconds)
               for src in plan.sources),
        VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS,
        length=32,
    )
    if expected_id != plan.id:
        raise ValueError("LOCAL_RENDER_PLAN_FINGERPRINT_CHANGED")
    output = _target_file(plan.output, [s.path for s in plan.sources])
    # No output file is created unless QA succeeds.
    with tempfile.TemporaryDirectory(prefix=".holoclip-render-", dir=output.parent) as tmp:
        temporary = Path(tmp) / "preview.mp4"
        argv = _ffmpeg_command(plan, temporary)
        timeout = min(3600.0, max(120.0, plan.expected_duration * 12.0))
        proc = _call(argv, timeout=timeout)
        if proc.returncode != 0 or not temporary.is_file():
            raise ValueError("LOCAL_RENDER_FFMPEG_FAILED")
        # Guard against source mutation while FFmpeg was decoding it.
        for src in plan.sources:
            digest, size = _hash(src.path)
            if (digest, size) != (src.sha256, src.size_bytes):
                raise ValueError("LOCAL_RENDER_SOURCE_CHANGED_DURING_EXECUTION")
        duration, video, audio = _probe(temporary)
        if (not video or not audio
                or abs(duration - plan.expected_duration) > max(0.5, 2 / VIDEO_FPS)):
            raise ValueError("LOCAL_RENDER_BASIC_QA_FAILED")
        result_sha, result_size = _hash(temporary)
        try:
            # Atomic no-clobber, also rejects a competing creation of the output.
            os.link(temporary, output)
        except (FileExistsError, OSError) as exc:
            raise ValueError("LOCAL_RENDER_OUTPUT_COMMIT_FAILED") from exc
    return {
        "status": "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
        "plan_id": plan.id,
        "output": str(output),
        "output_sha256": result_sha,
        "output_size_bytes": result_size,
        "measured_duration": duration,
        "expected_duration": plan.expected_duration,
        "source_count": len(plan.sources),
        "segment_count": len(plan.timeline.segments),
        "youtube_contacted": False,
        "publication_performed": False,
        "human_editorial_qa_completed": False,
        "ready": False,
        "production_ready": False,
    }
