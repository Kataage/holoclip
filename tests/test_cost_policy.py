from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from holoclip.providers.cost import (
    AccountEvidence, CostGuard, CostPolicyError, FreeModel, FreeModelReverification, QuotaBlocked, QuotaLedger,
    credential_fingerprint, free_registry,
)

NOW = datetime(2026, 10, 4, 10, tzinfo=UTC)


def guard_at(path, *, provider="nvidia", budget=100, clock=lambda: NOW):
    account = AccountEvidence(
        provider=provider, credential_sha256=credential_fingerprint("test-key"),
        account_id="fixture-account", plan="FREE", billing_enabled=False,
        verified_at=NOW, evidence_reference="mock-account-contract",
        daily_limits={"requests": budget, "tokens": 1000000, "audio_seconds": 20000,
                      "uploaded_bytes": 10000000},
    )
    return CostGuard(QuotaLedger(path), [account], clock=clock)


@pytest.mark.parametrize("mutation", [
    {"paid_fallback_allowed": True}, {"cost_class": "PAID"}, {"cost_class": "LOCAL"},
])
def test_paid_or_local_model_is_not_representable(mutation):
    raw = next(iter(free_registry().values())).model_dump()
    with pytest.raises(ValidationError):
        FreeModel.model_validate(raw | mutation)


def test_reverification_refreshes_only_known_official_free_model_evidence(tmp_path):
    guard = guard_at(tmp_path / "quota.db", clock=lambda: NOW + timedelta(days=9))
    entry = guard.registry["nvidia:z-ai/glm-5.3"]
    with pytest.raises(CostPolicyError, match="FREE_STATUS_STALE"):
        guard.model(entry.provider, entry.model, entry.base_url)
    proof = FreeModelReverification(provider=entry.provider, model=entry.model,
        verified_at=NOW + timedelta(days=9), official_reference=entry.verification_references[0],
        reviewed_by="free-pricing-review", confirmed_no_charge=True)
    guard.apply_free_reverification([proof])
    updated = guard.model(entry.provider, entry.model, entry.base_url)
    assert updated.verified_at == proof.verified_at
    assert updated.base_url == entry.base_url
    assert updated.operations == entry.operations
    assert updated.capabilities == entry.capabilities
    assert updated.free_status_evidence == proof.official_reference


@pytest.mark.parametrize("change", [
    {"official_reference": "https://paid.example.invalid"},
    {"model": "paid/unregistered"},
    {"verified_at": NOW + timedelta(days=10)},
])
def test_unverified_free_refresh_cannot_override_whitelist(tmp_path, change):
    guard = guard_at(tmp_path / "quota.db", clock=lambda: NOW + timedelta(days=9))
    entry = guard.registry["nvidia:z-ai/glm-5.3"]
    original = entry.verified_at
    proof = FreeModelReverification(provider=entry.provider, model=entry.model,
        verified_at=NOW + timedelta(days=9), official_reference=entry.verification_references[0],
        reviewed_by="free-pricing-review", confirmed_no_charge=True)
    with pytest.raises(CostPolicyError):
        guard.apply_free_reverification([proof.model_copy(update=change)])
    assert guard.registry[entry.identity].verified_at == original


@pytest.mark.parametrize("mutation", [{"billing_enabled": True}, {"plan": "PAID"}])
def test_paid_account_rejected(tmp_path, mutation):
    raw = guard_at(tmp_path / "quota.db").accounts[0].model_dump()
    with pytest.raises(ValidationError):
        AccountEvidence.model_validate(raw | mutation)


