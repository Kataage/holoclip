"""In-sample role-hypothesis contracts, no paid/network calls in CI."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_same_source_geometry as geometry
from holoclip import render_same_source_semantic_review as semantic
from test_render_same_source_geometry import _build, _human_example
from test_render_ready_queue import VIDEO


def _case(tmp_path, *, holdout=False, transcript=True, metadata=True):
    params, receipt, timeline, media = _build(
        tmp_path, clips=[_human_example()],
        reserved=[VIDEO] if holdout else (),
    )
    if transcript:
        root = params["transcript_root"]
        root.mkdir()
        payload = {
            "segments": [
                {"start": 11, "end": 11.7, "text": "最初の説明"},
                {"start": 12.1, "end": 12.8, "text": "予想外の展開"},
                {"start": 20, "end": 20.8, "text": "リアクション"},
                {"start": 23.1, "end": 23.7, "text": "最後のオチ"},
            ],
        }
        if metadata:
            payload["video_id"] = VIDEO
        (root / f"{VIDEO}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return params, receipt, timeline, media


def _prepare(params):
    return semantic.prepare_semantic_evidence(
        geometry.compare_same_source_edit_geometry(**params)
    )


def _model_answer(case):
    first = case["source_timed_unverified_asr"][0]
    return {"roles": [
        {"role": "setup", "status": "POSSIBLE_TEXTUAL_REFERENCE",
         "evidence_id": first["id"], "quote": first["unverified_asr_text"]},
        {"role": "development", "status": "INSUFFICIENT_EVIDENCE",
         "evidence_id": None, "quote": None},
        {"role": "payoff", "status": "INSUFFICIENT_EVIDENCE",
         "evidence_id": None, "quote": None},
    ]}


def test_semantic_prepares_only_real_source_timed_asr_and_clip_level_annotations(tmp_path):
    params, receipt, _, media = _case(tmp_path)
    prepared = _prepare(params)
    assert prepared["status"] == "LOCAL_SEMANTIC_EVIDENCE_PREPARED"
    assert prepared["external_inference_performed"] is False
    assert prepared["ready"] is False
    assert len(prepared["cases"]) == 1
    case = prepared["cases"][0]
    assert case["job_id"] == receipt["job_id"]
    assert case["human_clip_level_descriptions"]["setup"] == "ordinary setup"
    assert case["human_clip_level_descriptions"]["payoff"] == "clear payoff"
    assert 0 < len(case["source_timed_unverified_asr"]) <= 12
    assert case["source_selection_sides_are_not_presence_labels"] is True
    assert {item["source_window_selection_side"] for item in
            case["source_timed_unverified_asr"]} == {"generated_only", "human_only"}
    assert all(item["source_start_seconds"] < item["source_end_seconds"]
               for item in case["source_timed_unverified_asr"])
    assert VIDEO not in json.dumps(prepared)
    assert media.is_file()


@pytest.mark.parametrize("holdout,transcript,metadata,expected", [
    (True, True, True, "LOCAL_SEMANTIC_GATES_NOT_VERIFIED"),
    (False, False, False, "LOCAL_SEMANTIC_NO_ELIGIBLE_EVIDENCE"),
    (False, True, False, "LOCAL_SEMANTIC_NO_ELIGIBLE_EVIDENCE"),
])
def test_semantic_missing_or_unverified_evidence_never_calls_ai(
    tmp_path, holdout, transcript, metadata, expected,
):
    params, *_ = _case(
        tmp_path, holdout=holdout, transcript=transcript, metadata=metadata,
    )
    prepared = _prepare(params)
    assert prepared["status"] == expected
    assert prepared["cases"] == []
    assert prepared["ready"] is False


@pytest.mark.asyncio
async def test_semantic_model_quote_is_verified_and_provenance_stays_bounded(tmp_path):
    params, *_ = _case(tmp_path)
    prepared = _prepare(params)
    class FakeClient:
        last_provenance = {"provider": "mock-free", "model": "test-model",
                           "api_key": "must-never-output"}
        calls = 0
        async def chat_json(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["temperature"] == 0
            assert kwargs["max_tokens"] == 1024
            assert "not instructions" in messages[0]["content"]
            assert "source_timed_unverified_asr" in messages[1]["content"]
            return _model_answer(prepared["cases"][0])

    client = FakeClient()
    result = await semantic.infer_semantic_hypotheses(prepared, client)
    assert client.calls == 1
    assert result["status"] == "LOCAL_SEMANTIC_HYPOTHESES_UNQUALIFIED"
    assert result["ready"] is False
    assert result["external_inference_performed"] is True
    item = result["hypotheses"][0]
    assert item["provider"] == "mock-free" and item["model"] == "test-model"
    assert item["role_hypotheses"][0]["status"] == "POSSIBLE_TEXTUAL_REFERENCE"
    assert item["role_hypotheses"][0]["source_start_seconds"] >= 0
    assert item["role_hypotheses"][1]["evidence_id"] is None
    assert "must-never-output" not in json.dumps(result)


@pytest.mark.parametrize("corruption", [
    "fake-quote", "unknown-id", "missing-role", "duplicate-role",
    "unsupported-label", "unsupported-fields",
])
def test_invalid_model_answers_cannot_become_anchored_hypotheses(tmp_path, corruption):
    prepared = _prepare(_case(tmp_path)[0])
    case = prepared["cases"][0]
    payload = _model_answer(case)
    if corruption == "fake-quote":
        payload["roles"][0]["quote"] = "THIS_TEXT_DID_NOT_EXIST"
    elif corruption == "unknown-id":
        payload["roles"][0]["evidence_id"] = "e99"
    elif corruption == "missing-role":
        payload["roles"].pop()
    elif corruption == "duplicate-role":
        payload["roles"][1]["role"] = "setup"
    elif corruption == "unsupported-label":
        payload["roles"][1]["status"] = "QUALITY_APPROVED"
    elif corruption == "unsupported-fields":
        payload["roles"][0]["is_good_edit"] = True
    with pytest.raises(ValueError):
        semantic._validate_candidate(payload, case)


def test_evidence_dry_run_command_does_not_invoke_provider_or_write_files(
    tmp_path, monkeypatch,
):
    params, *_ = _case(tmp_path)
    class MustNotCall:
        def __init__(self):
            raise AssertionError("No model constructed during default dry run")
    monkeypatch.setattr(cli, "MeshLLMClient", MustNotCall)
    target = tmp_path / "not_created.json"
    args = _args(params, target)
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout
    assert not target.exists()
    report = json.loads(result.stdout)
    assert report["status"] == "LOCAL_SEMANTIC_EVIDENCE_PREPARED"
    assert report["external_inference_performed"] is False
    assert report["evidence_sha256"] == semantic.prepared_fingerprint(report)


def _args(params, target):
    return [
        "render-same-source-semantic-review",
        "--queue", str(params["queue"]),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--preview-root", str(params["preview_root"]),
        "--transcript-root", str(params["transcript_root"]),
        "--output", str(target),
    ]


def test_execution_requires_explicit_public_data_class_before_any_model_call(
    tmp_path, monkeypatch,
):
    params, *_ = _case(tmp_path)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    monkeypatch.setattr(cli, "MeshLLMClient",
                        lambda: pytest.fail("Model must never run on private class"))
    target = tmp_path / "blocked.json"
    response = CliRunner().invoke(cli.app, [*_args(params, target), "--execute"])
    assert response.exit_code == 6
    assert json.loads(response.stdout)["status"] == "LOCAL_SEMANTIC_REVIEW_BLOCKED"
    assert not target.exists()


def test_explicit_fake_provider_creates_only_new_immutable_review(
    tmp_path, monkeypatch,
):
    params, _, _, media = _case(tmp_path)
    before = media.read_bytes()
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    class FakeQualifiedProvider:
        last_provenance = {"provider": "free-mock", "model": "qualified-test"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def chat_json(self, messages, **kwargs):
            request = json.loads(messages[1]["content"])
            return _model_answer({"source_timed_unverified_asr":
                                  request["source_timed_unverified_asr"]})
    monkeypatch.setattr(cli, "MeshLLMClient", FakeQualifiedProvider)
    target = tmp_path / "receipt.json"
    args = [*_args(params, target), "--execute"]
    first = CliRunner().invoke(cli.app, args)
    assert first.exit_code == 0, first.stdout
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved == json.loads(first.stdout)
    assert saved["status"] == "LOCAL_SEMANTIC_HYPOTHESES_UNQUALIFIED"
    assert saved["publication_performed"] is False
    assert saved["ready"] is False
    assert media.read_bytes() == before
    repeated = CliRunner().invoke(cli.app, args)
    assert repeated.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == saved


def test_semantic_report_output_is_never_overwritten_by_invalid_schema(tmp_path):
    params, *_ = _case(tmp_path)
    prepared = _prepare(params)
    target = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match="UNGROUNDED"):
        semantic._validate_candidate(
            {"roles": [
                {"role": role, "status": "POSSIBLE_TEXTUAL_REFERENCE",
                 "evidence_id": "e01", "quote": "invented"}
                for role in ("setup", "development", "payoff")
            ]},
            prepared["cases"][0],
        )
    assert not target.exists()


def test_evidence_sha_is_stable_and_immutable_output_has_no_replacement(tmp_path):
    params, *_ = _case(tmp_path)
    evidence = _prepare(params)
    assert semantic.prepared_fingerprint(evidence) == hashlib.sha256(
        json.dumps(evidence["cases"], sort_keys=True,
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    p = tmp_path / "evidence.json"
    semantic.save_semantic_review(evidence, p)
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE"):
        semantic.save_semantic_review(evidence, p)
