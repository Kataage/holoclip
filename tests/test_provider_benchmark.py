import json
from datetime import timedelta

import httpx
import pytest

from holoclip.execution import make_input_fingerprint
from holoclip.provider_benchmark import (
    Assessment, BenchmarkCase, BenchmarkManifest, BenchmarkRunner, VERSION, atomic_json,
    derive_qualifications, read_keys,
)
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import CostPolicyError, request_scope
from holoclip.providers.mesh import Capability, ExecutionMode, load_qualifications
from holoclip.video_measurement import checksum_file
from test_cost_policy import NOW


def text_case(**updates):
    return BenchmarkCase(id="c1", clip_id="clip1", task="text", source_url="https://youtu.be/public",
        teacher_reference="human-finished-clip", public_data=True, prompt='Return JSON {"ok":true}',
        dimensions={"japanese"}, **updates)


def report_fixture():
    manifest = BenchmarkManifest(cases=tuple(text_case().model_copy(update={
        "id": f"c{i}", "clip_id": f"clip{i // 6}", "prompt": f"Distinct corpus transcript {i}",
        "held_out": True, "real_media": True}) for i in range(30)))
    inputs = {c.id: make_input_fingerprint(c.prompt) for c in manifest.cases}
    dataset = make_input_fingerprint({"manifest": manifest.model_dump(mode="json"), "inputs": inputs})
    rows = [{"job_id": f"j{i}-{n}", "case_id": c.id, "clip_id": c.clip_id, "provider": "groq",
        "model": "openai/gpt-oss-120b", "mode": "text", "repeat": n,
        "dataset_sha256": dataset, "input_sha256": inputs[c.id], "verified_free_at": NOW.isoformat(),
        "at": NOW.isoformat(), "status": "SUCCEEDED", "structured_valid": True,
        "output": {"ok": True}, "latency_seconds": 1,
        "requests": [{"id": i*2+n, "status": "http_200", "reserved_units": {"requests": 1}, "observed_usage": {}}]}
        for i, c in enumerate(manifest.cases) for n in range(2)]
    for row in rows:
        row["job_id"] = make_input_fingerprint({"dataset": dataset, "window": row["input_sha256"],
            "case": row["case_id"], "entry": "groq:openai/gpt-oss-120b", "mode": "text", "repeat": row["repeat"]})
    assessments = [Assessment(result_sha256=make_input_fingerprint(r), capability=Capability.REASONING_STRONG,
        quality_score=.95, hallucination_rate=0, error_rate=0, critical_hallucinations=0,
        dimensions={"japanese"}, method="HUMAN_GOLD", reviewer="fixture-reviewer",
        gold_reference="fixture reviewed teacher", consistency_facts={f"{r['case_id']}:canonical-payoff"}
        ).model_dump(mode="json") for r in rows]
    return {"version": VERSION, "dataset_sha256": dataset, "manifest": manifest.model_dump(mode="json"),
        "input_identities": inputs, "results": rows, "assessments": assessments, "generated_at": NOW.isoformat()}


def test_holdout_original_clip_identity_cannot_leak():
    with pytest.raises(ValueError, match="Held-out"):
        BenchmarkManifest(cases=(text_case(held_out=True),), training_clip_ids={"clip1"})


def test_free_registry_is_valid_during_jst_audit_before_utc_midnight(free_cost_guard):
    free_cost_guard.clock = lambda: NOW.replace(day=3, hour=23)
    entry = free_cost_guard.registry["groq:openai/gpt-oss-120b"]
    assert free_cost_guard.model(entry.provider, entry.model, entry.base_url) == entry


