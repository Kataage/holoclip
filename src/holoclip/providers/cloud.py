"""Bounded HTTP adapters. They execute a route; they never choose a vendor."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from holoclip.execution import StageArtifactCache, make_input_fingerprint
from holoclip.providers.cost import CostGuard, CostPolicyError, QuotaBlocked
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
        headers = {"Authorization": f"Bearer {key}"}
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
            # Deliberate worst-case neuron reservation; exact observed usage is reported separately.
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
                    input_fingerprint=fingerprint, stage_version="cloud-v1", provider=entry.provider,
                    model=entry.model, prompt_version="cloud-json-v1")
        cached = self.cache.reusable_json(**args) if self.cache else None
        if cached is not None:
            return cached
        run = self.cache.start(**args) if self.cache else None
        try:
            response = await self.guard.post(self.client, provider=entry.provider, model=entry.model,
                base_url=entry.base_url, key=key, operation=operation, estimate=estimate,
                public=public, headers=headers, json=body)
            if response.status_code == 429:
                try:
                    delay = float(response.headers["Retry-After"])
                except (KeyError, ValueError):
                    delay = None
                raise QuotaBlocked(delay)
            response.raise_for_status()
            raw = response.json()
            if entry.provider == "gemini":
                value = "".join(p.get("text", "") for p in raw["candidates"][0]["content"]["parts"]
                                if not p.get("thought"))
            elif entry.provider == "cloudflare":
                value = raw["result"]["response"]
            else:
                value = raw["choices"][0]["message"]["content"]
            output = parse_object(value)
        except BaseException as exc:
            if run:
                from holoclip.execution import StageStatus
                self.cache.fail(run, status=StageStatus.FAILED_RETRYABLE, error=exc)
            raise
        if run:
            self.cache.succeed_json(run, output, kind="provider_structured_response")
        return output

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
