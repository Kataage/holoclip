"""Production protocol adapters: quality-qualified capability routes only."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from holoclip.execution import StageArtifactCache
from holoclip.models import TranscriptSegment
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import Capability, CostGuard, CostPolicyError
from holoclip.providers.mesh import (
    CapabilityRouter, ExecutionMode, MeshDeferred, ModeRequirement, StageContract, load_qualifications,
)
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.video_measurement import checksum_file, probe_video


@dataclass(frozen=True)
class RoutingSettings:
    model: str = Capability.REASONING_STRONG.value
    fast_model: str = Capability.REASONING_FAST.value
    fast_reasoning_effort: str = "low"
    strong_reasoning_effort: str = "high"
    timeout_seconds: float = 60

    @classmethod
    def from_env(cls):
        return cls(timeout_seconds=min(120, max(1, float(os.getenv("HOLOCLIP_AI_TIMEOUT_SECONDS", "60")))))


class ProviderRuntime:
    def __init__(self, *, guard=None, keys=None, qualifications=None, client=None, cache=None):
        from holoclip.provider_benchmark import read_keys
        self.guard = guard or CostGuard.from_env()
        env_path = Path(os.getenv("HOLOCLIP_PROVIDER_ENV", ".env"))
        self.keys = keys if keys is not None else read_keys(env_path if env_path.is_file() else None)
        self.qualifications = qualifications if qualifications is not None else load_qualifications(
            Path(os.getenv("HOLOCLIP_QUALIFICATIONS", "data/providers/qualifications.json")))
        self.repository = None
        if cache is None:
            self.repository = SQLiteCanonicalRepository(Path(os.getenv("HOLOCLIP_PROVIDER_STAGES", "data/providers/stages.db")))
            cache = StageArtifactCache(self.repository, Path("data/providers/artifacts"))
        self.router = CapabilityRouter(self.guard, self.qualifications, self.keys, cache=cache)
        self.adapters = CloudAdapters(self.guard, self.keys, client=client, cache=cache)

    async def startup(self):
        # Reuse complete checked discovery for six hours; repeated chat calls do not
        # hammer model catalogs. Errors never invalidate all models.
        with self.guard.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS discovery_checks (provider TEXT PRIMARY KEY, at REAL)")
            checked = dict(db.execute("SELECT provider,at FROM discovery_checks").fetchall())
        pending = {p for p in self.keys if self.guard.clock().timestamp() - checked.get(p, 0) >= 21600}
        if not pending:
            return
        found = await self.adapters.discovery_snapshot(providers=pending)
        with self.guard.ledger.connect() as db:
            for provider, outcome in found.items():
                if outcome.get("state") == "DISCOVERED" and outcome.get("complete"):
                    db.execute("INSERT INTO discovery_checks VALUES (?,?) ON CONFLICT(provider) DO UPDATE SET at=excluded.at",
                               (provider, self.guard.clock().timestamp()))

    async def aclose(self):
        await self.adapters.aclose()
        if self.repository:
            self.repository.close()

    def contract(self, stage, mode, caps, *, timeout=60):
        return StageContract(stage=stage, alternatives=(ModeRequirement(mode=mode, required=caps),),
            dimensions={"japanese"}, public_data=os.getenv("HOLOCLIP_DATA_CLASS") == "public",
            quota_estimate={"requests": 1}, timeout_seconds=timeout)


class MeshLLMClient:
    """LLMProvider-compatible bridge; semantic labels replace vendor model order."""
    strict_output = True
    cache_provider = "capability_mesh"

    def __init__(self, settings=None, *, event_callback=None, runtime=None):
        self.settings = settings or RoutingSettings.from_env()
        self.runtime = runtime or ProviderRuntime()
        self.event_callback = event_callback
        self.last_provenance = None

    async def __aenter__(self):
        await self.runtime.startup()
        if not any(self.runtime.router.candidates(self.runtime.contract("production:readiness", ExecutionMode.TEXT,
            {cap, Capability.JSON})) for cap in (Capability.REASONING_STRONG, Capability.REASONING_FAST)):
            await self.runtime.aclose()
            raise MeshDeferred(["No qualified Japanese text/JSON route; diagnostics belong to providers run"])
        return self

    async def __aexit__(self, *_):
        await self.runtime.aclose()

    async def list_models(self):
        await self.runtime.startup()
        return {"data": [{"id": e.identity, "capabilities": sorted(e.capabilities),
                          "state": self.runtime.router.state.state(e.identity).value}
                         for e in self.runtime.guard.registry.values()]}

    async def chat_json(self, messages, *, model=None, temperature=.1, max_tokens=4096, reasoning_effort=None):
        if model in {None, self.settings.model, Capability.REASONING_STRONG.value}:
            cap = Capability.REASONING_STRONG
        elif model in {self.settings.fast_model, Capability.REASONING_FAST.value}:
            cap = Capability.REASONING_FAST
        else:
            raise CostPolicyError("Production calls select a capability, not an arbitrary model")
        contract = self.runtime.contract("production:" + cap.value, ExecutionMode.TEXT,
            {cap, Capability.JSON}, timeout=min(120, self.settings.timeout_seconds))
        # Serialize roles rather than folding untrusted user text into a system role.
        prompt = "Follow these role-labeled task messages and return only the requested JSON object:\n" + json.dumps(list(messages), ensure_ascii=False)
        async def invoke(route, payload):
            return await self.runtime.adapters.call_json(route, payload["prompt"], max_tokens=max_tokens,
                public=contract.public_data, cache_identity={"messages": payload["messages"]})
        result = await self.runtime.router.run(contract, {"prompt": prompt, "messages": list(messages),
            "max_tokens": max_tokens, "temperature": temperature, "reasoning_effort": reasoning_effort}, invoke)
        self.last_provenance = result["provenance"]
        if self.event_callback:
            self.event_callback(f"{cap}: {result['provenance']['provider']}:{result['provenance']['model']}")
        return result["output"]

    async def chat(self, messages, **kwargs):
        return json.dumps(await self.chat_json(messages, **kwargs), ensure_ascii=False)


class MeshEmbeddingClient:
    """Pin a qualified vector space; model changes require a separate index rebuild."""
    def __init__(self, settings=None, *, runtime=None):
        self.runtime = runtime or ProviderRuntime()
        self._identity = None
        self.last_provenance = None

    async def __aenter__(self):
        await self.runtime.startup()
        return self

    async def __aexit__(self, *_):
        await self.runtime.aclose()

    def contract(self):
        return self.runtime.contract("production:embedding", ExecutionMode.EMBEDDING, {Capability.EMBEDDING})

    @property
    def model(self):
        if self._identity is None:
            routes = self.runtime.router.candidates(self.contract())
            if not routes:
                raise MeshDeferred(["No qualified Japanese embedding vector space"])
            self._identity = routes[0].entry.identity
        # Namespaces prevent reuse/mixing with legacy and unrelated vector models.
        return "mesh:" + self._identity

    async def _embed(self, texts, input_type):
        texts = [t.strip() for t in texts]
        if not texts or any(not t for t in texts):
            raise ValueError("Embedding inputs must be nonempty")
        namespace = self.model
        contract = self.contract()
        eligible = [q for q in self.runtime.router.qualifications if f"{q.provider}:{q.model}" == self._identity]
        pinned = CapabilityRouter(self.runtime.guard, eligible, self.runtime.keys, cache=self.runtime.router.cache)
        async def invoke(route, payload):
            return await self.runtime.adapters.embeddings(route, payload["texts"], input_type=input_type,
                                                          public=contract.public_data)
        result = await pinned.run(contract, {"texts": texts, "input_type": input_type, "namespace": namespace}, invoke)
        self.last_provenance = result["provenance"]
        return result["output"]["vectors"]

    async def embed_passages(self, texts):
        return await self._embed(texts, "passage")

    async def embed_queries(self, texts):
        return await self._embed(texts, "query")

    async def embed_query(self, text):
        return (await self.embed_queries([text]))[0]


class MeshTranscriber:
    def __init__(self, settings=None, *, runtime=None):
        self.runtime = runtime or ProviderRuntime()
        self.last_provenance = None

    async def __aenter__(self):
        await self.runtime.startup()
        return self

    async def __aexit__(self, *_):
        await self.runtime.aclose()

    async def transcribe_observation(self, path, *, words=False):
        duration = float(probe_video(path)["format"]["duration"])
        caps = {Capability.ASR_SEGMENTS} | ({Capability.ASR_WORDS} if words else set())
        contract = self.runtime.contract("production:asr_words" if words else "production:asr_segments",
                                         ExecutionMode.AUDIO, caps)
        async def invoke(route, payload):
            return await self.runtime.adapters.transcribe(route, path, seconds=duration,
                                                          words=words, public=contract.public_data)
        result = await self.runtime.router.run(contract, {"media_sha256": checksum_file(path),
                                                         "duration": duration, "words": words}, invoke)
        self.last_provenance = result["provenance"]
        return result["output"]

    async def transcribe_file(self, path, *, offset_seconds=0):
        output = await self.transcribe_observation(path)
        return [TranscriptSegment(start=s["start"] + offset_seconds, end=s["end"] + offset_seconds,
                                  text=s["text"]) for s in output["segments"]]
