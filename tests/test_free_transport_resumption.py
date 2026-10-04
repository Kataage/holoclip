from datetime import timedelta

import httpx
import pytest

from holoclip.provider_benchmark import summarize
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import Capability, CostPolicyError, reset_delay, request_scope
from holoclip.providers.mesh import CapabilityRouter, ExecutionMode, Route
from holoclip.video_semantics import MeasuredFrame, MeasuredVideoWindow, VideoUnderstandingProvider
from test_cost_policy import NOW
from test_provider_mesh import qualification


def test_retry_after_date_and_duration_reset_headers():
    assert reset_delay({"retry-after": "Sun, 04 Oct 2026 10:01:00 GMT"}, NOW) == 60
    assert reset_delay({"x-ratelimit-reset-tokens": "1m2.5s", "x-ratelimit-reset-requests": "5ms"}, NOW) == 62.5
    assert reset_delay({"x-ratelimit-reset-tokens": "garbage"}, NOW) is None


def test_next_daily_reset_does_not_inherently_require_new_attestation(free_cost_guard):
    entry = free_cost_guard.registry["groq:qwen/qwen3.8-27b"]
    free_cost_guard.clock = lambda: NOW + timedelta(days=1, seconds=1)
    assert free_cost_guard.authorize(entry.provider, entry.model, entry.base_url, "test-key", public=True)
    free_cost_guard.accounts = [a.model_copy(update={"evidence_ttl_seconds": 60}) for a in free_cost_guard.accounts]
    with pytest.raises(CostPolicyError, match="stale"):
        free_cost_guard.authorize(entry.provider, entry.model, entry.base_url, "test-key", public=True)


async def test_old_unknown_429_block_repaired_without_refunding_reservations(free_cost_guard):
    def handler(request):
        return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "2s"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await free_cost_guard.post(c, provider="groq", model="openai/gpt-oss-120b",
            base_url="https://api.groq.com/openai/v1", key="test-key", operation="chat/completions",
            estimate={"requests": 1}, public=True)
    with free_cost_guard.ledger.connect() as db:
        db.execute("UPDATE blocks SET until=?", (NOW.timestamp() + 86400,))
    free_cost_guard.ledger.reconcile_rate_resets(NOW + timedelta(seconds=3))
    with free_cost_guard.ledger.connect() as db:
        assert db.execute("SELECT until FROM blocks").fetchone()[0] == NOW.timestamp() + 2
        assert db.execute("SELECT amount FROM usage WHERE unit='requests'").fetchone()[0] == 1


