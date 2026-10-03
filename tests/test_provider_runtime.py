import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.execution import StageArtifactCache, StageStatus
from holoclip.providers.cost import Capability
from holoclip.providers.mesh import ExecutionMode, MeshDeferred, ProviderState
from holoclip.providers.runtime import (
    MeshEmbeddingClient, MeshLLMClient, MeshTranscriber, ProviderRuntime, RoutingSettings,
)
from holoclip.repository import SQLiteCanonicalRepository
from test_provider_mesh import qualification


def runtime_fixture(tmp_path, guard, client, rows):
    repository = SQLiteCanonicalRepository(tmp_path / "stages.db")
    cache = StageArtifactCache(repository, tmp_path / "artifacts")
    return ProviderRuntime(guard=guard, keys={"groq": "test-key", "nvidia": "test-key"},
        client=client, cache=cache, qualifications=rows), repository


def text_scores(guard):
    return [qualification(guard.registry[identity], cap, ExecutionMode.TEXT)
        for identity in ("nvidia:z-ai/glm-5.3-flash", "groq:qwen/qwen3.8-27b")
        for cap in (Capability.REASONING_STRONG, Capability.REASONING_FAST, Capability.JSON)]


async def test_production_without_gold_never_sends_inference(tmp_path, free_cost_guard):
    methods = []
    def handler(request):
        methods.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": "z-ai/glm-5.3-flash"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, [])
        try:
            with pytest.raises(MeshDeferred):
                async with MeshLLMClient(runtime=runtime):
                    pytest.fail("Unqualified production context entered")
        finally:
            await runtime.aclose()
            repository.close()
    assert "POST" not in methods


async def test_real_http_boundary_timeout_falls_to_other_qualified_vendor(tmp_path, free_cost_guard):
    calls = []
    def handler(request):
        assert request.url.host in {"api.groq.com", "integrate.api.nvidia.com"}
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "z-ai/glm-5.3-flash"}, {"id": "qwen/qwen3.8-27b"}]})
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ReadTimeout("fault injected", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ranked":[]}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, text_scores(free_cost_guard))
        try:
            async with MeshLLMClient(RoutingSettings(), runtime=runtime) as llm:
                assert await llm.chat_json([{"role": "user", "content": "Select"}]) == {"ranked": []}
                assert llm.last_provenance["diagnostic_only"] is False
        finally:
            repository.close()
    assert len(calls) == 2 and calls[0] != calls[1]


async def test_quota_cannot_select_low_quality_alternate(tmp_path, free_cost_guard):
    calls = []
    rows = text_scores(free_cost_guard)
    first_provider = "groq"
    rows = [r.model_copy(update={"quality_score": .79}) if r.provider != first_provider else r for r in rows]
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "z-ai/glm-5.3-flash"}, {"id": "qwen/qwen3.8-27b"}]})
        calls.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "3600"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, rows)
        try:
            async with MeshLLMClient(runtime=runtime) as llm:
                with pytest.raises(MeshDeferred) as failure:
                    await llm.chat_json([{"role": "user", "content": "Select"}])
                assert failure.value.quota
        finally:
            repository.close()
    assert len(calls) == 1 and "api.groq.com" in calls[0]


async def test_partial_json_qualification_is_not_enough(tmp_path, free_cost_guard):
    def handler(request):
        pytest.fail("Missing required reasoning qualification must block before POST")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        entry = free_cost_guard.registry["groq:qwen/qwen3.8-27b"]
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c,
            [qualification(entry, Capability.JSON, ExecutionMode.TEXT)])
        try:
            with pytest.raises(MeshDeferred):
                await MeshLLMClient(runtime=runtime).chat_json([{"role": "user", "content": "Select"}])
        finally:
            await runtime.aclose()
            repository.close()


async def test_embedding_space_cannot_silently_change(tmp_path, free_cost_guard):
    calls = []
    old = free_cost_guard.registry["nvidia:nvidia/nemotron-3-embed-1b"]
    future = old.model_copy(update={"model": "fixture-embed-v2"})
    free_cost_guard.registry[future.identity] = future
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1., 2.]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        rows = [qualification(old, Capability.EMBEDDING, ExecutionMode.EMBEDDING)]
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, rows)
        embed = MeshEmbeddingClient(runtime=runtime)
        try:
            assert embed.model == "mesh:" + old.identity
            assert await embed.embed_query("Japanese structural query") == [1., 2.]
            runtime.router.qualifications.append(qualification(future, Capability.EMBEDDING, ExecutionMode.EMBEDDING))
            runtime.router.state.record(old.identity, ProviderState.MODEL_DEPRECATED)
            with pytest.raises(MeshDeferred):
                await embed.embed_query("A different query")
        finally:
            await runtime.aclose()
            repository.close()
    assert len(calls) == 1


async def test_word_timestamps_require_separate_qualification(tmp_path, monkeypatch, free_cost_guard):
    import holoclip.providers.runtime as module
    monkeypatch.setattr(module, "probe_video", lambda _: {"format": {"duration": "1"}})
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"test audio")
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "text": "test"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        entry = free_cost_guard.registry["groq:whisper-large-v3"]
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c,
            [qualification(entry, Capability.ASR_SEGMENTS, ExecutionMode.AUDIO)])
        transcriber = MeshTranscriber(runtime=runtime)
        try:
            assert (await transcriber.transcribe_file(audio, offset_seconds=10))[0].start == 10
            with pytest.raises(MeshDeferred):
                await transcriber.transcribe_observation(audio, words=True)
        finally:
            await runtime.aclose()
            repository.close()
    assert len(calls) == 1


async def test_private_media_blocked_despite_quality(tmp_path, monkeypatch, free_cost_guard):
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS")
    def handler(_):
        pytest.fail("Private input cannot reach HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, text_scores(free_cost_guard))
        try:
            with pytest.raises(MeshDeferred):
                await MeshLLMClient(runtime=runtime).chat_json([{"role": "user", "content": "private"}])
        finally:
            await runtime.aclose()
            repository.close()


async def test_outer_timeout_marks_stage_cancelled(tmp_path, free_cost_guard):
    async def handler(request):
        await asyncio.sleep(10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        runtime, repository = runtime_fixture(tmp_path, free_cost_guard, c, text_scores(free_cost_guard))
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.05):
                    await MeshLLMClient(runtime=runtime).chat_json([{"role": "user", "content": "test"}])
            rows = repository._connection.execute("SELECT payload FROM stage_runs").fetchall()
            assert any(json.loads(r[0])["status"] == StageStatus.CANCELLED.value for r in rows)
        finally:
            await runtime.aclose()
            repository.close()


def test_cli_doctor_no_inference_and_machine_readable_block(monkeypatch, tmp_path, free_cost_guard):
    class Runtime:
        def __init__(self):
            self.router = type("Router", (), {"candidates": lambda *_: []})()
        async def startup(self):
            pass
        async def aclose(self):
            pass
        def contract(self, *_):
            return None
    monkeypatch.setattr(cli, "ProviderRuntime", Runtime)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 3
    output = json.loads(result.stdout)
    assert output["paid_fallback_allowed"] is False and output["ready"] is False


def test_cli_handles_mesh_block_without_vendor_upgrade_hint(monkeypatch):
    monkeypatch.setattr(cli, "ProviderRuntime", lambda: (_ for _ in ()).throw(MeshDeferred(["Need reviewed gold"])))
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 3
    assert "BLOCKED_DEPENDENCY" in result.output and "ZERO_COST_ONLY" in result.output