async def test_job_scoped_telemetry_excludes_secrets_and_content(free_cost_guard):
    def handler(request):
        return httpx.Response(200, json={"usage": {"total_tokens": 17, "secret": "never store"},
            "choices": [{"message": {"content": '{"ok":true}'}}]},
            headers={"x-ratelimit-remaining-tokens": "100", "authorization": "secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = request_scope.set("job-a")
        try:
            await free_cost_guard.post(client, provider="groq", model="openai/gpt-oss-120b",
                base_url="https://api.groq.com/openai/v1", key="test-key", operation="chat/completions",
                estimate={"requests": 1, "tokens": 10}, public=True,
                json={"model": "openai/gpt-oss-120b"})
        finally:
            request_scope.reset(token)
    row = free_cost_guard.ledger.telemetry("job-a")[0]
    assert row["observed_usage"] == {"total_tokens": 17}
    assert row["rate_headers"] == {"x-ratelimit-remaining-tokens": "100"}
    assert free_cost_guard.ledger.telemetry("job-b") == []


async def test_run_resumes_success_and_missing_keys_without_paid_requests(tmp_path, free_cost_guard):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapters = CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=client)
        runner = BenchmarkRunner(free_cost_guard, {"groq": "test-key"}, tmp_path, adapters=adapters)
        manifest = BenchmarkManifest(cases=(text_case(),), repeats=2)
        try:
            first = await runner.run(manifest, only={"groq:openai/gpt-oss-120b", "nvidia:z-ai/glm-5.3"})
            second = await runner.run(manifest, only={"groq:openai/gpt-oss-120b", "nvidia:z-ai/glm-5.3"})
        finally:
            await runner.aclose()
    assert calls.count("POST") == 2
    for path in (first, second):
        report = json.loads(path.read_text())
        assert report["qualification_status"] == "PENDING_GOLD"
        assert len([r for r in report["results"] if r["status"] == "SUCCEEDED"]) == 2
        assert all(r["paid_fallback_allowed"] is False for r in report["results"])


async def test_quota_preserves_cursor_until_reset(tmp_path, free_cost_guard):
    calls = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, headers={"Retry-After": "60"},
            json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = BenchmarkRunner(free_cost_guard, {"groq": "test-key"}, tmp_path,
            adapters=CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=client))
        manifest = BenchmarkManifest(cases=(text_case(),), repeats=1)
        try:
            first = await runner.run(manifest, only={"groq:openai/gpt-oss-120b"})
            assert json.loads(first.read_text())["results"][0]["status"] == "BLOCKED_QUOTA"
            await runner.run(manifest, only={"groq:openai/gpt-oss-120b"})
            assert len(calls) == 1
            free_cost_guard.clock = lambda: NOW + timedelta(days=1)
            free_cost_guard.accounts = [a.model_copy(update={"verified_at": free_cost_guard.clock()}) for a in free_cost_guard.accounts]
            resumed = await runner.run(manifest, only={"groq:openai/gpt-oss-120b"})
            assert json.loads(resumed.read_text())["results"][0]["status"] == "SUCCEEDED"
        finally:
            await runner.aclose()
    assert len(calls) == 2


def test_weak_teacher_cannot_qualify():
    report = report_fixture()
    for a in report["assessments"]:
        a["method"] = "AUTO_WEAK_TEACHER"
    assert derive_qualifications(report, "a" * 64) == []


def test_same_quality_scores_cannot_mask_inconsistent_semantic_facts():
    report = report_fixture()
    for a in report["assessments"][1::2]:
        a["consistency_facts"] = ["opposite-payoff-or-subject"]
    scores = derive_qualifications(report, "a" * 64)
    assert scores and scores[0].consistency == 0 and not scores[0].qualified


def test_old_reviewed_scores_without_fact_evidence_cannot_promote():
    report = report_fixture()
    for a in report["assessments"]:
        a.pop("consistency_facts")
    assert not any(q.qualified for q in derive_qualifications(report, "a" * 64))


