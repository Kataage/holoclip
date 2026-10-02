from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Sequence
from typing import Any, Self

import httpx

from holoclip.config import NimSettings


class _RateGate:
    """Conservative process-local requests-per-minute gate."""

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


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(value[start : end + 1])

    if not isinstance(parsed, dict):
        raise TypeError("LLM JSON response must be an object")
    return parsed


class NvidiaNimClient:
    def __init__(
        self,
        settings: NimSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._rate_gate = _RateGate(settings.rpm)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        payload = {
            "model": model or self.settings.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        async with self._semaphore:
            for attempt in range(self.settings.max_retries + 1):
                await self._rate_gate.wait()
                try:
                    response = await self._client.post(
                        f"{self.settings.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.settings.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt >= self.settings.max_retries:
                        raise
                    await asyncio.sleep(min(60.0, (2**attempt) + random.random()))
                    continue

                if response.status_code < 400:
                    data = response.json()
                    return data["choices"][0]["message"]["content"]

                retryable = response.status_code == 429 or 500 <= response.status_code < 600
                if retryable:
                    if attempt >= self.settings.max_retries:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else float(2**attempt)
                    except ValueError:
                        delay = float(2**attempt)
                    await asyncio.sleep(min(120.0, delay + random.random()))
                    continue

                response.raise_for_status()

        raise RuntimeError("NVIDIA NIM request loop exited unexpectedly")

    async def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        text = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _extract_json(text)
