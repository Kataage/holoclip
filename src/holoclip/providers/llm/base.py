from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class LLMProvider(Protocol):
    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
    ) -> str: ...

    async def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]: ...
