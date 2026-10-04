from __future__ import annotations

import httpx
import pytest

from holoclip.config import NimEmbeddingSettings
from holoclip.providers.embedding.nvidia_nim import NvidiaNimEmbeddingClient


pytestmark = pytest.mark.usefixtures("free_cost_guard")


@pytest.mark.asyncio
async def test_embedding_client_uses_passage_and_query_input_types() -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads((await request.aread()).decode("utf-8"))
        seen.append(payload)
        vectors = []
        for index, _text in enumerate(payload["input"]):
            vectors.append({"index": index, "embedding": [float(index + 1), 0.5]})
        return httpx.Response(200, json={"data": vectors})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = NimEmbeddingSettings(
        api_key="test-key",
        rpm=60000,
        batch_size=16,
        max_retries=0,
    )

    async with NvidiaNimEmbeddingClient(settings, client=client) as embedding:
        passages = await embedding.embed_passages(["a", "b"])
        query = await embedding.embed_query("q")

    await client.aclose()

    assert passages == [[1.0, 0.5], [2.0, 0.5]]
    assert query == [1.0, 0.5]
    assert seen[0]["input_type"] == "passage"
    assert seen[1]["input_type"] == "query"
    assert all(item["model"] == "nvidia/nemotron-3-embed-1b" for item in seen)
    assert all(item["truncate"] == "NONE" for item in seen)