async def test_accepted_202_resumes_with_get_without_second_post(free_cost_guard):
    request_id = "8afe13bd-e6d7-4fcb-9018-1e568c365ca4"
    methods = []
    def handler(request):
        methods.append(request.method)
        assert request.url.host == "integrate.api.nvidia.com"
        if request.method == "POST":
            assert request.headers["NVCF-POLL-SECONDS"] == "5"
            return httpx.Response(202, headers={"nvcf-reqid": request_id})
        assert request.url.path == "/v1/status/" + request_id
        if len(methods) == 2:
            raise httpx.ReadTimeout("interrupt poll", request=request)
        return httpx.Response(200, json={"usage": {"total_tokens": 5},
            "choices": [{"message": {"content": '{"ok":true}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["nvidia:z-ai/glm-5.3-flash"], mode=ExecutionMode.TEXT, qualification=())
        token = request_scope.set("accepted-job")
        try:
            with pytest.raises(httpx.ReadTimeout):
                await a.call_json(route, "test", public=True)
            b = CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=c)
            assert await b.call_json(route, "test", public=True) == {"ok": True}
        finally:
            request_scope.reset(token)
    assert methods == ["POST", "GET", "GET"]
    rows = free_cost_guard.ledger.telemetry("accepted-job")
    assert len(rows) == 1 and rows[0]["observed_usage"] == {"total_tokens": 5}
    assert rows[0]["poll_reads"][0]["status"] == 200


@pytest.mark.parametrize("poll_status", [302, 404])
async def test_status_redirect_or_expiry_never_reaches_partner(free_cost_guard, poll_status):
    methods = []
    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, headers={"nvcf-reqid": "8afe13bd-e6d7-4fcb-9018-1e568c365ca4"})
        assert request.url.host == "integrate.api.nvidia.com"
        return httpx.Response(poll_status, headers={"Location": "https://paid.partner.invalid"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["nvidia:z-ai/glm-5.3-flash"], mode=ExecutionMode.TEXT, qualification=())
        with pytest.raises(CostPolicyError):
            await a.call_json(route, "test", public=True)
    assert methods == ["POST", "GET"]


async def test_over_free_request_budget_blocked_before_post(free_cost_guard):
    def handler(request):
        pytest.fail("Oversized request reached HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["groq:qwen/qwen3.8-27b"], mode=ExecutionMode.TEXT, qualification=())
        with pytest.raises(CostPolicyError, match="no truncation"):
            await a.call_json(route, "x" * 8001, public=True)


def test_eventual_success_does_not_erase_timeout_or_invalid_json():
    row = {"provider": "p", "model": "m", "mode": "text", "clip_id": "c", "status": "SUCCEEDED",
           "structured_valid": True, "latency_seconds": 1, "wall_seconds": 90, "requests": [],
           "attempt_history": [{"status": "TIMEOUT", "structured_valid": False, "latency_seconds": 40},
                               {"status": "SUCCEEDED", "structured_valid": True, "latency_seconds": 1}]}
    result = summarize([row])[0]
    assert result["eventual_completion_rate"] == 1
    assert result["success_rate"] == result["json_validity"] == .5
    assert result["timeouts"] == 1 and result["p50_latency"] == 90


async def test_intrinsic_request_limit_does_not_become_daily_quota_or_leak_account(free_cost_guard):
    def handler(request):
        return httpx.Response(429, json={"error": {"message":
            "Request too large for model in org_private on tokens per minute (TPM): Limit: 8000, Requested: 9000"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["groq:qwen/qwen3.8-27b"], mode=ExecutionMode.TEXT, qualification=())
        token = request_scope.set("large-job")
        try:
            with pytest.raises(CostPolicyError, match="actual Free TPM"):
                await a.call_json(route, "test", public=True)
        finally:
            request_scope.reset(token)
    row = free_cost_guard.ledger.telemetry("large-job")[0]
    assert row["error_metadata"]["numbers"] == {"limit": 8000., "requested": 9000.}
    assert "org_private" not in str(row)
    with free_cost_guard.ledger.connect() as db:
        assert db.execute("SELECT * FROM blocks").fetchone() is None


async def test_native_disappearance_preserves_all_frames_with_free_tpm_batching(tmp_path, free_cost_guard):
    import json
    frames = []
    for i in range(10):
        path = tmp_path / f"{i}.png"
        path.write_bytes(b"fixture-image")
        frames.append(MeasuredFrame(id=f"f{i}", shot_id="s1", pts_seconds=i,
                                   locator=str(path), checksum="fixture"))
    window = MeasuredVideoWindow(id="window", source_asset_id="asset", source_start=0, source_end=20,
        video_path="unused.mp4", media_sha256="fixture", frames=tuple(frames), contact_sheet_paths=(),
        transcript={"t1": "Full transcript context"}, extraction_version="test")
    for identity, entry in list(free_cost_guard.registry.items()):
        if Capability.VIDEO_NATIVE in entry.capabilities:
            del free_cost_guard.registry[identity]
    entry = free_cost_guard.registry["groq:qwen/qwen3.8-27b"]
    scores = [qualification(entry, cap, ExecutionMode.MULTIFRAME) for cap in (Capability.VIDEO_MULTIFRAME, Capability.JSON)]
    router = CapabilityRouter(free_cost_guard, scores, {"groq": "test-key"})
    sent_frames, widths = [], []
    def handler(request):
        assert request.url.host == "api.groq.com"
        content = json.loads(request.content)["messages"][0]["content"]
        context = json.loads(content[0]["text"].rsplit("\n", 1)[1])
        assert context["transcript"] == window.transcript
        ids = [f["id"] for f in context["frames"]]
        sent_frames.extend(ids)
        widths.append(len(content) - 1)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "window_id": "window", "events": [{"kind": "avatar", "description": "fixture",
                "evidence_ids": ids, "confidence": .9}], "unknowns": []})}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        result = await VideoUnderstandingProvider(router, CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=c)).understand(
            window, public_data=True, dimensions={"japanese"})
    assert result["provenance"]["mode"] == ExecutionMode.MULTIFRAME
    assert set(sent_frames) == {f.id for f in frames} and len(sent_frames) == 10
    assert max(widths) <= 2


@pytest.mark.parametrize("data", [{}, {"data": [None]}, {"data": [{"index": 0, "embedding": [True]}]}])
async def test_malformed_embedding_response_can_defer_without_truncation(free_cost_guard, data):
    import json
    def handler(request):
        assert json.loads(request.content)["truncate"] == "NONE"
        return httpx.Response(200, json=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        a = CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["nvidia:nvidia/nemotron-3-embed-1b"],
                      mode=ExecutionMode.EMBEDDING, qualification=())
        with pytest.raises(ValueError):
            await a.embeddings(route, ["whole input"], input_type="passage", public=True)


@pytest.mark.parametrize("data", [[], {"segments": [None]}, {"segments": [{"start": "bad", "end": 1}]}])
async def test_malformed_asr_timestamps_defer_instead_of_crashing(tmp_path, free_cost_guard, data):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"bounded fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as c:
        a = CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["groq:whisper-large-v3"],
                      mode=ExecutionMode.AUDIO, qualification=())
        with pytest.raises(ValueError):
            await a.transcribe(route, path, seconds=2, public=True)
