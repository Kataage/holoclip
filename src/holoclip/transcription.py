from __future__ import annotations

import tempfile
from pathlib import Path

from holoclip.config import GroqTranscriptionSettings
from holoclip.media import split_media_audio
from holoclip.models import TranscriptSegment
from holoclip.providers.transcription.groq import GroqWhisperTranscriber


async def transcribe_media(
    media_path: Path,
    settings: GroqTranscriptionSettings,
) -> list[TranscriptSegment]:
    with tempfile.TemporaryDirectory(prefix="holoclip-audio-") as temp_dir:
        chunks = split_media_audio(
            media_path,
            Path(temp_dir),
            chunk_seconds=settings.chunk_seconds,
        )

        segments: list[TranscriptSegment] = []
        async with GroqWhisperTranscriber(settings) as transcriber:
            for chunk in chunks:
                segments.extend(
                    await transcriber.transcribe_file(
                        chunk.path,
                        offset_seconds=chunk.offset_seconds,
                    )
                )

    return sorted(segments, key=lambda item: (item.start, item.end))
