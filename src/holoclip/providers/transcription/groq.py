from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

import httpx

from holoclip.config import GroqTranscriptionSettings
from holoclip.providers.cost import CostGuard, QuotaBlocked
from holoclip.models import TranscriptSegment


class GroqRateLimitDeferred(QuotaBlocked):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        message = "Groq free-tier rate/quota limit reached"
        if retry_after is not None:
            message += f"; retry after approximately {retry_after:.0f} seconds"
        RuntimeError.__init__(self, message)


class _RateGate:
    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / rpm
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                await asyncio.sleep(self._next_at - now)
            now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval


def _parse_segments(
    payload: dict[str, Any],
    *,
    offset_seconds: float,
) -> list[TranscriptSegment]:
    raw_segments = payload.get("segments")
    result: list[TranscriptSegment] = []

    if isinstance(raw_segments, Sequence) and not isinstance(raw_segments, (str, bytes)):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item["start"]) + offset_seconds
                end = float(item["end"]) + offset_seconds
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("text", "")).strip()
            if not text or end <= start:
                continue
            result.append(
                TranscriptSegment(
                    start=start,
                    end=end,
                    text=text,
                )
            )

    if result:
        return result

    text = str(payload.get("text", "")).strip()
    try:
        duration = float(payload.get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    if text and duration > 0:
        return [
            TranscriptSegment(
                start=offset_seconds,
                end=offset_seconds + duration,
                text=text,
            )
        ]
    return []


class GroqWhisperTranscriber:
    def __init__(
        self,
        settings: GroqTranscriptionSettings,
        *,
        client: httpx.AsyncClient | None = None,
        cost_guard: CostGuard | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._rate_gate = _RateGate(settings.rpm)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._cost_guard = cost_guard or CostGuard.from_env()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe_file(
        self,
        path: Path,
        *,
        offset_seconds: float = 0.0,
    ) -> list[TranscriptSegment]:
        if not path.exists():
            raise FileNotFoundError(path)

        for attempt in range(self.settings.max_retries + 1):
            await self._rate_gate.wait()

            try:
                with path.open("rb") as handle:
                    response = await self._cost_guard.post(
                        self._client, provider="groq", model=self.settings.model,
                        base_url=self.settings.base_url, key=self.settings.api_key,
                        operation="audio/transcriptions",
                        estimate={"requests": 1, "audio_seconds": self.settings.chunk_seconds,
                                  "uploaded_bytes": path.stat().st_size},
                        headers={"Authorization": f"Bearer {self.settings.api_key}"},
                        files={"file": (path.name, handle, "audio/mpeg")},
                        data={
                            "model": self.settings.model,
                            "language": self.settings.language,
                            "response_format": "verbose_json",
                            "temperature": "0",
                            "timestamp_granularities[]": "segment",
                        },
                    )
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt >= self.settings.max_retries:
                    raise
                await asyncio.sleep(min(60.0, (2**attempt) + random.random()))
                continue

            if response.status_code < 400:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("Groq transcription response must be a JSON object")
                return _parse_segments(payload, offset_seconds=offset_seconds)

            if response.status_code == 429:
                retry_after_raw = response.headers.get("Retry-After")
                try:
                    retry_after = (
                        float(retry_after_raw) if retry_after_raw is not None else None
                    )
                except ValueError:
                    retry_after = None

                raise GroqRateLimitDeferred(retry_after)

            if 500 <= response.status_code < 600:
                if attempt >= self.settings.max_retries:
                    response.raise_for_status()
                await asyncio.sleep(min(60.0, (2**attempt) + random.random()))
                continue

            response.raise_for_status()

        raise RuntimeError("Groq transcription request loop exited unexpectedly")
