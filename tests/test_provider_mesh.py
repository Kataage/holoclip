from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from holoclip.execution import StageArtifactCache
from holoclip.providers.cost import Capability, QuotaBlocked
from holoclip.providers.mesh import (
    CapabilityRouter, ExecutionMode, MeshDeferred, ModeRequirement, Opinion, ProviderState,
    Qualification, QualityFloor, StageContract, reconcile_opinions,
)
from holoclip.repository import SQLiteCanonicalRepository
from test_cost_policy import NOW


def qualification(entry, cap, mode, **changes):
    raw = dict(provider=entry.provider, model=entry.model, capability=cap, mode=mode,
               benchmark_version="holoclip-real-v1", dataset_sha256="a" * 64,
               report_sha256="b" * 64, real_media=True, held_out=True, gold_cases=30,
               qualified=True, quality_score=0.9, hallucination_rate=0,
               error_rate=0, critical_hallucinations=0, success_rate=1, json_validity=1,
               consistency=1, p50_latency=1, p95_latency=2, quota_efficiency=1,
               verified_free_at=NOW, evaluated_at=NOW, covered_dimensions={"japanese"})
    return Qualification(**(raw | changes))


def router_for(guard, *, cache=None):
    entries = [guard.registry["nvidia:z-ai/glm-5.3-flash"],
               guard.registry["groq:qwen/qwen3.8-27b"]]
    qualifications = [qualification(e, cap, mode) for e in entries for cap, mode in [
        (Capability.REASONING_STRONG, ExecutionMode.TEXT),
        (Capability.VIDEO_MULTIFRAME, ExecutionMode.MULTIFRAME),
        (Capability.VIDEO_MULTIFRAME, ExecutionMode.CONTACT_SHEET),
    ]]
    return CapabilityRouter(guard, qualifications, {"nvidia": "test-key", "groq": "test-key"},
                            cache=cache)


def contract(*, video=False, second=False):
    alternatives = (ModeRequirement(mode=ExecutionMode.TEXT,
                                     required={Capability.REASONING_STRONG}),)
    if video:
        alternatives = tuple(ModeRequirement(mode=m, required={cap}) for m, cap in [
            (ExecutionMode.NATIVE, Capability.VIDEO_NATIVE),
            (ExecutionMode.MULTIFRAME, Capability.VIDEO_MULTIFRAME),
            (ExecutionMode.CONTACT_SHEET, Capability.VIDEO_MULTIFRAME),
        ])
    return StageContract(stage="video_semantics" if video else "reasoning", alternatives=alternatives,
                         dimensions={"japanese"}, public_data=True,
                         quota_estimate={"requests": 1}, timeout_seconds=0.02,
                         second_opinion_required=second)


async def test_all_native_removed_multiframe_still_completes(free_cost_guard):
    router = router_for(free_cost_guard)
    for e in list(router.guard.registry.values()):
        if Capability.VIDEO_NATIVE in e.capabilities:
            router.guard.registry.pop(e.identity)
    async def invoke(route, payload):
        assert route.mode in {ExecutionMode.MULTIFRAME, ExecutionMode.CONTACT_SHEET}
        return {"events": [{"frame_id": "measured-f1"}]}
    result = await router.run(contract(video=True), {"frame_ids": ["measured-f1"]}, invoke)
    assert result["output"]["events"]
    assert result["provenance"]["benchmark_versions"] == ["holoclip-real-v1"]


async def test_timeout_switches_independent_vendor(free_cost_guard):
    import asyncio
    router = router_for(free_cost_guard)
    first = router.candidates(contract())[0].entry.provider
    calls = []
    async def invoke(route, payload):
        calls.append(route.entry.provider)
        if route.entry.provider == first:
            await asyncio.sleep(1)
        return {"ok": True}
    result = await router.run(contract(), {}, invoke)
    assert len(calls) == 2 and result["provenance"]["provider"] != first
    assert router.state.state(router.candidates(contract())[0].entry.identity) == ProviderState.HEALTHY


