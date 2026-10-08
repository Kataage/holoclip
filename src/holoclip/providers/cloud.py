"""Bounded HTTP adapters. They execute a route; they never choose a vendor."""
from __future__ import annotations

import base64
import json
import asyncio
import re
from pathlib import Path
from uuid import uuid4

import httpx

from holoclip.execution import StageArtifactCache, make_input_fingerprint
from holoclip.providers.cost import CostGuard, CostPolicyError, QuotaBlocked, reset_delay, quota_error_metadata
from holoclip.providers.mesh import Route


def data_uri(path: Path, mime: str, maximum: int) -> str:
    if path.stat().st_size > maximum:
        raise CostPolicyError("Bounded media exceeds verified provider input limit")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def parse_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("No structured response content")
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    # Do not silently extract arbitrary prose surrounded by a valid object.
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Structured response must be an object")
    return parsed


class CloudAdapters:
    def __init__(self, guard: CostGuard, keys: dict[str, str], *, client=None,
                 cache: StageArtifactCache | None = None):
        self.guard = guard
        self.keys = keys
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self.owns_client = client is None
        self.cache = cache
        with guard.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pending_responses (fingerprint TEXT PRIMARY KEY, "
                       "identity TEXT, request_id TEXT, reservation_id INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS poll_reads (reservation_id INTEGER, at TEXT, status INTEGER)")

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()

    async def call_json(self, route: Route, prompt: str, *, images: list[Path] | None = None,
                        video: Path | None = None, max_tokens=2048, public=False,
                        cache_identity=None) -> dict:
        entry = route.entry
        images = images or []
        if len(images) > entry.max_images:
            raise CostPolicyError("Too many images; split measured work or use qualified contact sheets")
        if video and entry.provider not in {"gemini", "nvidia"}:
            raise CostPolicyError("Provider has no verified native-video schema")
        if video and entry.model == "nvidia/cosmos3-nano-reasoner":
            raise CostPolicyError("Cosmos hosted video schema remains unverified; diagnostic probe blocked")
        media = [(data_uri(p, "image/png", entry.max_bytes), "image/png") for p in images]
        if video:
            media.append((data_uri(video, "video/mp4", entry.max_bytes), "video/mp4"))
        uploaded = sum(p.stat().st_size for p in images) + (video.stat().st_size if video else 0)
        if uploaded > entry.max_bytes:
            raise CostPolicyError("Total media exceeds provider input budget")
        key = self.keys.get(entry.provider, "")
        # Conservative UTF-8 text estimate plus model-specific image/video allowance.
        estimate = {"requests": 1, "tokens": len(prompt.encode("utf-8")) + max_tokens}
        if images:
            estimate["tokens"] += len(images) * (2048 if entry.provider == "groq" else 8192)
        if video:
            # Bounded-unit duration is supplied by the measurement layer; no guessed full-stream cost.
            seconds = (cache_identity or {}).get("duration_seconds")
            if not isinstance(seconds, (int, float)) or not 0 < seconds <= entry.max_video_seconds:
                raise CostPolicyError("Native video needs measured duration within provider limit")
            estimate["video_seconds"] = seconds
            estimate["tokens"] += seconds * 2048
        if uploaded:
            estimate["uploaded_bytes"] = uploaded
        if entry.max_request_tokens is not None and estimate["tokens"] > entry.max_request_tokens:
            raise CostPolicyError("Input cannot fit verified free per-request token budget; no truncation allowed")
        headers = {"Authorization": f"Bearer {key}"}
        if entry.provider == "nvidia":
            headers["NVCF-POLL-SECONDS"] = "5"
        if entry.provider == "gemini":
            headers = {"x-goog-api-key": key}
            parts = [{"text": prompt}] + [{"inline_data": {"mime_type": mime,
                     "data": uri.split(",", 1)[1]}} for uri, mime in media]
            body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {
                "temperature": 0, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}}
            operation = f"models/{entry.model}:generateContent"
        elif entry.provider == "cloudflare":
            # REST vision input accepts one base64 image; contact sheets retain tile IDs.
            body = {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0}
            if images:
                body["image"] = media[0][0].split(",", 1)[1]
            operation = entry.model
            # Planning estimate, not an authoritative upper bound. The verified
            # Free account's server-side hard cap is the monetary invariant.
            estimate["neurons"] = max_tokens * 0.2 + estimate["tokens"] * 0.1
        else:
            content = [{"type": "text", "text": prompt}]
            content += [{"type": "video_url" if mime == "video/mp4" else "image_url",
                        "video_url" if mime == "video/mp4" else "image_url": {"url": uri}}
                        for uri, mime in media]
            body = {"model": entry.model, "messages": [{"role": "user", "content": content
                    if media else prompt}], "temperature": 0, "max_tokens": max_tokens,
                    "stream": False}
            if entry.provider in {"groq", "mistral", "cerebras"}:
                body["response_format"] = {"type": "json_object"}
            if entry.provider == "openrouter":
                if not entry.model.endswith(":free"):
                    raise CostPolicyError("OpenRouter model must be an exact verified :free route")
                body["provider"] = {"allow_fallbacks": False, "max_price": {"prompt": 0, "completion": 0}}
            operation = "chat/completions"
        fingerprint = make_input_fingerprint({"model": entry.identity, "mode": route.mode,
                                              "prompt": prompt, "media": media,
                                              "measurement": cache_identity, "max_tokens": max_tokens})
        args = dict(stage="provider_response", subject_type="inference_unit", subject_id=fingerprint,
                    input_fingerprint=fingerprint, stage_version="cloud-v2", provider=entry.provider,
                    model=entry.model, prompt_version="cloud-json-v1")
        # Even a cached raw observation must have current no-charge entitlement.
        self.guard.authorize(entry.provider, entry.model, entry.base_url, key, public=public)
        cached = self.cache.reusable_json(**args) if self.cache else None
        if cached is not None:
            return cached
        holder = uuid4().hex
        if not self.guard.ledger.claim_inference(fingerprint, holder, self.guard.clock()):
            # Another worker already owns this exact unit. Deferring avoids a
            # second chargeable-looking attempt and lets quota-aware workers retry.
            raise QuotaBlocked(15)
        try:
            # Close the race with a worker that committed between the first
            # cache lookup and our acquisition of the atomic SQLite lease.
            cached = self.cache.reusable_json(**args) if self.cache else None
            if cached is not None:
                return cached
            run = self.cache.start(**args) if self.cache else None
            try:
                with self.guard.ledger.connect() as db:
                    pending = db.execute("SELECT identity,request_id,reservation_id FROM pending_responses "
                                         "WHERE fingerprint=?", (fingerprint,)).fetchone()
                if pending:
                    if pending[0] != entry.identity:
                        raise CostPolicyError("Pending request belongs to a different model")
                    response = await self.poll(route, pending[1], pending[2], public=public)
                else:
                    response = await self.guard.post(self.client, provider=entry.provider, model=entry.model,
                        base_url=entry.base_url, key=key, operation=operation, estimate=estimate,
                        public=public, headers=headers, json=body)
                    if response.status_code == 202:
                        request_id = response.headers.get("nvcf-reqid", "")
                        if entry.provider != "nvidia" or not re.fullmatch(r"[a-fA-F0-9-]{36}", request_id):
                            raise CostPolicyError("No verified asynchronous status request ID")
                        reservation = response.extensions["holoclip_reservation_id"]
                        with self.guard.ledger.connect() as db:
                            db.execute("INSERT OR REPLACE INTO pending_responses VALUES (?,?,?,?)",
                                       (fingerprint, entry.identity, request_id, reservation))
                        response = await self.poll(route, request_id, reservation, public=public)
                if response.status_code == 429:
                    if quota_error_metadata(response).get("request_too_large"):
                        raise CostPolicyError("Request exceeds actual Free TPM ceiling; waiting or paid upgrade cannot repair this unit")
                    delay = reset_delay(response.headers, self.guard.clock())
                    raise QuotaBlocked(delay)
                response.raise_for_status()
                raw = response.json()
                try:
                    if entry.provider == "gemini":
                        value = "".join(p.get("text", "") for p in raw["candidates"][0]["content"]["parts"]
                                        if not p.get("thought"))
                    elif entry.provider == "cloudflare":
                        value = raw["result"]["response"]
                    else:
                        value = raw["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("Provider response envelope is invalid") from exc
                output = parse_object(value)
            except BaseException as exc:
                if run:
                    from holoclip.execution import StageStatus
                    self.cache.fail(run, status=StageStatus.FAILED_RETRYABLE, error=exc)
                raise
            if run:
                self.cache.succeed_json(run, output, kind="provider_structured_response")
            with self.guard.ledger.connect() as db:
                db.execute("DELETE FROM pending_responses WHERE fingerprint=?", (fingerprint,))
            return output
        finally:
            self.guard.ledger.release_inference(fingerprint, holder)

    async def poll(self, route, request_id, reservation, *, public=False):
        # Official NIM status endpoint, same verified host. Never follow Location
        # or invoke a function/partner endpoint. GET resumes one accepted POST.
        from uuid import UUID
        entry = route.entry
        if entry.provider != "nvidia" or str(UUID(request_id)) != request_id.lower():
            raise CostPolicyError("Invalid asynchronous request ID")
        while True:
            self.guard.authorize(entry.provider, entry.model, entry.base_url,
                                 self.keys.get(entry.provider, ""), public=public)
            response = await self.client.get(entry.base_url + "/status/" + request_id,
                headers={"Authorization": f"Bearer {self.keys[entry.provider]}", "NVCF-POLL-SECONDS": "5"},
                follow_redirects=False)
            with self.guard.ledger.connect() as db:
                db.execute("INSERT INTO poll_reads VALUES (?,?,?)",
                           (reservation, self.guard.clock().isoformat(), response.status_code))
            if 300 <= response.status_code < 400:
                raise CostPolicyError("Asynchronous result redirect rejected")
            if response.status_code in {404, 410}:
                raise CostPolicyError("Accepted request expired; do not deprecate model from a status lookup")
            if response.status_code != 202:
                if response.status_code == 200:
                    self.guard.ledger.finish(reservation, "http_200")
                    self.guard.ledger.observe(reservation, response)
                return response
            await asyncio.sleep(1)

    async def discover_models(self, entry) -> tuple[set[str], bool]:
        self.guard.model(entry.provider, entry.model, entry.base_url)
        if entry.provider == "cloudflare":
            return set(), False  # Account-scoped discovery requires a separately verified read path.
        headers = ({"x-goog-api-key": self.keys.get(entry.provider, "")} if entry.provider == "gemini"
                   else {"Authorization": f"Bearer {self.keys.get(entry.provider, '')}"})
        response = await self.client.get(entry.base_url + "/models", headers=headers, follow_redirects=False)
        response.raise_for_status()
        raw = response.json()
        rows = raw.get("models", []) if entry.provider == "gemini" else raw.get("data", [])
        if not isinstance(rows, list) or not rows:
            return set(), False
        ids = {str(r.get("id", r.get("name", ""))).removeprefix("models/") for r in rows}
        return ids, not bool(raw.get("nextPageToken") or raw.get("has_more"))

    async def discovery_snapshot(self, *, timeout=30, providers=None):
        import asyncio
        from holoclip.providers.mesh import MeshState, ProviderState
        found = {}
        self.guard.ledger.reconcile_rate_resets(self.guard.clock())
        state = MeshState(self.guard)
        for provider in sorted(providers or {e.provider for e in self.guard.registry.values()}):
            entries = [e for e in self.guard.registry.values() if e.provider == provider and e.available]
            if not self.keys.get(provider) or not entries:
                found[provider] = {"state": "MISSING_CREDENTIALS_OR_FREE_CONTRACT"}
                continue
            try:
                self.guard.authorize(provider, entries[0].model, entries[0].base_url,
                                     self.keys[provider], public=True)
                async with asyncio.timeout(timeout):
                    ids, complete = await self.discover_models(entries[0])
                state.reconcile_models(provider, ids, complete=complete)
                for entry in entries:
                    if entry.model in ids and state.state(entry.identity) == ProviderState.MODEL_DEPRECATED:
                        state.record(entry.identity, ProviderState.VERIFIED_FREE)
                found[provider] = {"state": "DISCOVERED", "complete": complete, "ids": sorted(ids)}
            except (CostPolicyError, httpx.HTTPError, TimeoutError, ValueError) as exc:
                found[provider] = {"state": "DISCOVERY_FAILED", "error_class": type(exc).__name__}
        return found

    async def embeddings(self, route: Route, texts: list[str], *, input_type: str, public=False):
        entry = route.entry
        response = await self.guard.post(self.client, provider=entry.provider, model=entry.model,
            base_url=entry.base_url, key=self.keys.get(entry.provider, ""), operation="embeddings",
            public=public, estimate={"requests": 1, "tokens": sum(len(t.encode("utf-8")) for t in texts)},
            headers={"Authorization": f"Bearer {self.keys.get(entry.provider, '')}"},
            json={"model": entry.model, "input": texts, "input_type": input_type,
                  "encoding_format": "float", "truncate": "NONE"})
        if response.status_code == 429:
            raise QuotaBlocked()
        response.raise_for_status()
        raw = response.json()
        rows = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) or
                type(r.get("index")) is not int for r in rows):
            raise ValueError("Invalid embedding response envelope/index")
        rows = sorted(rows, key=lambda r: r["index"])
        if [r["index"] for r in rows] != list(range(len(texts))):
            raise ValueError("Embedding output count/index mismatch")
        import math
        vectors = [r.get("embedding") for r in rows]
        if not vectors or any(not isinstance(v, list) or not v or
                              not all(type(x) in (int, float) and math.isfinite(x) for x in v)
                              for v in vectors) or len({len(v) for v in vectors}) != 1:
            raise ValueError("Invalid embedding vectors")
        return {"vectors": vectors}

    async def transcribe(self, route: Route, path: Path, *, seconds: float, words=False, public=False):
        entry = route.entry
        if entry.provider != "groq" or not 0 < seconds <= 1200 or path.stat().st_size > entry.max_bytes:
            raise CostPolicyError("No verified bounded ASR transport")
        fields = [("timestamp_granularities[]", (None, "segment"))]
        if words:
            fields.append(("timestamp_granularities[]", (None, "word")))
        with path.open("rb") as handle:
            response = await self.guard.post(self.client, provider=entry.provider, model=entry.model,
                base_url=entry.base_url, key=self.keys.get(entry.provider, ""), operation="audio/transcriptions",
                public=public, estimate={"requests": 1, "audio_seconds": seconds,
                                        "uploaded_bytes": path.stat().st_size},
                headers={"Authorization": f"Bearer {self.keys.get(entry.provider, '')}"},
                data={"model": entry.model, "language": "ja", "response_format": "verbose_json",
                      "temperature": "0"},
                files=[("file", (path.name, handle, "audio/mpeg")), *fields])
        if response.status_code == 429:
            raise QuotaBlocked()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid ASR response envelope")
        for field in (["segments", "words"] if words else ["segments"]):
            rows = payload.get(field)
            if not isinstance(rows, list):
                raise ValueError("ASR lacks requested timestamp granularity")
            if any(not isinstance(row, dict) or type(row.get("start")) not in (int, float) or
                   type(row.get("end")) not in (int, float) or
                   not 0 <= row["start"] <= row["end"] <= seconds + .5 for row in rows):
                raise ValueError("ASR timestamps exceed measured media duration")
        return payload
