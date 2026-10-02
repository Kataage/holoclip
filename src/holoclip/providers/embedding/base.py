from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