async def test_low_quality_fallback_is_rejected_without_dispatch(free_cost_guard):
    router = router_for(free_cost_guard)
    router.qualifications = [q.model_copy(update={"quality_score": 0.79}) for q in router.qualifications]
    async def invoke(*args):
        pytest.fail("Quality floor was lowered")
    with pytest.raises(MeshDeferred, match="unchanged quality floor"):
        await router.run(contract(), {}, invoke)
    with pytest.raises(ValidationError):
        QualityFloor(quality_score=0.79)


def test_partial_qualification_does_not_leak_between_capabilities(free_cost_guard):
    router = router_for(free_cost_guard)
    router.qualifications = [q for q in router.qualifications if q.capability == Capability.REASONING_STRONG]
    assert router.candidates(contract())
    assert not router.candidates(contract(video=True))
    router.qualifications = [q.model_copy(update={"evaluated_at": NOW - timedelta(days=15)})
                             for q in router.qualifications]
    assert not router.candidates(contract())


async def test_quota_restart_resume_and_cached_success(tmp_path, free_cost_guard):
    cache = StageArtifactCache(SQLiteCanonicalRepository(tmp_path / "stages.db"), tmp_path / "artifacts")
    router = router_for(free_cost_guard, cache=cache)
    calls = 0
    async def blocked(route, payload):
        nonlocal calls
        calls += 1
        raise QuotaBlocked(60)
    with pytest.raises(MeshDeferred) as error:
        await router.run(contract(), {"measured_id": "f1"}, blocked)
    assert error.value.quota and calls == 2
    router = router_for(free_cost_guard, cache=cache)
    with pytest.raises(MeshDeferred) as error:
        await router.run(contract(), {"measured_id": "f1"}, blocked)
    assert error.value.quota and calls == 2
    free_cost_guard.clock = lambda: NOW + timedelta(seconds=61)
    async def success(route, payload):
        return {"ok": True}
    result = await router.run(contract(), {"measured_id": "f1"}, success)
    router.guard.registry = {}
    assert await router.run(contract(), {"measured_id": "f1"}, blocked) == result


def test_deprecation_requires_complete_model_list(free_cost_guard):
    router = router_for(free_cost_guard)
    router.state.reconcile_models("groq", set(), complete=False)
    assert any(r.entry.provider == "groq" for r in router.candidates(contract()))
    router.state.reconcile_models("groq", set(), complete=True)
    assert all(r.entry.provider != "groq" for r in router.candidates(contract()))


async def test_hard_disagreement_suppresses_aggressive_edit(free_cost_guard):
    router = router_for(free_cost_guard)
    async def invoke(route, payload):
        return {"decision": "crop" if route.entry.provider == "groq" else "retain",
                "confidence": 0.95, "evidence_ids": ["f1"]}
    result = await router.run(contract(second=True), {}, invoke)
    assert result["consensus"]["state"] == "HARD_DISAGREE"
    assert result["output"]["aggressive_edit_allowed"] is False
    assert result["second_opinion"]["provenance"]["provider"] != result["provenance"]["provider"]


def test_same_vendor_is_insufficient_even_if_models_agree():
    opinion = Opinion(provider="vendor", decision="crop", confidence=0.99, evidence_ids={"f1"})
    assert reconcile_opinions(opinion, opinion)["state"] == "INSUFFICIENT_EVIDENCE"


async def test_403_and_removed_model_fallback(free_cost_guard):
    router = router_for(free_cost_guard)
    first = router.candidates(contract())[0].entry
    async def invoke(route, payload):
        if route.entry.identity == first.identity:
            response = httpx.Response(410, request=httpx.Request("POST", first.base_url))
            response.raise_for_status()
        return {"ok": True}
    assert (await router.run(contract(), {}, invoke))["provenance"]["provider"] != first.provider
    assert router.state.state(first.identity) == ProviderState.MODEL_DEPRECATED