def test_dimension_repeats_do_not_count_as_distinct_gold_observations():
    report = report_fixture()
    special = "small_ui_event"
    for case in report["manifest"]["cases"]:
        case["dimensions"].append(special)
    report["manifest"] = BenchmarkManifest.model_validate(report["manifest"]).model_dump(mode="json")
    report["dataset_sha256"] = make_input_fingerprint({"manifest": report["manifest"], "inputs": report["input_identities"]})
    for row in report["results"]:
        row["dataset_sha256"] = report["dataset_sha256"]
        row["job_id"] = make_input_fingerprint({"dataset": report["dataset_sha256"], "window": row["input_sha256"],
            "case": row["case_id"], "entry": "groq:openai/gpt-oss-120b", "mode": "text", "repeat": row["repeat"]})
    for index, assessment in enumerate(report["assessments"]):
        assessment["result_sha256"] = make_input_fingerprint(report["results"][index])
        if index < 4:  # Two distinct cases, four repeats: insufficient dimension coverage.
            assessment["dimensions"].append(special)
    score = derive_qualifications(report, "a" * 64)[0]
    assert special not in score.covered_dimensions and "japanese" in score.covered_dimensions


@pytest.mark.parametrize("change", ["missing_gold", "hallucination", "low_quality", "not_real", "single_repeat", "failure"])
def test_quality_floor_never_relaxed(change):
    report = report_fixture()
    if change == "missing_gold":
        report["assessments"].pop()
    elif change == "hallucination":
        report["assessments"][0]["critical_hallucinations"] = 1
    elif change == "low_quality":
        report["assessments"][0]["quality_score"] = .79
    elif change == "not_real":
        for c in report["manifest"]["cases"]:
            c["real_media"] = False
        report["dataset_sha256"] = make_input_fingerprint({"manifest": report["manifest"], "inputs": report["input_identities"]})
        for r in report["results"]:
            r["dataset_sha256"] = report["dataset_sha256"]
            r["job_id"] = make_input_fingerprint({"dataset": report["dataset_sha256"], "window": r["input_sha256"],
                "case": r["case_id"], "entry": "groq:openai/gpt-oss-120b", "mode": "text", "repeat": r["repeat"]})
        report["assessments"] = []
    elif change == "single_repeat":
        report["assessments"] = report["assessments"][::2]
    else:
        report["results"][0]["status"] = "TIMEOUT"
        report["assessments"].pop(0)
    assert not any(q.qualified for q in derive_qualifications(report, "a" * 64))


def test_promotion_recomputes_and_pins_report(tmp_path):
    report = report_fixture()
    path = tmp_path / "report.json"
    atomic_json(path, report)
    sha = checksum_file(path)
    quals = derive_qualifications(report, sha)
    assert len(quals) == 1 and quals[0].qualified
    assert quals[0].capability == Capability.REASONING_STRONG
    assert quals[0].mode == ExecutionMode.TEXT
    bundle = tmp_path / "qualifications.json"
    raw = {"version": VERSION, "report_path": "report.json", "report_sha256": sha,
           "qualifications": [q.model_dump(mode="json") for q in quals]}
    atomic_json(bundle, raw)
    assert load_qualifications(bundle) == quals
    raw["qualifications"][0]["quality_score"] = 1
    atomic_json(bundle, raw)
    with pytest.raises(CostPolicyError, match="recomputed"):
        load_qualifications(bundle)
    path.write_text('{}')
    with pytest.raises(CostPolicyError, match="changed"):
        load_qualifications(bundle)


def test_changed_and_duplicate_assessments_rejected():
    report = report_fixture()
    report["assessments"][0]["result_sha256"] = "a" * 64
    with pytest.raises(CostPolicyError, match="changed/missing"):
        derive_qualifications(report, "b" * 64)
    report = report_fixture()
    report["assessments"].append(report["assessments"][0])
    with pytest.raises(CostPolicyError, match="Duplicate assessment"):
        derive_qualifications(report, "b" * 64)


def test_env_parser_does_not_execute_or_replace_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "actual-key")
    path = tmp_path / ".env"
    path.write_text('GROQ_API_KEY="$(do-not-execute)"\nNVIDIA_API_KEY=fixture\nUNRELATED=secret')
    assert read_keys(path) == {"groq": "actual-key", "nvidia": "fixture"}
