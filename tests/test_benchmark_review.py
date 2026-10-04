import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from holoclip.benchmark_review import comparison_sheet, draft_grades, review_pack
from holoclip.execution import make_input_fingerprint
from holoclip.provider_benchmark import BenchmarkManifest, BenchmarkRunner, VERSION, atomic_json, derive_qualifications, summarize
from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import CostPolicyError
from test_cost_policy import NOW
from test_provider_benchmark import text_case


def report_file(tmp_path):
    manifest = BenchmarkManifest(cases=(text_case(real_media=True),), training_clip_ids={"clip1"}, repeats=1)
    inputs = {c.id: make_input_fingerprint(c.prompt) for c in manifest.cases}
    dataset = make_input_fingerprint({"manifest": manifest.model_dump(mode="json"), "inputs": inputs})
    row = {"case_id": "c1", "clip_id": "clip1", "provider": "groq", "model": "openai/gpt-oss-120b",
           "mode": "text", "repeat": 0, "dataset_sha256": dataset, "input_sha256": inputs["c1"],
           "verified_free_at": NOW.isoformat(), "at": NOW.isoformat(), "status": "SUCCEEDED",
           "structured_valid": True, "latency_seconds": 1, "output": {"ok": True},
           "requests": [{"id": 999, "status": "http_200", "reserved_units": {}, "observed_usage": {}}]}
    row["job_id"] = make_input_fingerprint({"dataset": dataset, "window": inputs["c1"],
        "case": "c1", "entry": "groq:openai/gpt-oss-120b", "mode": "text", "repeat": 0})
    path = tmp_path / "report.json"
    atomic_json(path, {"version": VERSION, "dataset_sha256": dataset,
        "manifest": manifest.model_dump(mode="json"), "input_identities": inputs,
        "results": [row], "assessments": [], "generated_at": NOW.isoformat()})
    return path


def grade(**updates):
    return {"quality_score": .9, "hallucination_rate": 0, "error_rate": 0,
            "critical_hallucinations": 0, "dimensions": ["japanese"], "evidence_ids": ["reference"],
            "explanation": "Supported fixture", "uncertainties": [], **updates}


def model_list(request):
    if request.method == "GET":
        return httpx.Response(200, json={"data": [{"id": m} for m in
            ("z-ai/glm-5.3", "z-ai/glm-5.3-flash", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning") ]})


def test_blind_pack_preserves_split_caps_and_never_claims_gold(tmp_path):
    path = report_file(tmp_path)
    raw = json.loads(path.read_text())
    raw["results"][0]["output"] = {"text": "</pre><script>bad</script>"}
    atomic_json(path, raw)
    out = tmp_path / "review"
    summary = review_pack(path, out, max_cases=1)
    pack = json.loads((out / "review-pack.json").read_text())
    assert summary["cases"] == 1 and summary["qualification_allowed"] is False
    assert pack["cases"][0]["held_out"] is False and pack["cases"][0]["training_clip"] is True
    assert pack["cases"][0]["gold_reference_draft"]["review_status"] == "UNREVIEWED"
    page = (out / "review.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert "gpt-oss-120b" not in page and "groq" not in page
    assert "groq" in (out / "identity-key.json").read_text()
    with pytest.raises(ValueError, match="capped"):
        review_pack(path, out, max_cases=31)


async def test_independent_weak_grades_cached_without_qualification(tmp_path, free_cost_guard):
    path = report_file(tmp_path)
    calls = []
    def handler(request):
        listed = model_list(request)
        if listed:
            return listed
        assert request.url.host == "integrate.api.nvidia.com"  # Candidate's Groq vendor is excluded.
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert "gpt-oss-120b" not in prompt and '"provider"' not in prompt
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(grade())}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = BenchmarkRunner(free_cost_guard, {"nvidia": "test-key", "groq": "test-key"}, tmp_path / "run",
            adapters=CloudAdapters(free_cost_guard, {"nvidia": "test-key", "groq": "test-key"}, client=client))
        try:
            result = await draft_grades(runner, path)
            assert result == await draft_grades(runner, path)
        finally:
            await runner.aclose()
    raw = json.loads(result.read_text())
    assert len(calls) == 1 and raw["qualification_allowed"] is False
    assert raw["assessments"][0]["method"] == "AUTO_WEAK_TEACHER"
    assessed = json.loads(path.read_text()) | {"assessments": raw["assessments"]}
    assert derive_qualifications(assessed, "a" * 64) == []


@pytest.mark.parametrize("updates", [{"method": "HUMAN_GOLD"}, {"evidence_ids": ["invented"]}, {"dimensions": ["game_ui"]}])
async def test_judge_cannot_promote_itself_or_invent_evidence(tmp_path, free_cost_guard, updates):
    def handler(request):
        return model_list(request) or httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(grade(**updates))}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = BenchmarkRunner(free_cost_guard, {"nvidia": "test-key"}, tmp_path / "run",
            adapters=CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=client))
        try:
            result = json.loads((await draft_grades(runner, report_file(tmp_path))).read_text())
        finally:
            await runner.aclose()
    assert result["results"][0]["status"] == "BLOCKED_DEPENDENCY" and not result["assessments"]


async def test_weak_judge_quota_preserves_cursor_and_resumes(tmp_path, free_cost_guard):
    calls = []
    def handler(request):
        listed = model_list(request)
        if listed:
            return listed
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, headers={"Retry-After": "1"},
            json={"choices": [{"message": {"content": json.dumps(grade())}}]})
    path = report_file(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = BenchmarkRunner(free_cost_guard, {"nvidia": "test-key"}, tmp_path / "run",
            adapters=CloudAdapters(free_cost_guard, {"nvidia": "test-key"}, client=client))
        try:
            first = json.loads((await draft_grades(runner, path)).read_text())
            assert first["results"][0]["status"] == "BLOCKED_QUOTA"
            await draft_grades(runner, path)
            assert len(calls) == 1
            free_cost_guard.clock = lambda: NOW + timedelta(seconds=2)
            resumed = json.loads((await draft_grades(runner, path, resume_quota_only=True)).read_text())
            assert resumed["results"][0]["status"] == "SUCCEEDED"
        finally:
            await runner.aclose()
    assert len(calls) == 2


def test_tampered_report_rejected_before_review(tmp_path):
    path = report_file(tmp_path)
    raw = json.loads(path.read_text())
    raw["results"][0]["input_sha256"] = "bad"
    atomic_json(path, raw)
    with pytest.raises(CostPolicyError, match="fingerprint"):
        review_pack(path, tmp_path / "review")


def test_comparison_sheet_preserves_pixels_without_downsampling(tmp_path):
    from PIL import Image
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (100, 60), "red").save(a)
    Image.new("RGB", (100, 40), "blue").save(b)
    path = comparison_sheet(SimpleNamespace(contact_sheet_paths=(a, b)), tmp_path / "combined.png")
    with Image.open(path) as image:
        assert image.size == (100, 100)
        assert image.getpixel((50, 59)) == (255, 0, 0) and image.getpixel((50, 60)) == (0, 0, 255)


def test_gateway_failures_are_not_dropped_from_operational_counts(tmp_path):
    raw = json.loads(report_file(tmp_path).read_text())
    row = raw["results"][0]
    row.update(status="FAILED", structured_valid=False)
    row["requests"][0]["status"] = "http_504"
    summary = summarize([row])[0]
    assert summary["http_counts"]["504"] == 1 and summary["success_rate"] == 0
