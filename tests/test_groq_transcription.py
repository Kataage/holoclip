from pathlib import Path

import httpx
import pytest

from holoclip.config import GroqTranscriptionSettings
from holoclip.providers.transcription.groq import (
    GroqRateLimitDeferred,
    GroqWhisperTranscriber,
)


@pytest.mark.asyncio
async def test_transcription_offsets_segment_timestamps(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"fake-audio")

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        assert request.url.path.endswith("/audio/transcriptions")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "text": "こんにちは",
                "segments": [
                    {"start": 1.5, "end": 3.0, "text": "こんにちは"},
                    {"start": 3.1, "end": 5.0, "text": "今日は雑談です"},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = GroqTranscriptionSettings(
        api_key="test-key",
        rpm=60000,
        max_retries=0,
    )

    async with GroqWhisperTranscriber(settings, client=client) as transcriber:
        result = await transcriber.transcribe_file(audio, offset_seconds=900)

    await client.aclose()

    assert result[0].start == 901.5
    assert result[0].end == 903.0
    assert result[1].start == 903.1
    assert result[1].text == "今日は雑談です"


@pytest.mark.asyncio
async def test_long_rate_limit_is_deferred_without_waiting(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"fake-audio")

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(429, headers={"Retry-After": "3600"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = GroqTranscriptionSettings(
        api_key="test-key",
        rpm=60000,
        max_retries=6,
    )

    async with GroqWhisperTranscriber(settings, client=client) as transcriber:
        with pytest.raises(GroqRateLimitDeferred) as error:
            await transcriber.transcribe_file(audio)

    await client.aclose()

    assert error.value.retry_after == 3600