@pytest.mark.parametrize("provider,model,headers", [
    ("gemini", "gemini-3.8-flash", {"x-goog-api-key": "paid-project-key"}),
    ("gemini", "gemini-3.8-flash", {"Authorization": "Bearer other-key"}),
    ("groq", "openai/gpt-oss-120b", {"Authorization": "Bearer paid-project-key"}),
    ("groq", "openai/gpt-oss-120b", {"x-goog-api-key": "paid-project-key"}),
])
async def test_request_cannot_substitute_a_paid_wire_credential(tmp_path, provider, model, headers):
    guard = guard_at(tmp_path / "quota.db", provider=provider)
    entry = guard.registry[f"{provider}:{model}"]
    def fail_if_called(request):
        pytest.fail("Unverified credential reached HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        with pytest.raises(CostPolicyError, match="API key|Authorization"):
            await guard.post(client, provider=provider, model=model, base_url=entry.base_url,
                key="test-key", operation=next(iter(entry.operations)),
                estimate={"requests": 1}, public=True, headers=headers)
    with guard.ledger.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("provider,model", [
    ("gemini", "gemini-3.8-flash"),
    ("groq", "openai/gpt-oss-120b"),
])
async def test_verified_wire_credentials_are_injected_even_if_caller_omits_headers(tmp_path, provider, model):
    guard = guard_at(tmp_path / "quota.db", provider=provider)
    entry = guard.registry[f"{provider}:{model}"]
    observed = []
    def handler(request):
        observed.append(request.headers)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await guard.post(client, provider=provider, model=model, base_url=entry.base_url,
            key="test-key", operation=next(iter(entry.operations)),
            estimate={"requests": 1}, public=True)
    assert len(observed) == 1
    if provider == "gemini":
        assert observed[0].get("x-goog-api-key") == "test-key"
    else:
        assert observed[0].get("authorization") == "Bearer test-key"


@pytest.mark.parametrize("base,model", [
    ("https://paid.nvidia.com/v1", "z-ai/glm-5.3"),
    ("https://integrate.api.nvidia.com/v1?route=paid", "z-ai/glm-5.3"),
    ("http://localhost:8000", "z-ai/glm-5.3"),
    ("https://integrate.api.nvidia.com/v1", "paid/unregistered"),
])
async def test_reject_before_http(tmp_path, base, model):
    def handler(request):
        pytest.fail("Forbidden endpoint reached")
    guard = guard_at(tmp_path / "quota.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CostPolicyError):
            await guard.post(client, provider="nvidia", model=model, base_url=base,
                             key="test-key", operation="chat/completions",
                             estimate={"requests": 1}, public=True)


async def test_429_restart_reset_never_reaches_paid_endpoint(tmp_path):
    calls = []
    current = [NOW]
    guard = guard_at(tmp_path / "quota.db", clock=lambda: current[0])
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(429 if len(calls) == 1 else 200, headers={"Retry-After": "60"})
    args = dict(provider="nvidia", model="z-ai/glm-5.3",
                base_url="https://integrate.api.nvidia.com/v1", key="test-key",
                operation="chat/completions", estimate={"requests": 1}, public=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await guard.post(client, **args)
        guard = guard_at(tmp_path / "quota.db", clock=lambda: current[0])
        with pytest.raises(QuotaBlocked):
            await guard.post(client, **args)
        current[0] += timedelta(seconds=61)
        assert (await guard.post(client, **args)).status_code == 200
    assert calls == [args["base_url"] + "/chat/completions"] * 2


def test_account_budget_is_atomic_and_shared_between_models(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota.db")
    def reserve(index):
        try:
            ledger.reserve("shared-account", str(index), {"requests": 3}, {"requests": 1}, NOW)
            return True
        except QuotaBlocked:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(20))) == 3
    with pytest.raises(QuotaBlocked):
        QuotaLedger(ledger.path).reserve("shared-account", "another-model", {"requests": 3},
                                       {"requests": 1}, NOW)
    ledger.reserve("shared-account", "next-day", {"requests": 3}, {"requests": 1},
                   NOW + timedelta(days=1))


@pytest.mark.parametrize("estimate", [{"requests": 1, "tokens": -1},
                                         {"requests": 1, "tokens": float("nan")},
                                         {"requests": 0}, {"requests": 1, "unknown": 1}])
def test_bad_estimate_cannot_bypass_quota(tmp_path, estimate):
    with pytest.raises(CostPolicyError):
        QuotaLedger(tmp_path / "q.db").reserve("a", "m", {"requests": 3, "tokens": 5},
                                               estimate, NOW)


def test_stale_free_status_and_credential_binding(tmp_path):
    guard = guard_at(tmp_path / "q.db")
    with pytest.raises(CostPolicyError, match="account"):
        guard.authorize("nvidia", "z-ai/glm-5.3", "https://integrate.api.nvidia.com/v1", "other")
    guard.clock = lambda: NOW + timedelta(days=8)
    with pytest.raises(CostPolicyError, match="FREE_STATUS_STALE"):
        guard.model("nvidia", "z-ai/glm-5.3", "https://integrate.api.nvidia.com/v1")


async def test_redirect_private_and_missing_account_rejected(tmp_path):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://paid.example/run"})
    guard = guard_at(tmp_path / "q.db")
    args = dict(provider="nvidia", model="z-ai/glm-5.3",
                base_url="https://integrate.api.nvidia.com/v1", key="test-key",
                operation="chat/completions", estimate={"requests": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        with pytest.raises(CostPolicyError, match="public"):
            await guard.post(c, **args, public=False)
        with pytest.raises(CostPolicyError, match="redirect"):
            await guard.post(c, **args, public=True)
        guard.accounts = []
        with pytest.raises(CostPolicyError, match="account"):
            await guard.post(c, **args, public=True)
    assert len(calls) == 1
