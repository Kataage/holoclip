"""Fail-closed monetary policy at the HTTP boundary, including legacy adapters.

Public free pricing and account entitlement are separate evidence. A successful
request is never evidence of either. No endpoint, billing or tier fallback exists.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class CostPolicy(StrEnum):
    ZERO_COST_ONLY = "ZERO_COST_ONLY"


class Capability(StrEnum):
    VIDEO_NATIVE = "video_semantics_native"
    VIDEO_MULTIFRAME = "video_semantics_multiframe"
    IMAGE = "image_semantics"
    GROUNDING = "visual_grounding_hint"
    JAPANESE_READING = "japanese_text_reading"
    ASR_SEGMENTS = "asr_segment_timestamps"
    ASR_WORDS = "asr_word_timestamps"
    REASONING_STRONG = "text_reasoning_strong"
    REASONING_FAST = "text_reasoning_fast"
    JSON = "structured_json"
    EMBEDDING = "embedding"
    RENDER_QA = "semantic_render_qa"


class CostPolicyError(RuntimeError):
    """No request is permitted; fix/reverify policy rather than changing tiers."""


class QuotaBlocked(RuntimeError):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__("BLOCKED_QUOTA: preserve artifacts and resume after reset")


class FreeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    model: str
    base_url: str
    operations: frozenset[str]
    capabilities: frozenset[Capability]
    cost_class: Literal["FREE_TIER", "FREE_ENDPOINT"]
    verified_at: datetime
    verification_references: tuple[str, ...]
    quota_snapshot: dict[str, float]
    data_use: str
    public_data_only: Literal[True] = True
    paid_fallback_allowed: Literal[False] = False
    max_images: int = 0
    max_bytes: int = 20_000_000
    max_video_seconds: float = 120
    available: bool = True

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"


class AccountEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    credential_sha256: str
    account_id: str
    plan: Literal["FREE"]
    billing_enabled: Literal[False]
    paid_fallback_allowed: Literal[False] = False
    verified_at: datetime
    evidence_reference: str = Field(min_length=1)
    # Hard internal budgets, shared by every model and process for this account.
    daily_limits: dict[str, float]


def credential_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _fresh(timestamp: datetime, now: datetime, lifetime: timedelta) -> bool:
    return timestamp.tzinfo is not None and timedelta(0) <= now - timestamp <= lifetime


def free_registry() -> dict[str, FreeModel]:
    """Dated candidate evidence, never a production quality qualification."""
    verified = datetime(2026, 10, 4, tzinfo=UTC)
    text = {Capability.REASONING_STRONG, Capability.REASONING_FAST, Capability.JSON}
    vision = text | {Capability.IMAGE, Capability.VIDEO_MULTIFRAME, Capability.GROUNDING,
                     Capability.JAPANESE_READING, Capability.RENDER_QA}
    rows: list[FreeModel] = []

    def add(provider, model, base, caps, refs, *, images=0, quota=None, available=True):
        operation = "embeddings" if Capability.EMBEDDING in caps else (
            "audio/transcriptions" if Capability.ASR_SEGMENTS in caps else "chat/completions"
        )
        if provider == "gemini":
            operation = f"models/{model}:generateContent"
        rows.append(FreeModel(
            provider=provider, model=model, base_url=base,
            operations=frozenset({operation}), capabilities=frozenset(caps),
            cost_class="FREE_ENDPOINT" if provider == "nvidia" else "FREE_TIER",
            verified_at=verified, verification_references=tuple(refs),
            quota_snapshot=quota or {}, max_images=images, available=available,
            data_use="Public workflow data only; account terms and retention must be reviewed. "
                     "Gemini Free content may be used for product improvement.",
        ))

    nim = "https://integrate.api.nvidia.com/v1"
    for model, caps, images, ref in [
        ("z-ai/glm-5.3", text, 0, "https://build.nvidia.com/z-ai/glm-5-3"),
        ("z-ai/glm-5.3-flash", vision, 1, "https://build.nvidia.com/z-ai/glm-5-3-flash"),
        ("nvidia/nemotron-3-embed-1b", {Capability.EMBEDDING}, 0,
         "https://build.nvidia.com/nvidia/nemotron-3-embed-1b"),
        ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", vision | {Capability.VIDEO_NATIVE}, 1,
         "https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"),
        ("nvidia/cosmos3-nano-reasoner", vision | {Capability.VIDEO_NATIVE}, 1,
         "https://build.nvidia.com/nvidia/cosmos3-nano-reasoner"),
    ]:
        add("nvidia", model, nim, caps, [ref], images=images)
    # Current public page redirects to experience-unavailable. Do not infer access.
    add("nvidia", "meta/llama-3.2-90b-vision-instruct", nim, vision,
        ["https://build.nvidia.com/meta/llama-3.2-90b-vision-instruct"], images=1, available=False)
    groq = "https://api.groq.com/openai/v1"
    for model in ("whisper-large-v3", "whisper-large-v3-turbo"):
        add("groq", model, groq, {Capability.ASR_SEGMENTS, Capability.ASR_WORDS},
            ["https://console.groq.com/docs/rate-limits"],
            quota={"requests": 2000, "audio_seconds": 28800})
    add("groq", "openai/gpt-oss-120b", groq, text,
        ["https://console.groq.com/docs/rate-limits"], quota={"requests": 1000, "tokens": 200000})
    add("groq", "qwen/qwen3.8-27b", groq, vision,
        ["https://console.groq.com/docs/vision", "https://console.groq.com/docs/rate-limits"],
        images=3, quota={"requests": 1000, "tokens": 200000})
    for model in ("gemini-3.8-flash", "gemini-3.5-flash-lite"):
        add("gemini", model, "https://generativelanguage.googleapis.com/v1beta",
            vision | {Capability.VIDEO_NATIVE}, ["https://ai.google.dev/gemini-api/docs/pricing"],
            images=16)
    for model in ("mistral-medium-3-5", "mistral-small-2603"):
        add("mistral", model, "https://api.mistral.ai/v1", vision,
            ["https://docs.mistral.ai/admin/billing-usage/usage-limits"], images=8)
    for model in ("gpt-oss-120b", "qwen-3.8-27b"):
        add("cerebras", model, "https://api.cerebras.ai/v1", text,
            ["https://inference-docs.cerebras.ai/models/overview"], available=False)
    # Dynamic free-model/Cloudflare account paths are added by verified discovery;
    # HF credits and SambaNova payment-required paths have no automatic entry.
    return {row.identity: row for row in rows}


class QuotaLedger:
    """Atomic, cross-process, account-scoped UTC daily reservations.

    Failed/uncertain requests retain their reservation. Restart never refunds a
    potentially accepted request. Reservations precede network I/O.
    """
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, unit TEXT, "
                       "amount REAL NOT NULL, PRIMARY KEY(account, day, unit))")
            db.execute("CREATE TABLE IF NOT EXISTS blocks (account TEXT PRIMARY KEY, until REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, account TEXT, "
                       "model TEXT, at TEXT, estimate TEXT, status TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, account: str, model: str, limits: dict, estimate: dict, now: datetime) -> int:
        if not estimate or estimate.get("requests") != 1:
            raise CostPolicyError("Every attempt must reserve one request")
        for unit, amount in estimate.items():
            if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                raise CostPolicyError("Invalid quota estimate")
            if unit not in limits or not math.isfinite(limits[unit]) or limits[unit] <= 0:
                raise CostPolicyError(f"No verified budget for {unit}")
        day = now.date().isoformat()
        reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            block = db.execute("SELECT until FROM blocks WHERE account=?", (account,)).fetchone()
            if block and block[0] > now.timestamp():
                raise QuotaBlocked(block[0] - now.timestamp())
            for unit, amount in estimate.items():
                row = db.execute("SELECT amount FROM usage WHERE account=? AND day=? AND unit=?",
                                 (account, day, unit)).fetchone()
                if (row[0] if row else 0) + amount > limits[unit]:
                    raise QuotaBlocked((reset - now).total_seconds())
            for unit, amount in estimate.items():
                db.execute("INSERT INTO usage VALUES (?,?,?,?) ON CONFLICT(account,day,unit) "
                           "DO UPDATE SET amount=amount+excluded.amount", (account, day, unit, amount))
            cursor = db.execute("INSERT INTO requests(account,model,at,estimate,status) "
                                "VALUES (?,?,?,?,?)", (account, model, now.isoformat(),
                                                       json.dumps(estimate), "reserved"))
            return cursor.lastrowid

    def finish(self, request_id: int, status: str):
        with self.connect() as db:
            db.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))

    def block(self, account: str, now: datetime, retry_after: float | None):
        delay = retry_after if retry_after is not None and math.isfinite(retry_after) else 86400
        with self.connect() as db:
            db.execute("INSERT INTO blocks VALUES (?,?) ON CONFLICT(account) DO UPDATE SET "
                       "until=max(until,excluded.until)", (account, now.timestamp() + max(1, delay)))


class CostGuard:
    def __init__(self, ledger: QuotaLedger, accounts: list[AccountEvidence], *, registry=None,
                 clock=None):
        self.ledger = ledger
        self.accounts = accounts
        self.registry = free_registry() if registry is None else registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self.policy = CostPolicy.ZERO_COST_ONLY

    @classmethod
    def from_env(cls):
        path = Path(os.getenv("HOLOCLIP_FREE_ACCOUNTS", "data/providers/free-accounts.json"))
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        accounts = [AccountEvidence.model_validate(row) for row in raw]
        return cls(QuotaLedger(Path(os.getenv("HOLOCLIP_QUOTA_LEDGER", "data/providers/quota.db"))),
                   accounts)

    def model(self, provider: str, model: str, base_url: str) -> FreeModel:
        entry = self.registry.get(f"{provider}:{model}")
        if entry is None or not entry.available:
            raise CostPolicyError("Unregistered or unavailable zero-cost model")
        entry = FreeModel.model_validate(entry.model_dump())
        # Exact equality excludes userinfo, ports, query/path tricks and paid partner hosts.
        if base_url != entry.base_url:
            raise CostPolicyError("Endpoint does not match verified free registry")
        if not _fresh(entry.verified_at, self.clock(), timedelta(days=7)):
            raise CostPolicyError("FREE_STATUS_STALE: reverify official free status")
        return entry

    def authorize(self, provider: str, model: str, base_url: str, key: str, *, public=True):
        entry = self.model(provider, model, base_url)
        if not public:
            raise CostPolicyError("Only public workflow data is eligible")
        account = next((a for a in self.accounts if a.provider == provider and
                        a.credential_sha256 == credential_fingerprint(key)), None)
        if account is None or not _fresh(account.verified_at, self.clock(), timedelta(hours=24)):
            raise CostPolicyError("Missing/stale credential-bound free account evidence")
        account = AccountEvidence.model_validate(account.model_dump())
        return entry, account

    async def post(self, client: httpx.AsyncClient, *, provider: str, model: str, base_url: str,
                   key: str, operation: str, estimate: dict, public=None, **kwargs):
        if public is None:
            public = os.getenv("HOLOCLIP_DATA_CLASS") == "public"
        entry, account = self.authorize(provider, model, base_url, key, public=public)
        if operation not in entry.operations:
            raise CostPolicyError("Operation is not verified zero-cost")
        body = kwargs.get("json", kwargs.get("data", {}))
        if isinstance(body, dict) and "model" in body and body["model"] != model:
            raise CostPolicyError("Payload model does not match authorized model")
        headers = kwargs.get("headers", {})
        auth = headers.get("Authorization", headers.get("authorization"))
        if auth is not None and auth != f"Bearer {key}":
            raise CostPolicyError("Payload credential does not match account evidence")
        limits = dict(account.daily_limits)
        for unit, maximum in entry.quota_snapshot.items():
            limits[unit] = min(limits.get(unit, maximum), maximum * 0.8)
        account_key = f"{provider}:{account.account_id}"
        reservation = self.ledger.reserve(account_key, model, limits, estimate, self.clock())
        try:
            response = await client.post(f"{base_url}/{operation}", follow_redirects=False, **kwargs)
        except BaseException:
            self.ledger.finish(reservation, "uncertain")
            raise
        self.ledger.finish(reservation, f"http_{response.status_code}")
        if 300 <= response.status_code < 400:
            raise CostPolicyError("Provider redirect rejected before following another endpoint")
        if response.status_code == 429:
            try:
                retry = float(response.headers["Retry-After"])
            except (ValueError, KeyError):
                retry = None
            self.ledger.block(account_key, self.clock(), retry)
        return response
