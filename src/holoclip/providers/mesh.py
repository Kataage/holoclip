"""Capability routing with identical quality floors across vendors and modes."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from holoclip.execution import StageArtifactCache, StageStatus, make_input_fingerprint
from holoclip.providers.cost import Capability, CostGuard, CostPolicyError, FreeModel, QuotaBlocked


class ExecutionMode(StrEnum):
    TEXT = "text"
    NATIVE = "native_video"
    MULTIFRAME = "shot_aware_multiframe"
    CONTACT_SHEET = "contact_sheet"
    FUSION = "evidence_fusion"
    AUDIO = "audio"
    EMBEDDING = "embedding"


class ProviderState(StrEnum):
    VERIFIED_FREE = "VERIFIED_FREE"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    QUOTA_LOW = "QUOTA_LOW"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"
    AUTH_FAILED = "AUTH_FAILED"
    MODEL_DEPRECATED = "MODEL_DEPRECATED"
    FREE_STATUS_STALE = "FREE_STATUS_STALE"
    DISQUALIFIED_QUALITY = "DISQUALIFIED_QUALITY"


class QualityFloor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    quality_score: float = Field(default=0.8, ge=0.8, le=1)
    success_rate: float = Field(default=0.95, ge=0.95, le=1)
    json_validity: float = Field(default=0.99, ge=0.99, le=1)
    max_hallucination_rate: float = Field(default=0.02, ge=0, le=0.02)
    max_error_rate: float = Field(default=0.05, ge=0, le=0.05)
    consistency: float = Field(default=0.9, ge=0.9, le=1)
    min_gold_cases: int = Field(default=30, ge=30)


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    model: str
    capability: Capability
    mode: ExecutionMode
    benchmark_version: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    real_media: bool
    held_out: bool
    gold_cases: int = Field(ge=0)
    qualified: bool
    quality_score: float = Field(ge=0, le=1)
    hallucination_rate: float = Field(ge=0, le=1)
    error_rate: float = Field(ge=0, le=1)
    critical_hallucinations: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    json_validity: float = Field(ge=0, le=1)
    consistency: float = Field(ge=0, le=1)
    p50_latency: float = Field(ge=0)
    p95_latency: float = Field(ge=0)
    quota_efficiency: float = Field(ge=0)
    verified_free_at: datetime
    evaluated_at: datetime
    covered_dimensions: frozenset[str]
    notes: tuple[str, ...] = ()

    def clears(self, floor: QualityFloor, now: datetime, dimensions: frozenset[str]) -> bool:
        return (
            self.qualified and self.real_media and self.held_out and
            self.benchmark_version.startswith("holoclip-real-") and
            self.evaluated_at.tzinfo is not None and self.verified_free_at.tzinfo is not None and
            timedelta(0) <= now - self.evaluated_at <= timedelta(days=14) and
            timedelta(0) <= now - self.verified_free_at <= timedelta(days=7) and
            self.gold_cases >= floor.min_gold_cases and self.critical_hallucinations == 0 and
            self.quality_score >= floor.quality_score and self.success_rate >= floor.success_rate and
            self.json_validity >= floor.json_validity and
            self.hallucination_rate <= floor.max_hallucination_rate and
            self.error_rate <= floor.max_error_rate and self.consistency >= floor.consistency and
            dimensions <= self.covered_dimensions
        )


class ModeRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: ExecutionMode
    required: frozenset[Capability] = Field(min_length=1)


class StageContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: str
    alternatives: tuple[ModeRequirement, ...] = Field(min_length=1)
    optional: frozenset[Capability] = frozenset()
    floor: QualityFloor = Field(default_factory=QualityFloor)
    dimensions: frozenset[str] = frozenset()
    public_data: bool = False
    quota_estimate: dict[str, float]
    timeout_seconds: float = Field(default=60, gt=0, le=120)
    second_opinion_required: bool = False


class Route(BaseModel):
    entry: FreeModel
    mode: ExecutionMode
    qualification: tuple[Qualification, ...]


class MeshDeferred(CostPolicyError):
    def __init__(self, reasons: list[str], *, quota=False, retry_after=None):
        self.reasons = reasons
        self.quota = quota
        self.retry_after = retry_after
        super().__init__(("BLOCKED_QUOTA" if quota else "BLOCKED_DEPENDENCY") + ": " + "; ".join(reasons))


class MeshState:
    def __init__(self, guard: CostGuard):
        self.guard = guard
        with guard.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS health (identity TEXT PRIMARY KEY, state TEXT, "
                       "retry_at REAL, checked_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS outcomes (identity TEXT, mode TEXT, at TEXT, "
                       "state TEXT, latency REAL)")

    def state(self, identity: str) -> ProviderState:
        with self.guard.ledger.connect() as db:
            row = db.execute("SELECT state,retry_at FROM health WHERE identity=?", (identity,)).fetchone()
        if not row:
            return ProviderState.VERIFIED_FREE
        state = ProviderState(row[0])
        if state in {ProviderState.BLOCKED_QUOTA, ProviderState.DEGRADED} and row[1] is not None:
            if self.guard.clock().timestamp() >= row[1]:
                return ProviderState.VERIFIED_FREE
        return state

    def record(self, identity: str, state: ProviderState, *, mode="", latency=0, delay=None):
        now = self.guard.clock()
        retry_at = now.timestamp() + delay if delay is not None else None
        with self.guard.ledger.connect() as db:
            db.execute("INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                       "state=excluded.state,retry_at=excluded.retry_at,checked_at=excluded.checked_at",
                       (identity, state.value, retry_at, now.isoformat()))
            db.execute("INSERT INTO outcomes VALUES (?,?,?,?,?)",
                       (identity, str(mode), now.isoformat(), state.value, latency))

    def reconcile_models(self, provider: str, active_ids: set[str], *, complete: bool):
        # An auth/error/partial model-list response cannot deprecate the entire provider.
        if not complete:
            return
        for entry in self.guard.registry.values():
            if entry.provider == provider and entry.model not in active_ids:
                self.record(entry.identity, ProviderState.MODEL_DEPRECATED)


class CapabilityRouter:
    def __init__(self, guard: CostGuard, qualifications: list[Qualification], keys: dict[str, str],
                 *, cache: StageArtifactCache | None = None):
        self.guard = guard
        self.qualifications = qualifications
        self.keys = keys
        self.state = MeshState(guard)
        self.cache = cache

    def candidates(self, contract: StageContract, *, exclude_vendors=frozenset()) -> list[Route]:
        routes = []
        now = self.guard.clock()
        for entry in self.guard.registry.values():
            if entry.provider in exclude_vendors:
                continue
            if self.state.state(entry.identity) not in {
                ProviderState.VERIFIED_FREE, ProviderState.HEALTHY, ProviderState.QUOTA_LOW,
            }:
                continue
            try:
                self.guard.authorize(entry.provider, entry.model, entry.base_url,
                                     self.keys.get(entry.provider, ""), public=contract.public_data)
            except CostPolicyError:
                continue
            for alternative in contract.alternatives:
                if not alternative.required <= entry.capabilities:
                    continue
                if any(self.state.state(f"{entry.identity}/{alternative.mode}/{cap}") ==
                       ProviderState.DISQUALIFIED_QUALITY for cap in alternative.required):
                    continue
                scores = []
                for cap in alternative.required:
                    rows = [q for q in self.qualifications if q.provider == entry.provider and
                            q.model == entry.model and q.capability == cap and q.mode == alternative.mode
                            and q.clears(contract.floor, now, contract.dimensions)]
                    if not rows:
                        break
                    scores.append(max(rows, key=lambda q: q.evaluated_at))
                if len(scores) == len(alternative.required):
                    routes.append(Route(entry=entry, mode=alternative.mode, qualification=tuple(scores)))
        # No vendor/mode priority. The weakest required capability defines route quality.
        return sorted(routes, key=lambda r: (
            -min(q.quality_score for q in r.qualification),
            -min(q.success_rate for q in r.qualification),
            max(q.hallucination_rate for q in r.qualification),
            -min(q.quota_efficiency for q in r.qualification),
            max(q.p95_latency for q in r.qualification), r.entry.identity, r.mode.value,
        ))

    async def run(self, contract: StageContract, payload: dict,
                  invoke: Callable[[Route, dict], Awaitable[dict]], *, exclude_vendors=frozenset(),
                  validate: Callable[[dict], dict] | None = None, diagnostic=False) -> dict:
        contract = StageContract.model_validate(contract.model_dump())
        if diagnostic and not contract.stage.startswith("provider_benchmark"):
            raise CostPolicyError("Diagnostic qualification bypass is restricted to benchmark stages")
        fingerprint = make_input_fingerprint({"contract": contract.model_dump(mode="json"),
                                              "payload": payload, "exclude": sorted(exclude_vendors),
                                              "diagnostic": diagnostic})
        args = dict(stage=contract.stage, subject_type="capability_inference", subject_id=fingerprint,
                    input_fingerprint=fingerprint, stage_version="mesh-v1", provider="capability_mesh",
                    model="router-v1", prompt_version=payload.get("prompt_version", "v1"))
        cached = self.cache.reusable_json(**args) if self.cache else None
        if cached is not None:
            return cached
        run = self.cache.start(**args) if self.cache else None
        routes = self.candidates(contract, exclude_vendors=exclude_vendors)
        # Benchmark bootstrap relaxes only quality qualification; cost/data checks remain mandatory.
        if diagnostic:
            routes = []
            identity = payload.get("diagnostic_identity")
            mode = ExecutionMode(payload.get("diagnostic_mode", "text"))
            entry = self.guard.registry.get(identity)
            if entry and any(a.mode == mode and a.required <= entry.capabilities
                             for a in contract.alternatives):
                try:
                    self.guard.authorize(entry.provider, entry.model, entry.base_url,
                                         self.keys.get(entry.provider, ""), public=contract.public_data)
                    routes = [Route(entry=entry, mode=mode, qualification=())]
                except CostPolicyError:
                    pass
        reasons = []
        quota_seen = False
        retry = None
        for route in routes:
            started = asyncio.get_running_loop().time()
            try:
                # Enforce router wall-clock timeout even if adapter hangs.
                async with asyncio.timeout(contract.timeout_seconds):
                    output = await invoke(route, payload)
                    if not isinstance(output, dict):
                        raise ValueError("Expected structured JSON object")
                    if validate:
                        output = validate(output)
                    if contract.second_opinion_required:
                        # Validate inside the guarded attempt, before recording success.
                        Opinion(provider=route.entry.provider, **{
                            k: output.get(k) for k in ("decision", "confidence", "evidence_ids")})
            except QuotaBlocked as exc:
                quota_seen = True
                retry = exc.retry_after if retry is None else min(retry, exc.retry_after or retry)
                self.state.record(route.entry.identity, ProviderState.BLOCKED_QUOTA,
                                  mode=route.mode, delay=exc.retry_after or 86400)
                reasons.append(f"{route.entry.identity}: BLOCKED_QUOTA")
                continue
            except asyncio.CancelledError as exc:
                if run:
                    self.cache.fail(run, status=StageStatus.CANCELLED, error=exc)
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError):
                self.state.record(route.entry.identity, ProviderState.DEGRADED, mode=route.mode,
                                  latency=asyncio.get_running_loop().time() - started, delay=300)
                reasons.append(f"{route.entry.identity}: timeout/network")
                continue
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                state = (ProviderState.AUTH_FAILED if code in {401, 403} else
                         ProviderState.MODEL_DEPRECATED if code in {404, 410} else
                         ProviderState.DEGRADED)
                self.state.record(route.entry.identity, state, mode=route.mode, delay=300
                                  if state == ProviderState.DEGRADED else None)
                reasons.append(f"{route.entry.identity}: HTTP {code}")
                continue
            except CostPolicyError:
                reasons.append(f"{route.entry.identity}: policy blocked")
                continue
            except ValueError:
                requirement = next(a for a in contract.alternatives if a.mode == route.mode)
                for cap in requirement.required:
                    self.state.record(f"{route.entry.identity}/{route.mode}/{cap}",
                                      ProviderState.DISQUALIFIED_QUALITY, mode=route.mode)
                reasons.append(f"{route.entry.identity}: invalid output/policy")
                continue
            elapsed = asyncio.get_running_loop().time() - started
            self.state.record(route.entry.identity, ProviderState.HEALTHY, mode=route.mode, latency=elapsed)
            result = {"output": output, "provenance": {
                "provider": route.entry.provider, "model": route.entry.model,
                "mode": route.mode.value, "verified_free_at": route.entry.verified_at.isoformat(),
                "benchmark_versions": sorted({q.benchmark_version for q in route.qualification}),
                "input_fingerprint": fingerprint, "latency_seconds": elapsed,
                "diagnostic_only": diagnostic,
            }}
            if contract.second_opinion_required:
                first = Opinion(provider=route.entry.provider, **{
                    k: output[k] for k in ("decision", "confidence", "evidence_ids")})
                second_contract = StageContract.model_validate(contract.model_dump() | {
                    "stage": contract.stage + ":second_opinion", "second_opinion_required": False})
                second = None
                second_result = None
                try:
                    second_result = await self.run(second_contract, payload, invoke,
                        exclude_vendors=exclude_vendors | {route.entry.provider}, validate=validate)
                    second = Opinion(provider=second_result["provenance"]["provider"], **{
                        k: second_result["output"][k] for k in
                        ("decision", "confidence", "evidence_ids")})
                except MeshDeferred:
                    pass
                result["consensus"] = reconcile_opinions(first, second)
                result["second_opinion"] = second_result
                result["output"]["aggressive_edit_allowed"] = result["consensus"]["aggressive_edit_allowed"]
            if run:
                self.cache.succeed_json(run, result, kind="capability_observation")
            return result
        if not reasons:
            # Persisted quota failures must remain quota-blocked across router restarts.
            quota_seen = any(self.state.state(e.identity) == ProviderState.BLOCKED_QUOTA
                             for e in self.guard.registry.values())
            reasons = ["No currently free, healthy, qualified route clears the unchanged quality floor"]
        error = MeshDeferred(reasons, quota=quota_seen, retry_after=retry)
        if run:
            self.cache.fail(run, status=StageStatus.BLOCKED_QUOTA if quota_seen else
                            StageStatus.BLOCKED_DEPENDENCY, error=error, retry_after=retry)
        raise error


class ConsensusState(StrEnum):
    AGREE = "AGREE"
    SOFT_DISAGREE = "SOFT_DISAGREE"
    HARD_DISAGREE = "HARD_DISAGREE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Opinion(BaseModel):
    provider: str
    decision: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: frozenset[str]


def reconcile_opinions(first: Opinion, second: Opinion | None) -> dict:
    if second is None or first.provider == second.provider or not first.evidence_ids or not second.evidence_ids:
        state = ConsensusState.INSUFFICIENT_EVIDENCE
    elif first.decision == second.decision:
        state = ConsensusState.AGREE
    elif min(first.confidence, second.confidence) >= 0.8:
        state = ConsensusState.HARD_DISAGREE
    else:
        state = ConsensusState.SOFT_DISAGREE
    return {"state": state.value, "aggressive_edit_allowed": state == ConsensusState.AGREE
            and second is not None and min(first.confidence, second.confidence) >= 0.8,
            "first": first.model_dump(mode="json"),
            "second": second.model_dump(mode="json") if second else None}


def load_qualifications(path: Path) -> list[Qualification]:
    if not path.exists():
        return []
    from holoclip.provider_benchmark import VERSION, derive_qualifications
    from holoclip.video_measurement import checksum_file
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or bundle.get("version") != VERSION:
        raise CostPolicyError("Qualification needs a checksum-bound benchmark bundle")
    report_path = path.parent / bundle["report_path"]
    if checksum_file(report_path) != bundle["report_sha256"]:
        raise CostPolicyError("Qualification benchmark report changed")
    actual = derive_qualifications(json.loads(report_path.read_text(encoding="utf-8")), bundle["report_sha256"])
    declared = [Qualification.model_validate(row) for row in bundle["qualifications"]]
    if actual != declared:
        raise CostPolicyError("Qualification differs from recomputed benchmark evidence")
    return actual
