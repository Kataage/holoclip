from __future__ import annotations

import httpx
import pytest

from holoclip.config import NimSettings
from holoclip.providers.llm.nvidia_nim import (
    NvidiaNimAuthorizationError,
    NvidiaNimClient,
    NvidiaNimTimeoutError,
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


@pytest.mark.asyncio
async def test_chat_sends_reasoning_effort() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"candidates\":[]}"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = NimSettings(api_key="test-key", rpm=60000, max_retries=0)

    async with NvidiaNimClient(settings, client=client) as nim:
        await nim.chat_json(
            [{"role": "user", "content": "hello"}],
            model="z-ai/glm-5.3-flash",
            reasoning_effort="low",
        )

    await client.aclose()

    assert captured["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_timeout_becomes_concise_nim_timeout_error() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow endpoint", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = NimSettings(
        api_key="test-key",
        rpm=60000,
        max_retries=6,
        timeout_seconds=1,
    )

    async with NvidiaNimClient(settings, client=client) as nim:
        with pytest.raises(NvidiaNimTimeoutError) as error:
            await nim.chat(
                [{"role": "user", "content": "hello"}],
                model="z-ai/glm-5.3-flash",
                reasoning_effort="low",
            )

    await client.aclose()

    assert attempts == 3
    assert error.value.model == "z-ai/glm-5.3-flash"
    assert error.value.attempts == 3
