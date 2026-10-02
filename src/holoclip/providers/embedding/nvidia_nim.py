from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from typing import Any, Self

import httpx

from holoclip.config import NimEmbeddingSettings
from holoclip.providers.embedding.base import EmbeddingProviderError


class NvidiaEmbeddingAuthorizationError(EmbeddingProviderError):
    pass


class NvidiaEmbeddingRateLimitDeferred(EmbeddingProviderError):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        message = "NVIDIA embedding endpoint rate/quota limited"
        if retry_after is not None:
            message += f"; retry after approximately {retry_after:.0f} seconds"
        super().__init__(message)


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


class NvidiaNimEmbeddingClient:
    def __init__(
        self,
        settings: NimEmbeddingSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._rate_gate = _RateGate(settings.rpm)

    @property
    def model(self) -> str:
        return self.settings.model

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _embed(
        self,
        texts: Sequence[str],
        *,
        input_type: str,
    ) -> list[list[float]]:
        clean = [text.strip() for text in texts]
        if not clean or any(not text for text in clean):
            raise ValueError("embedding input text must not be empty")

        output: list[list[float]] = []
        for start in range(0, len(clean), self.settings.batch_size):
            batch = clean[start : start + self.settings.batch_size]
            await self._rate_gate.wait()

            payload = {
                "input": batch,
                "model": self.settings.model,
                "input_type": input_type,
                "encoding_format": "float",
                "truncate": "END",
            }

            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = await self._client.post(
                        f"{self.settings.base_url}/embeddings",
                        headers={
                            "Authorization": f"Bearer {self.settings.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt >= self.settings.max_retries:
                        raise EmbeddingProviderError(
                            f"NVIDIA embedding network request failed: {exc}"
                        ) from exc
                    await asyncio.sleep(min(30.0, (2**attempt) + random.random()))
                    continue

                if response.status_code < 400:
                    raw: Any = response.json()
                    data = raw.get("data") if isinstance(raw, dict) else None
                    if not isinstance(data, list):
                        raise TypeError("embedding response data must be a list")
                    ordered = sorted(
                        data,
                        key=lambda item: int(item.get("index", 0))
                        if isinstance(item, dict)
                        else 0,
                    )
                    for item in ordered:
                        if not isinstance(item, dict):
                            raise TypeError("embedding response item must be an object")
                        vector = item.get("embedding")
                        if not isinstance(vector, list) or not vector:
                            raise TypeError("embedding vector must be a non-empty list")
                        output.append([float(value) for value in vector])
                    break

                if response.status_code in {401, 403}:
                    raise NvidiaEmbeddingAuthorizationError(
                        f"NVIDIA embedding authorization failed with HTTP "
                        f"{response.status_code}"
                    )

                if response.status_code == 429:
                    raw_retry = response.headers.get("Retry-After")
                    try:
                        retry_after = float(raw_retry) if raw_retry else None
                    except ValueError:
                        retry_after = None
                    if retry_after is not None and retry_after > 120:
                        raise NvidiaEmbeddingRateLimitDeferred(retry_after)
                    if attempt >= self.settings.max_retries:
                        raise NvidiaEmbeddingRateLimitDeferred(retry_after)
                    delay = retry_after if retry_after is not None else float(2**attempt)
                    await asyncio.sleep(min(120.0, delay + random.random()))
                    continue

                if 500 <= response.status_code < 600 and attempt < self.settings.max_retries:
                    await asyncio.sleep(min(30.0, (2**attempt) + random.random()))
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise EmbeddingProviderError(
                        f"NVIDIA embedding HTTP {response.status_code}"
                    ) from exc

        if len(output) != len(clean):
            raise RuntimeError(
                f"embedding response count mismatch: expected {len(clean)}, got {len(output)}"
            )
        return output

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, input_type="passage")

    async def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, input_type="query")

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]
