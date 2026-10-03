from __future__ import annotations

import tempfile
from pathlib import Path

from collections.abc import Callable

from holoclip.config import GroqTranscriptionSettings
from holoclip.execution import (
    StageArtifactCache,
    StageStatus,
    file_input_identity,
    make_input_fingerprint,
)
from holoclip.media import split_media_audio
from holoclip.providers.runtime import MeshTranscriber
from holoclip.models import TranscriptSegment
from holoclip.providers.transcription.groq import (
    GroqRateLimitDeferred,
)


async def transcribe_media(
    media_path: Path,
    settings: GroqTranscriptionSettings,
    *,
    stage_cache: StageArtifactCache | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[TranscriptSegment]:
    with tempfile.TemporaryDirectory(prefix="holoclip-audio-") as temp_dir:
        chunks = split_media_audio(
            media_path,
            Path(temp_dir),
            chunk_seconds=settings.chunk_seconds,
        )

        segments: list[TranscriptSegment] = []
        media_identity = file_input_identity(media_path)

        async with MeshTranscriber(settings) as transcriber:
            for index, chunk in enumerate(chunks):
                input_fingerprint = make_input_fingerprint(
                    {
                        "media": media_identity,
                        "chunk_index": index,
                        "offset_seconds": chunk.offset_seconds,
                        "chunk_seconds": settings.chunk_seconds,
                        "language": settings.language,
                    }
                )
                subject_id = f"{media_identity['path']}#chunk-{index:04d}"

                cached = None
                if stage_cache is not None:
                    cached = stage_cache.reusable_json(
                        stage="transcription_chunk",
                        subject_type="local_media_chunk",
                        subject_id=subject_id,
                        input_fingerprint=input_fingerprint,
                        stage_version="mesh-1",
                        provider="capability_mesh",
                        model="asr_segment_timestamps",
                    )

                if cached is not None:
                    batch = [
                        TranscriptSegment.model_validate(item)
                        for item in cached
                    ]
                    if progress is not None:
                        progress(
                            f"transcription chunk {index + 1}/{len(chunks)}: reused"
                        )
                    segments.extend(batch)
                    continue

                run = None
                if stage_cache is not None:
                    run = stage_cache.start(
                        stage="transcription_chunk",
                        subject_type="local_media_chunk",
                        subject_id=subject_id,
                        input_fingerprint=input_fingerprint,
                        stage_version="mesh-1",
                        provider="capability_mesh",
                        model="asr_segment_timestamps",
                    )

                try:
                    batch = await transcriber.transcribe_file(
                        chunk.path,
                        offset_seconds=chunk.offset_seconds,
                    )
                except GroqRateLimitDeferred as exc:
                    if stage_cache is not None and run is not None:
                        stage_cache.fail(
                            run,
                            status=StageStatus.BLOCKED_QUOTA,
                            error=exc,
                            retry_after=exc.retry_after,
                        )
                    raise
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
                        [item.model_dump(mode="json") for item in batch],
                        kind="transcript_chunk",
                    )

                if progress is not None:
                    progress(
                        f"transcription chunk {index + 1}/{len(chunks)}: completed"
                    )
                segments.extend(batch)

    return sorted(segments, key=lambda item: (item.start, item.end))
