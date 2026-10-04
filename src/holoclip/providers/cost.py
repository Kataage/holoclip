"""Fail-closed monetary policy at the HTTP boundary, including legacy adapters.

Public free pricing and account entitlement are separate evidence. A successful
request is never evidence of either. No endpoint, billing or tier fallback exists.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

# Isolated job attribution also works when callers run independent stages concurrently.
request_scope: ContextVar[str] = ContextVar("holoclip_request_scope", default="")


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
    data_retention: str = "UNVERIFIED; public media only; review provider account terms"
    geographic_restrictions: str = "Provider/account dependent; no entitlement inferred"
    public_data_only: Literal[True] = True
    paid_fallback_allowed: Literal[False] = False
    max_images: int = 0
    max_bytes: int = 20_000_000
    max_video_seconds: float = 120
    max_request_tokens: int | None = Field(default=None, gt=0)
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
    # Same upper bound as contract evidence; daily resets must not inherently
    # require a new human attestation. Operators may choose a shorter interval.
    evidence_ttl_seconds: int = Field(default=604800, ge=60, le=604800)
    # Hard internal budgets, shared by every model and process for this account.
    daily_limits: dict[str, float]


def credential_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _fresh(timestamp: datetime, now: datetime, lifetime: timedelta) -> bool:
    return timestamp.tzinfo is not None and timedelta(0) <= now - timestamp <= lifetime


def free_registry() -> dict[str, FreeModel]:
    """Dated candidate evidence, never a production quality qualification."""
    # The audit was performed on October 4 JST, October 3 UTC. Never round a
    # local calendar date forward to UTC midnight: that creates future evidence.
    verified = datetime(2026, 10, 3, 22, tzinfo=UTC)
    text = {Capability.REASONING_STRONG, Capability.REASONING_FAST, Capability.JSON}
    vision = text | {Capability.IMAGE, Capability.VIDEO_MULTIFRAME, Capability.GROUNDING,
                     Capability.JAPANESE_READING, Capability.RENDER_QA}
    rows: list[FreeModel] = []

    def add(provider, model, base, caps, refs, *, images=0, quota=None, available=True, tokens=None):
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
            max_request_tokens=tokens,
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
        ["https://console.groq.com/docs/rate-limits"], quota={"requests": 1000, "tokens": 200000}, tokens=8000)
    add("groq", "qwen/qwen3.8-27b", groq, vision,
        ["https://console.groq.com/docs/vision", "https://console.groq.com/docs/rate-limits"],
        images=3, quota={"requests": 1000, "tokens": 200000}, tokens=8000)
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


def reset_delay(headers, now: datetime) -> float | None:
    value = headers.get("retry-after")
    if value:
        try:
            return max(1, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo:
                    return max(1, (date - now).total_seconds())
            except (TypeError, ValueError):
                pass
    delays = []
    for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        duration = headers.get(name, "")
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", duration)
        if parts and "".join(a + b for a, b in parts) == duration:
            delays.append(sum(float(n) * {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}[u]
                              for n, u in parts))
    return max(1, max(delays)) if delays else None


def quota_error_metadata(response) -> dict:
    """Only typed quota numbers/dimensions, never error prose or account IDs."""
    try:
        error = response.json().get("error", {})
        message = error.get("message", "")
    except (ValueError, AttributeError):
        return {}
    if not isinstance(message, str):
        return {}
    dimension = next((d for d in ("TPM", "TPD", "RPM", "RPD", "ASH", "ASD") if f"({d})" in message), None)
    values = {name.lower(): float(value) for name, value in re.findall(
        r"\b(Limit|Used|Requested):\s*(\d+(?:\.\d+)?)", message)}
    too_large = dimension == "TPM" and values.get("requested", 0) > values.get("limit", float("inf"))
    return {"dimension": dimension, "request_too_large": too_large,
            "numbers": values} if dimension else {}


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
            db.execute("CREATE TABLE IF NOT EXISTS request_observations (request_id INTEGER PRIMARY KEY, "
                       "scope TEXT, usage TEXT, rate_headers TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS request_errors (request_id INTEGER PRIMARY KEY, metadata TEXT)")

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
            db.execute("INSERT INTO request_observations VALUES (?,?,?,?)",
                       (cursor.lastrowid, request_scope.get(), "{}", "{}"))
            return cursor.lastrowid

    def observe(self, request_id: int, response: httpx.Response):
        # Store numeric usage only: never content, credentials or arbitrary headers.
        try:
            payload = response.json()
            raw = payload.get("usage", payload.get("usageMetadata", {}))
        except (ValueError, AttributeError):
            raw = {}
        usage = {k: v for k, v in raw.items() if isinstance(v, (int, float)) and
                 not isinstance(v, bool) and math.isfinite(v)} if isinstance(raw, dict) else {}
        headers = {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-") or
                   k in {"retry-after", "ratelimit-remaining", "ratelimit-reset"}}
        with self.connect() as db:
            db.execute("UPDATE request_observations SET usage=?,rate_headers=? WHERE request_id=?",
                       (json.dumps(usage), json.dumps(headers), request_id))
            db.execute("INSERT OR REPLACE INTO request_errors VALUES (?,?)",
                       (request_id, json.dumps(quota_error_metadata(response))))

    def telemetry(self, scope: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT r.id,r.account,r.model,r.at,r.estimate,r.status,o.usage,o.rate_headers "
                              "FROM requests r JOIN request_observations o ON o.request_id=r.id "
                              "WHERE o.scope=? ORDER BY r.id", (scope,)).fetchall()
            has_polls = db.execute("SELECT name FROM sqlite_master WHERE name='poll_reads'").fetchone()
            polls = {r[0]: db.execute("SELECT at,status FROM poll_reads WHERE reservation_id=?", (r[0],)).fetchall()
                     for r in rows} if has_polls else {}
            errors = {r[0]: db.execute("SELECT metadata FROM request_errors WHERE request_id=?", (r[0],)).fetchone()
                      for r in rows}
        return [{"id": r[0], "account": r[1], "model": r[2], "at": r[3],
                 "reserved_units": json.loads(r[4]), "status": r[5],
                 "poll_reads": [{"at": at, "status": status} for at, status in polls.get(r[0], [])],
                 "error_metadata": json.loads(errors[r[0]][0]) if errors[r[0]] else {},
                 "observed_usage": json.loads(r[6]), "rate_headers": json.loads(r[7])} for r in rows]

    def reconcile_rate_resets(self, now: datetime):
        """Repair only old unknown-429 24h blocks using retained server reset proof.

        No reservation is refunded. An explicit Retry-After or daily budget is
        never shortened. The original response remains available in telemetry.
        """
        with self.connect() as db:
            for account, until in db.execute("SELECT account,until FROM blocks").fetchall():
                row = db.execute("SELECT r.at,o.rate_headers FROM requests r JOIN request_observations o "
                    "ON o.request_id=r.id WHERE r.account=? AND r.status='http_429' ORDER BY r.id DESC LIMIT 1",
                    (account,)).fetchone()
                if not row:
                    continue
                at = datetime.fromisoformat(row[0])
                headers = json.loads(row[1])
                delay = reset_delay(headers, at)
                if (not headers.get("retry-after") and delay is not None and delay < 86400 and
                    86400 <= until - at.timestamp() < 86460):
                    db.execute("UPDATE blocks SET until=? WHERE account=?", (at.timestamp() + delay, account))

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
        if registry is None:
            for account in accounts:
                if account.provider != "cloudflare" or not re.fullmatch(r"[a-f0-9]{32}", account.account_id):
                    continue
                template = self.registry["groq:qwen/qwen3.8-27b"].model_dump()
                model = "@cf/meta/llama-3.2-11b-vision-instruct"
                base = f"https://api.cloudflare.com/client/v4/accounts/{account.account_id}/ai/run"
                entry = FreeModel.model_validate(template | {"provider": "cloudflare", "model": model,
                    "base_url": base, "operations": {model}, "max_images": 1,
                    "quota_snapshot": {"neurons": 10000}, "max_request_tokens": None,
                    "verification_references": (
                        "https://developers.cloudflare.com/workers-ai/platform/pricing/",
                        "https://developers.cloudflare.com/workers-ai/models/llama-3.2-11b-vision-instruct/"),
                    "data_use": "Public media only; https://developers.cloudflare.com/workers-ai/platform/data-usage/"})
                self.registry[entry.identity] = entry
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
        if account is None or not _fresh(account.verified_at, self.clock(),
                                         timedelta(seconds=account.evidence_ttl_seconds)):
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
        self.ledger.observe(reservation, response)
        if 300 <= response.status_code < 400:
            raise CostPolicyError("Provider redirect rejected before following another endpoint")
        if response.status_code == 429 and not quota_error_metadata(response).get("request_too_large"):
            retry = reset_delay(response.headers, self.clock())
            self.ledger.block(account_key, self.clock(), retry)
        response.extensions["holoclip_reservation_id"] = reservation
        return response
