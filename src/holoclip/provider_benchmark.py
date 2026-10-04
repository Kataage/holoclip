"""Resumable real-media comparisons; transport success never grants qualification.

Gold assessments are separate from inference inputs. Automatic weak teachers are
useful for triage but cannot grant production access. All results are checksum bound.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from holoclip.execution import StageArtifactCache, make_input_fingerprint
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import (
    AccountEvidence, Capability, CostGuard, CostPolicyError, QuotaBlocked, QuotaLedger, request_scope,
)
from holoclip.providers.mesh import (
    CapabilityRouter, ExecutionMode, MeshDeferred, ProviderState, Qualification,
    QualityFloor, Route,
)
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.video_measurement import checksum_file, measure_window, probe_video
from holoclip.video_semantics import MeasuredVideoWindow, VideoUnderstandingProvider

VERSION = "holoclip-real-provider-v2"
app = typer.Typer(no_args_is_help=True)


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    clip_id: str
    task: Literal["video", "text", "asr", "embedding"]
    source_url: str
    teacher_reference: str
    public_data: Literal[True]
    real_media: bool = False
    held_out: bool = False
    dimensions: frozenset[str]
    measurement_path: str | None = None
    media_path: str | None = None
    media_sha256: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0, le=120)
    prompt: str = ""

    @field_serializer("dimensions")
    def sorted_dimensions(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def inputs_present(self):
        if self.task == "video" and not self.measurement_path:
            raise ValueError("Video case needs a measurement manifest")
        if self.task == "asr" and not (self.media_path and self.media_sha256 and self.duration_seconds):
            raise ValueError("ASR needs measured, bounded audio and its checksum")
        if self.task in {"text", "embedding"} and not self.prompt:
            raise ValueError("Text/embedding case needs actual corpus text")
        return self


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["holoclip-real-provider-v2"] = VERSION
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1)
    # Enforce the split explicitly, including original clip identity across windows.
    training_clip_ids: frozenset[str] = frozenset()
    repeats: int = Field(default=2, ge=1, le=5)
    timeout_seconds: float = Field(default=60, gt=0, le=120)

    @field_serializer("training_clip_ids")
    def sorted_training(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def split(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate benchmark case ID")
        if any(c.held_out and c.clip_id in self.training_clip_ids for c in self.cases):
            raise ValueError("Held-out clip is present in training")
        return self


class Assessment(BaseModel):
    """Small gold subset, assessed once per saved response and capability.

    A free independent judge may draft AUTO_WEAK_TEACHER rows. Promotion needs
    reviewed gold evidence; a model is never allowed to approve its own response.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability: Capability
    quality_score: float = Field(ge=0, le=1)
    hallucination_rate: float = Field(ge=0, le=1)
    error_rate: float = Field(ge=0, le=1)
    critical_hallucinations: int = Field(ge=0)
    dimensions: frozenset[str]
    method: Literal["HUMAN_GOLD", "HUMAN_REVIEWED_TEACHER", "AUTO_WEAK_TEACHER"]
    reviewer: str = Field(min_length=1)
    gold_reference: str = Field(min_length=1)
    # Reviewed canonical fact IDs, shared across provider/repeat wording. Score
    # stability alone cannot prove that payoff/identity/event claims agree.
    consistency_facts: frozenset[str] = frozenset()

    @field_serializer("dimensions", "consistency_facts")
    def sorted_sets(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def fact_ids(self):
        if any(not f.strip() or len(f) > 256 for f in self.consistency_facts):
            raise ValueError("Reviewed consistency facts need bounded non-empty IDs")
        return self


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_keys(env_file: Path | None = None) -> dict[str, str]:
    names = {p: p.upper() + "_API_KEY" for p in
             ("nvidia", "groq", "gemini", "mistral", "cerebras", "cloudflare", "openrouter")}
    values = dict(os.environ)
    if env_file:
        # Parse key assignments only. Never execute, interpolate, or print .env contents.
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip() in names.values():
                    values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return {p: values[n] for p, n in names.items() if values.get(n)}


def load_manifest(path: Path) -> BenchmarkManifest:
    manifest = BenchmarkManifest.model_validate_json(path.read_text(encoding="utf-8"))
    # Resolve paths against the manifest, never against the caller's cwd.
    cases = []
    for case in manifest.cases:
        changes = {}
        for name in ("measurement_path", "media_path"):
            value = getattr(case, name)
            if value:
                changes[name] = str((path.parent / value).resolve())
        cases.append(case.model_copy(update=changes))
    return manifest.model_copy(update={"cases": tuple(cases)})


def verified_window(case: BenchmarkCase) -> MeasuredVideoWindow:
    raw = json.loads(Path(case.measurement_path).read_text(encoding="utf-8"))
    window = MeasuredVideoWindow.model_validate(raw["window"])
    if checksum_file(Path(window.video_path)) != window.media_sha256 or any(
        checksum_file(Path(f.locator)) != f.checksum for f in window.frames
    ):
        raise CostPolicyError("Measured video/frame checksum changed")
    # Every sheet and its ID map must be pinned, including cached measurements.
    for name, checksum in raw.get("sheet_checksums", {}).items():
        if checksum_file(Path(name)) != checksum:
            raise CostPolicyError("Contact sheet or mapping checksum changed")
    if window.contact_sheet_paths and not raw.get("sheet_checksums"):
        raise CostPolicyError("Re-measure legacy manifest to pin contact sheets")
    return window


def modes_for(case: BenchmarkCase, entry) -> list[ExecutionMode]:
    if case.task == "video":
        modes = [ExecutionMode.NATIVE] if Capability.VIDEO_NATIVE in entry.capabilities else []
        if Capability.VIDEO_MULTIFRAME in entry.capabilities:
            modes += [ExecutionMode.MULTIFRAME, ExecutionMode.CONTACT_SHEET, ExecutionMode.FUSION]
        return modes
    cap, mode = {"text": (Capability.REASONING_STRONG, ExecutionMode.TEXT),
                 "asr": (Capability.ASR_SEGMENTS, ExecutionMode.AUDIO),
                 "embedding": (Capability.EMBEDDING, ExecutionMode.EMBEDDING)}[case.task]
    return [mode] if cap in entry.capabilities else []


class BenchmarkRunner:
    def __init__(self, guard: CostGuard, keys: dict[str, str], output: Path, *, adapters=None):
        self.guard, self.keys, self.output = guard, keys, output
        self.repository = SQLiteCanonicalRepository(output / "stages.db")
        self.cache = StageArtifactCache(self.repository, output / "artifacts")
        self.adapters = adapters or CloudAdapters(guard, keys, cache=self.cache)
        self.router = CapabilityRouter(guard, [], keys, cache=self.cache)

    async def aclose(self):
        await self.adapters.aclose()
        self.repository.close()

    async def discovery(self, timeout: float) -> dict:
        return await self.adapters.discovery_snapshot(timeout=timeout)

    async def invoke(self, case, entry, mode, job_id):
        route = Route(entry=entry, mode=mode, qualification=())
        if case.task == "video":
            return (await VideoUnderstandingProvider(self.router, self.adapters).understand(
                verified_window(case), diagnostic_identity=entry.identity, diagnostic_mode=mode.value,
                benchmark_run_id=job_id, public_data=case.public_data))["output"]
        if case.task == "text":
            return await self.adapters.call_json(route, case.prompt, public=True,
                                                 cache_identity={"benchmark_run_id": job_id})
        if case.task == "asr":
            path = Path(case.media_path)
            if checksum_file(path) != case.media_sha256 or path.stat().st_size > entry.max_bytes:
                raise CostPolicyError("ASR input checksum/limit mismatch")
            return await self.adapters.transcribe(route, path, seconds=case.duration_seconds,
                                                  words=True, public=case.public_data)
        values = (await self.adapters.embeddings(route, [case.prompt], input_type="passage",
                                                 public=case.public_data))["vectors"][0]
        return {"dimensions": len(values), "vector": values}

    async def run(self, manifest: BenchmarkManifest, *, only: frozenset[str] = frozenset(), progress=None,
                  resume_quota_only=False):
        input_ids = {c.id: make_input_fingerprint(verified_window(c).model_dump(mode="json"))
                     if c.task == "video" else c.media_sha256 or make_input_fingerprint(c.prompt)
                     for c in manifest.cases}
        dataset_sha = make_input_fingerprint({"manifest": manifest.model_dump(mode="json"), "inputs": input_ids})
        discovery = await self.discovery(manifest.timeout_seconds)
        results = []
        jobs = [(case, entry, mode, repeat) for case in manifest.cases
                for entry in self.guard.registry.values() if not only or entry.identity in only
                for mode in modes_for(case, entry) for repeat in range(manifest.repeats)]
        # Counterbalance provider order; bootstrap each execution mode before spending
        # the entire daily budget on one provider's dense-frame batches.
        order = {ExecutionMode.NATIVE: 0, ExecutionMode.CONTACT_SHEET: 1,
                 ExecutionMode.TEXT: 2, ExecutionMode.AUDIO: 3, ExecutionMode.EMBEDDING: 4,
                 ExecutionMode.MULTIFRAME: 5, ExecutionMode.FUSION: 6}
        jobs.sort(key=lambda j: (j[3], order[j[2]], j[0].id,
                                make_input_fingerprint(j[1].identity)))
        for case, entry, mode, repeat in jobs:
            window_sha = input_ids[case.id]
            job_id = make_input_fingerprint({"dataset": dataset_sha, "window": window_sha,
                "case": case.id, "entry": entry.identity, "mode": mode, "repeat": repeat})
            path = self.output / "results" / f"{job_id}.json"
            previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            now = self.guard.clock()
            if previous and (previous["status"] == "SUCCEEDED" or
                (resume_quota_only and previous["status"] != "BLOCKED_QUOTA") or
                previous.get("retry_at", 0) > now.timestamp()):
                results.append(previous)
                continue
            token = request_scope.set(job_id)
            started = asyncio.get_running_loop().time()
            result = {"job_id": job_id, "case_id": case.id, "clip_id": case.clip_id,
                "provider": entry.provider, "model": entry.model, "mode": mode.value,
                "repeat": repeat, "dataset_sha256": dataset_sha, "input_sha256": window_sha,
                "verified_free_at": entry.verified_at.isoformat(), "at": now.isoformat(),
                "cost_policy": "ZERO_COST_ONLY", "paid_fallback_allowed": False,
                "attempts": (previous or {}).get("attempts", 0) + 1}
            try:
                if not self.keys.get(entry.provider):
                    raise CostPolicyError("MISSING_CREDENTIALS")
                self.guard.authorize(entry.provider, entry.model, entry.base_url,
                                     self.keys[entry.provider], public=case.public_data)
                if self.router.state.state(entry.identity) == ProviderState.MODEL_DEPRECATED:
                    raise CostPolicyError("MODEL_DEPRECATED")
                async with asyncio.timeout(manifest.timeout_seconds):
                    output = await self.invoke(case, entry, mode, job_id)
                result.update(status="SUCCEEDED", structured_valid=True, output=output)
            except (QuotaBlocked, MeshDeferred) as exc:
                quota = isinstance(exc, QuotaBlocked) or exc.quota
                result.update(status="BLOCKED_QUOTA" if quota else "BLOCKED_DEPENDENCY",
                    error_class=type(exc).__name__, reason=str(exc), structured_valid=False,
                    retry_at=self.guard.clock().timestamp() + max(1, exc.retry_after or (86400 if quota else 300)))
            except (CostPolicyError, httpx.HTTPError, TimeoutError, ValueError, KeyError) as exc:
                result.update(status="BLOCKED_DEPENDENCY" if isinstance(exc, CostPolicyError)
                    else "TIMEOUT" if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                    else "FAILED", error_class=type(exc).__name__, structured_valid=False)
                if isinstance(exc, CostPolicyError):
                    result["reason"] = str(exc)
                if isinstance(exc, httpx.HTTPStatusError):
                    result["http_status"] = exc.response.status_code
            finally:
                request_scope.reset(token)
            result["latency_seconds"] = asyncio.get_running_loop().time() - started
            result["requests"] = self.guard.ledger.telemetry(job_id)
            history = list((previous or {}).get("attempt_history", []))
            history.append({"status": result["status"], "structured_valid": result["structured_valid"],
                            "latency_seconds": result["latency_seconds"],
                            "at": result["at"]})
            result["attempt_history"] = history
            result["first_started_at"] = (previous or {}).get("first_started_at", result["at"])
            result["wall_seconds"] = max(0, (self.guard.clock() - datetime.fromisoformat(result["first_started_at"])).total_seconds())
            # Keep historical attempts for audit; latest result is the resume cursor.
            atomic_json(path.with_name(f"{job_id}-attempt-{result['attempts']}.json"), result)
            atomic_json(path, result)
            results.append(result)
            if progress:
                progress(f"{case.id} {entry.identity} {mode} repeat={repeat}: {result['status']}")
        report = {"version": VERSION, "dataset_sha256": dataset_sha,
            "manifest": manifest.model_dump(mode="json"), "input_identities": input_ids,
            "discovery": discovery, "results": results,
            "assessments": [], "generated_at": self.guard.clock().isoformat(),
            "qualification_status": "PENDING_GOLD", "summary": summarize(results)}
        report_path = self.output / "reports" / f"{make_input_fingerprint(report)}.json"
        atomic_json(report_path, report)
        atomic_json(self.output / "latest.json", {"report_path": str(report_path.resolve()),
                                                  "report_sha256": checksum_file(report_path)})
        return report_path


def summarize(results: list[dict]) -> list[dict]:
    groups = {}
    for row in results:
        groups.setdefault((row["provider"], row["model"], row["mode"]), []).append(row)
    summary = []
    for (provider, model, mode), rows in groups.items():
        requests = [r for row in rows for r in row["requests"]]
        units = {}
        usage = {}
        for r in requests:
            for name, value in r["reserved_units"].items():
                units[name] = units.get(name, 0) + value
            for name, value in r["observed_usage"].items():
                usage[name] = usage.get(name, 0) + value
        histories = [h for r in rows for h in r.get("attempt_history", [{"status": r["status"], "latency_seconds": r["latency_seconds"]}])]
        # An eventual successful resume cannot erase earlier timeout/failure rate.
        operational = [h for h in histories if h["status"] != "BLOCKED_QUOTA"]
        latency = sorted(r.get("wall_seconds", r["latency_seconds"]) for r in rows)
        http_codes = {200, 202, 400, 401, 403, 404, 410, 429, 500, 502, 503, 504} | {
            int(r["status"].removeprefix("http_")) for r in requests
            if r["status"].startswith("http_") and r["status"].removeprefix("http_").isdigit()}
        summary.append({"provider": provider, "model": model, "mode": mode, "jobs": len(rows),
            "success_rate": sum(h["status"] == "SUCCEEDED" for h in operational) / max(1, len(operational)),
            "eventual_completion_rate": sum(r["status"] == "SUCCEEDED" for r in rows) / len(rows),
            "json_validity": sum(h.get("structured_valid", h["status"] == "SUCCEEDED")
                                 for h in operational) / max(1, len(operational)),
            "p50_latency": statistics.median(latency), "p95_latency": latency[math.ceil(len(rows) * .95) - 1],
            "timeouts": sum(h["status"] == "TIMEOUT" for h in histories),
            "quota_deferrals": sum(h["status"] == "BLOCKED_QUOTA" for h in histories),
            "http_counts": {str(code): sum(r["status"] == f"http_{code}" for r in requests)
                            for code in sorted(http_codes)},
            "poll_reads": sum(len(r.get("poll_reads", [])) for r in requests),
            "requests_per_clip": len(requests) / len({r["clip_id"] for r in rows}),
            "reserved_units": units, "observed_usage": usage,
            "quality_score": None, "clips_per_day": None,
            "capacity_status": "UNQUALIFIED; account remaining quota is not inferred from pricing"})
    return summary


def derive_qualifications(report: dict, report_sha: str) -> list[Qualification]:
    manifest = BenchmarkManifest.model_validate(report["manifest"])
    if report["version"] != VERSION or report["dataset_sha256"] != make_input_fingerprint({
        "manifest": manifest.model_dump(mode="json"), "inputs": report["input_identities"]}):
        raise CostPolicyError("Benchmark protocol/dataset fingerprint mismatch")
    cases = {c.id: c for c in manifest.cases}
    results = report["results"]
    identities = set()
    for row in results:
        key = (row["case_id"], row["provider"], row["model"], row["mode"], row["repeat"])
        if key in identities or row["case_id"] not in cases or not 0 <= row["repeat"] < manifest.repeats:
            raise CostPolicyError("Duplicate/unknown benchmark result")
        identities.add(key)
        if row["dataset_sha256"] != report["dataset_sha256"] or row["input_sha256"] != report["input_identities"][row["case_id"]]:
            raise CostPolicyError("Result input fingerprint mismatch")
        expected_job = make_input_fingerprint({"dataset": report["dataset_sha256"],
            "window": row["input_sha256"], "case": row["case_id"],
            "entry": f"{row['provider']}:{row['model']}", "mode": row["mode"], "repeat": row["repeat"]})
        if row["job_id"] != expected_job:
            raise CostPolicyError("Result job identity mismatch")
    hashes = {make_input_fingerprint(r): r for r in results}
    assessments = [Assessment.model_validate(a) for a in report["assessments"]]
    if any(a.result_sha256 not in hashes for a in assessments):
        raise CostPolicyError("Assessment references a changed/missing result")
    if len({(a.result_sha256, a.capability) for a in assessments}) != len(assessments):
        raise CostPolicyError("Duplicate assessment would bias qualification")
    output = []
    for summary in summarize(results):
        rows = [r for r in results if (r["provider"], r["model"], r["mode"]) ==
                (summary["provider"], summary["model"], summary["mode"])]
        by_hash = {make_input_fingerprint(r): r for r in rows}
        for cap in {a.capability for a in assessments if a.result_sha256 in by_hash}:
            gold = [a for a in assessments if a.result_sha256 in by_hash and a.capability == cap and
                    a.method != "AUTO_WEAK_TEACHER" and cases[by_hash[a.result_sha256]["case_id"]].held_out and
                    cases[by_hash[a.result_sha256]["case_id"]].real_media and
                    by_hash[a.result_sha256]["status"] == "SUCCEEDED"]
            if not gold:
                continue
            if any(not a.dimensions <= cases[by_hash[a.result_sha256]["case_id"]].dimensions for a in gold):
                raise CostPolicyError("Assessment claims an untested dimension")
            covered = frozenset(d for a in gold for d in a.dimensions if len({
                by_hash[b.result_sha256]["input_sha256"] for b in gold if d in b.dimensions}) >= 3)
            # Repeats must be genuine independently cached requests, not cloned result rows.
            repeated = {}
            for a in gold:
                row = by_hash[a.result_sha256]
                repeated.setdefault(row["case_id"], []).append((row["repeat"], a.quality_score, a.consistency_facts))
            consistency = sum(len({i for i, _, _ in values}) >= 2 and
                all(facts for _, _, facts in values) and len({facts for _, _, facts in values}) == 1 and
                max(q for _, q, _ in values) - min(q for _, q, _ in values) <= .1
                for values in repeated.values()) / len(repeated)
            expected_task = {"text": "text", "audio": "asr", "embedding": "embedding"}.get(summary["mode"], "video")
            expected_cases = [c for c in manifest.cases if c.task == expected_task]
            call_ids = [request["id"] for row in rows for request in row["requests"]]
            complete = (len(gold) == len(rows) and len(rows) == len(expected_cases) * manifest.repeats
                        and all(a.consistency_facts for a in gold)
                        and all(cases[r["case_id"]].held_out for r in rows)
                        and len({r["clip_id"] for r in rows}) >= 5
                        and all(any(req["status"] == "http_200" for req in r["requests"]) for r in rows)
                        and len(call_ids) == len(set(call_ids)))
            quality = min(a.quality_score for a in gold)  # weakest gold case, never vendor-wide inheritance
            q = Qualification(provider=summary["provider"], model=summary["model"], capability=cap,
                mode=summary["mode"], benchmark_version=VERSION, dataset_sha256=report["dataset_sha256"],
                report_sha256=report_sha, real_media=True, held_out=True,
                gold_cases=len({by_hash[a.result_sha256]["input_sha256"] for a in gold}), qualified=complete,
                quality_score=quality, hallucination_rate=max(a.hallucination_rate for a in gold),
                error_rate=max(a.error_rate for a in gold), critical_hallucinations=sum(a.critical_hallucinations for a in gold),
                success_rate=summary["success_rate"], json_validity=summary["json_validity"], consistency=consistency,
                p50_latency=summary["p50_latency"], p95_latency=summary["p95_latency"],
                quota_efficiency=1 / max(1, summary["requests_per_clip"]),
                verified_free_at=min(datetime.fromisoformat(r["verified_free_at"]) for r in rows),
                evaluated_at=report["generated_at"], covered_dimensions=covered,
                notes=("Checksum-bound reviewed holdout with semantic fact consistency; transport-only results cannot qualify",))
            if not q.clears(QualityFloor(), datetime.fromisoformat(report["generated_at"]), frozenset()):
                q = q.model_copy(update={"qualified": False})
            output.append(q)
    return output


def prepare_corpus(corpus_root: Path, output: Path, *, max_clips=3, window_seconds=20,
                   progress=None) -> Path:
    """Existing finished clips are weak teachers; never relabel training as gold.

    Acquisition and measurement are deterministic/non-AI. No private browser
    cookies, paid stream, or user .env mutation is involved.
    """
    from holoclip.clip_policy import PolicyDecision, evaluate_source_metadata
    from holoclip.providers.source.base import AcquisitionProfile
    from holoclip.providers.source.yt_dlp import YtDlpSourceProvider
    from holoclip.transcript import load_transcript
    if not 1 <= max_clips <= 20 or not 5 <= window_seconds <= 120:
        raise ValueError("Invalid bounded corpus preparation size")
    rows = [json.loads(line) for line in (corpus_root / "data/examples/human_clips.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: r.get("clip_duration_seconds") or float("inf"))
    source = YtDlpSourceProvider()
    cases, preparation, selected = [], [], 0
    for row in rows:
        if selected >= max_clips:
            break
        import re
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", row.get("clip_title", "")):
            preparation.append({"clip_id": row["id"], "status": "SKIPPED_NON_JAPANESE_TITLE"})
            continue
        try:
            metadata = source.probe(row["clip_url"])
            policy = evaluate_source_metadata(metadata)
            if policy.decision != PolicyDecision.ALLOW:
                preparation.append({"clip_id": row["id"], "status": "BLOCKED_POLICY"})
                continue
            acquired = source.acquire(row["clip_url"], output / "acquired", profile=AcquisitionProfile.EDITING_VIDEO)
            probe = probe_video(acquired.media_path)
            duration = float(probe["format"]["duration"])
            transcript = load_transcript(corpus_root / row["clip_transcript_path"]) if row.get("clip_transcript_path") else []
            starts = sorted({0., max(0., duration / 2 - window_seconds / 2), max(0., duration - window_seconds)})
            for n, start in enumerate(starts):
                end = min(duration, start + window_seconds)
                context = {f"t{i}": s.text for i, s in enumerate(transcript) if s.end > start and s.start < end}
                window = measure_window(acquired.media_path, output / "media", asset_id=row["id"],
                    start=start, end=end, transcript=context, dense_times=[(end - start) / 2])
                manifest_path = Path(window.video_path).parent / "measurement.json"
                common = dict(clip_id=row["id"], source_url=row["clip_url"], teacher_reference=
                    f"Human-finished clip, existing corpus {row['id']}; weak teacher, no held-out gold",
                    public_data=True, real_media=True, held_out=False,
                    dimensions=frozenset({"japanese"}))
                cases.append(BenchmarkCase(id=f"{row['id']}-{n}-video", task="video",
                    measurement_path=str(manifest_path.resolve()), **common))
                if context:
                    prompt = ("Understand this Japanese transcript from a public Hololive human-made clip. "
                        "Return only JSON {setup:string,development:string,payoff:string|null,"
                        "reaction:string|null,unknowns:[string]}. Do not invent missing payoff or visual actions.\n" +
                        json.dumps(context, ensure_ascii=False))
                    cases.append(BenchmarkCase(id=f"{row['id']}-{n}-text", task="text", prompt=prompt, **common))
                    cases.append(BenchmarkCase(id=f"{row['id']}-{n}-embedding", task="embedding",
                        prompt="\n".join(context.values()), **common))
                if any(s["codec_type"] == "audio" for s in probe["streams"]):
                    audio = Path(window.video_path).with_name("benchmark.wav")
                    if not audio.exists():
                        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", window.video_path,
                            "-vn", "-ar", "16000", "-ac", "1", str(audio)], check=True)
                    cases.append(BenchmarkCase(id=f"{row['id']}-{n}-asr", task="asr",
                        media_path=str(audio.resolve()), media_sha256=checksum_file(audio),
                        duration_seconds=float(probe_video(audio)["format"]["duration"]), **common))
            preparation.append({"clip_id": row["id"], "status": "PREPARED", "policy": policy.model_dump(mode="json"),
                "source": metadata.model_dump(mode="json"), "media_sha256": checksum_file(acquired.media_path)})
            selected += 1
        except Exception as exc:
            preparation.append({"clip_id": row["id"], "status": "FAILED", "error_class": type(exc).__name__})
        if progress:
            progress(f"prepare {row['id']}: {preparation[-1]['status']}")
    atomic_json(output / "preparation.json", preparation)
    if not cases:
        raise CostPolicyError("No public video corpus cases prepared; inspect preparation.json")
    manifest = BenchmarkManifest(cases=tuple(cases), training_clip_ids={r["id"] for r in rows})
    path = output / "manifest.json"
    atomic_json(path, manifest.model_dump(mode="json"))
    return path


@app.command("run")
def run_command(manifest: Path, accounts: Path = typer.Option(...),
                env_file: Path | None = None, output: Path = Path("data/provider-benchmark"),
                only: list[str] = typer.Option(None), wait_for_quota: bool = False):
    """Run/resume all modes and available free models with one command."""
    raw = json.loads(accounts.read_text(encoding="utf-8"))
    guard = CostGuard(QuotaLedger(output / "quota.db"), [AccountEvidence.model_validate(r) for r in raw])
    async def execute():
        runner = BenchmarkRunner(guard, read_keys(env_file), output)
        try:
            prepared = load_manifest(manifest)
            path = await runner.run(prepared, only=frozenset(only or []), progress=typer.echo)
            while wait_for_quota:
                report = json.loads(path.read_text(encoding="utf-8"))
                resets = [r["retry_at"] for r in report["results"] if r["status"] == "BLOCKED_QUOTA"]
                if not resets:
                    break
                reset = min(resets)
                while guard.clock().timestamp() < reset:
                    remaining = reset - guard.clock().timestamp()
                    typer.echo(f"BLOCKED_QUOTA: cached; automatic resume in {remaining:.0f}s")
                    await asyncio.sleep(min(60, remaining))
                path = await runner.run(prepared, only=frozenset(only or []), progress=typer.echo,
                                        resume_quota_only=True)
            return path
        finally:
            await runner.aclose()
    path = asyncio.run(execute())
    typer.echo(json.dumps({"report_path": str(path), "report_sha256": checksum_file(path),
                           "qualification_status": "PENDING_GOLD"}))


@app.command("qualify")
def qualify_command(report: Path, assessments: Path, output: Path):
    """Import one reviewed batch; bind and recompute every capability score."""
    raw = json.loads(report.read_text(encoding="utf-8"))
    raw["assessments"] = json.loads(assessments.read_text(encoding="utf-8"))
    raw["generated_at"] = datetime.now(UTC).isoformat()
    assessed = output.parent / "reports" / f"{make_input_fingerprint(raw)}.json"
    atomic_json(assessed, raw)
    quals = derive_qualifications(raw, checksum_file(assessed))
    atomic_json(output, {"version": VERSION, "report_path": str(assessed.resolve()),
        "report_sha256": checksum_file(assessed), "qualifications": [q.model_dump(mode="json") for q in quals]})
    typer.echo(json.dumps({"qualified_capabilities": sum(q.qualified for q in quals),
                           "output": str(output)}, ensure_ascii=False))


@app.command("corpus-run")
def corpus_run_command(corpus_root: Path, accounts: Path = typer.Option(...),
                       env_file: Path | None = None, output: Path = Path("data/provider-benchmark"),
                       max_clips: int = 3, wait_for_quota: bool = False):
    """Prepare human-made corpus videos and run/resume the comparison in one invocation."""
    manifest = prepare_corpus(corpus_root, output, max_clips=max_clips, progress=typer.echo)
    run_command(manifest, accounts=accounts, env_file=env_file, output=output, only=[], wait_for_quota=wait_for_quota)


@app.command("review-pack")
def review_pack_command(report: Path, output: Path, max_cases: int = 30):
    """Export a capped local blind sample; drafts never declare human review."""
    from holoclip.benchmark_review import review_pack
    typer.echo(json.dumps(review_pack(report, output, max_cases=max_cases)))


@app.command("draft-grades")
def draft_grades_command(report: Path, accounts: Path = typer.Option(...),
                         env_file: Path | None = None, output: Path = Path("data/provider-benchmark"),
                         wait_for_quota: bool = False):
    """Draft independent-vendor weak grades; never automatically qualify a provider."""
    from holoclip.benchmark_review import draft_grades, review_pack
    raw = json.loads(accounts.read_text(encoding="utf-8"))
    guard = CostGuard(QuotaLedger(output / "quota.db"), [AccountEvidence.model_validate(r) for r in raw])
    review = review_pack(report, output / "weak-grades" / "review")
    async def execute():
        runner = BenchmarkRunner(guard, read_keys(env_file), output)
        try:
            path = await draft_grades(runner, report, progress=typer.echo)
            while wait_for_quota:
                rows = json.loads(path.read_text(encoding="utf-8"))["results"]
                resets = [r["retry_at"] for r in rows if r["status"] == "BLOCKED_QUOTA"]
                if not resets:
                    break
                while guard.clock().timestamp() < min(resets):
                    remaining = min(resets) - guard.clock().timestamp()
                    typer.echo(f"BLOCKED_QUOTA: weak grades saved; automatic resume in {remaining:.0f}s")
                    await asyncio.sleep(min(60, remaining))
                path = await draft_grades(runner, report, progress=typer.echo, resume_quota_only=True)
            return path
        finally:
            await runner.aclose()
    path = asyncio.run(execute())
    typer.echo(json.dumps({"report_path": str(path), "report_sha256": checksum_file(path),
                           "qualification_allowed": False, "review_html": review["review_html"]}))
