from __future__ import annotations

import httpx
import pytest

from holoclip.config import NimSettings
from holoclip.providers.llm.nvidia_nim import (
    NvidiaNimAuthorizationError,
    NvidiaNimClient,
)


@pytest.mark.asyncio
async def test_chat_classifies_403_authorization_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                403,
                json={"status": 403, "title": "Forbidden", "detail": "Authorization failed"},
            )
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = NimSettings(
        api_key="test-key",
        rpm=60000,
        max_retries=0,
    )

    async with NvidiaNimClient(settings, client=client) as nim:
        with pytest.raises(NvidiaNimAuthorizationError) as error:
            await nim.chat([{"role": "user", "content": "hello"}])

    await client.aclose()

    assert error.value.status_code == 403
    assert error.value.detail == "Authorization failed"


@pytest.mark.asyncio
async def test_models_can_work_while_inference_is_forbidden() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "z-ai/glm-5.3"}, {"id": "z-ai/glm-5.3-flash"}]},
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                403,
                json={"detail": "Authorization failed"},
            )
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = NimSettings(
        api_key="test-key",
        rpm=60000,
        max_retries=0,
    )

    async with NvidiaNimClient(settings, client=client) as nim:
        models = await nim.list_models()
        assert len(models["data"]) == 2

        with pytest.raises(NvidiaNimAuthorizationError):
            await nim.chat([{"role": "user", "content": "hello"}])

    await client.aclose()
