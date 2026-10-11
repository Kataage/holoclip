"""Prompt-order invariance is a robustness probe, not semantic accuracy."""
from __future__ import annotations

import copy
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_real_semantic_order_sensitivity as ordering
from holoclip import render_real_semantic_contrast as contrast
from holoclip.corpus_growth import CorpusGrowthState
from test_ingested_source_transcript_identity_bridge import _native
from test_render_real_semantic_contrast import _answer, _paths
from test_render_ready_queue import VIDEO


def _case(tmp_path):
    params, _, _, media, _ = _native(tmp_path)
    prepared = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert prepared["status"] == "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED"
    return params, prepared, media


def _args(params, *opts):
    return [
        "render-real-semantic-order-sensitivity",
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
        *opts,
    ]


def _fake_client(prepared, *, different_status=False, different_quote=False,
                 different_route=False, invalid_second=False):
    class FakeClient:
        last_provenance = {"provider": "fake-qualified", "model": "model-a"}
        calls = 0
        async def chat_json(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["temperature"] == 0
            assert kwargs["max_tokens"] == 1280
            payload = json.loads(messages[1]["content"])
            case = prepared["cases"][0]
            assert payload["source_clock_selection_not_audible_truth"] is True
            if self.calls % 2 == 1:
                assert payload["source_timed_unverified_asr"] == (
                    case["source_timed_unverified_asr"])
                assert payload["counterfactuals"] == case["counterfactuals"]
            else:
                assert payload["source_timed_unverified_asr"] == list(reversed(
                    case["source_timed_unverified_asr"]))
                assert payload["counterfactuals"] == list(reversed(
                    case["counterfactuals"]))
            response = copy.deepcopy(_answer(case))
            if self.calls == 2:
                if different_status:
                    response["contrasts"][0].update({
                        "status": "INSUFFICIENT_EVIDENCE",
                        "lost_evidence_id": None, "lost_quote": None,
                        "retained_evidence_id": None, "retained_quote": None,
                    })
                if different_quote:
                    # Still an exact substring of the cited ASR.
                    response["contrasts"][0]["lost_quote"] = (
                        response["contrasts"][0]["lost_quote"][:1]
                    )
                if different_route:
                    self.last_provenance = {
                        "provider": "fake-qualified", "model": "model-b",
                    }
                if invalid_second:
                    response["contrasts"][0]["lost_quote"] = "FABRICATED_47129"
            return response
    return FakeClient()


def test_ordering_preview_preserves_redacted_evidence_and_never_calls_model(tmp_path):
    params, prepared, media = _case(tmp_path)
    before = media.read_bytes()
    preview = ordering.preview_real_order_sensitivity(prepared)
    assert preview["status"] == "LOCAL_ORDER_SENSITIVITY_EVIDENCE_PREPARED"
    assert preview["source_contrast_version"] == contrast.VERSION
    assert preview["model_call_upper_bound"] == 2
    assert preview["actual_model_calls"] == 0
    assert preview["contains_verbatim_asr"] is False
    assert preview["cases"]
    assert preview["semantic_accuracy"] is None
    assert preview["ready"] is False
    assert preview["all_quotes_redacted_to_sha256"] is True
    assert "導入部分" not in json.dumps(preview, ensure_ascii=False)
    assert VIDEO not in json.dumps(preview)
    assert "unverified_asr_text" not in json.dumps(preview)
    assert media.read_bytes() == before


def test_evidence_ids_and_timing_remain_identical_when_prompt_lists_reverse(tmp_path):
    _, prepared, _ = _case(tmp_path)
    case = prepared["cases"][0]
    original = ordering._prompt_messages(case, "ORIGINAL")
    reverse = ordering._prompt_messages(case, "REVERSED")
    assert original[0] == reverse[0]
    assert original != reverse
    left = json.loads(original[1]["content"])
    right = json.loads(reverse[1]["content"])
    assert right["source_timed_unverified_asr"] == list(reversed(
        left["source_timed_unverified_asr"]))
    assert right["counterfactuals"] == list(reversed(left["counterfactuals"]))
    with pytest.raises(ValueError, match="ORDERING_INVALID"):
        ordering._prompt_messages(case, "BAD")


@pytest.mark.asyncio
async def test_same_provider_same_valid_model_response_is_two_run_order_invariant(tmp_path):
    _, prepared, _ = _case(tmp_path)
    client = _fake_client(prepared)
    report = await ordering.infer_real_order_sensitivity(prepared, client)
    assert client.calls == 2
    assert report["status"] == "LOCAL_ORDER_SENSITIVITY_TWO_RUNS_UNQUALIFIED"
    assert report["actual_model_calls"] == 2
    assert report["same_model_route_verified"] is True
    assert report["comparable_variant_count"] == 3
    assert report["order_invariant_within_two_runs_count"] == 3
    assert report["status_change_count"] == 0
    assert report["citation_change_count"] == 0
    assert report["cases"]  # Saved only redacted evidence, not original ASR.
    assert all(row["comparison"] == "ORDER_INVARIANT_WITHIN_TWO_RUNS"
               for row in report["model_comparison_results"][0]["variants"])
    assert report["semantic_accuracy"] is None
    assert report["two_runs_do_not_prove_stability"] is True
    assert report["ready"] is False
    raw = json.dumps(report, ensure_ascii=False)
    assert "導入部分" not in raw
    assert "unverified_asr_text" not in raw
    assert "secret" not in raw


@pytest.mark.asyncio
async def test_status_flip_is_reported_not_promoted_to_quality(tmp_path):
    _, prepared, _ = _case(tmp_path)
    report = await ordering.infer_real_order_sensitivity(
        prepared, _fake_client(prepared, different_status=True),
    )
    assert report["status_change_count"] == 1
    assert report["order_invariant_within_two_runs_count"] == 2
    first = report["model_comparison_results"][0]["variants"][0]
    assert first["comparison"] == "ORDER_SENSITIVE_STATUS_CHANGE"
    assert first["status_agrees"] is False
    assert first["original"]["status"] == "POSSIBLE_COMPREHENSION_LOSS"
    assert first["reversed"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["auto_edit_allowed"] is False


@pytest.mark.asyncio
async def test_different_valid_quote_is_reported_as_citation_sensitivity(tmp_path):
    _, prepared, _ = _case(tmp_path)
    report = await ordering.infer_real_order_sensitivity(
        prepared, _fake_client(prepared, different_quote=True),
    )
    assert report["status_change_count"] == 0
    assert report["citation_change_count"] == 1
    first = report["model_comparison_results"][0]["variants"][0]
    assert first["comparison"] == "ORDER_SENSITIVE_CITATION_CHANGE"
    assert first["cited_ids_agree"] is True
    assert first["quote_hashes_agree"] is False
    assert "lost_quote" not in first["original"]
    assert len(first["original"]["lost_quote_sha256"]) == 64


@pytest.mark.asyncio
async def test_changed_model_route_makes_both_valid_runs_noncomparable(tmp_path):
    _, prepared, _ = _case(tmp_path)
    report = await ordering.infer_real_order_sensitivity(
        prepared, _fake_client(prepared, different_route=True),
    )
    assert report["same_model_route_verified"] is False
    assert report["comparable_variant_count"] == 0
    assert report["order_invariant_within_two_runs_count"] == 0
    assert report["status_change_count"] == 0
    assert all(item["comparison"] == "MODEL_ROUTE_NOT_COMPARABLE"
               for item in report["model_comparison_results"][0]["variants"])
    assert report["model_comparison_results"][0][
        "provider_route_identity_verified"] is False
    assert report["ready"] is False


@pytest.mark.asyncio
async def test_missing_provenance_never_counts_as_order_invariance(tmp_path):
    _, prepared, _ = _case(tmp_path)
    client = _fake_client(prepared)
    client.last_provenance = None
    report = await ordering.infer_real_order_sensitivity(prepared, client)
    assert report["comparable_variant_count"] == 0
    assert report["same_model_route_verified"] is False


@pytest.mark.asyncio
async def test_fabricated_quote_in_second_run_rejects_entire_result(tmp_path):
    _, prepared, _ = _case(tmp_path)
    client = _fake_client(prepared, invalid_second=True)
    with pytest.raises(ValueError, match="UNGROUNDED_CITATION"):
        await ordering.infer_real_order_sensitivity(prepared, client)
    assert client.calls == 2


def test_cli_default_order_probe_is_offline_and_output_not_created(tmp_path, monkeypatch):
    params, _, media = _case(tmp_path)
    before = media.read_bytes()
    class NoProvider:
        def __init__(self, *args, **kwargs):
            pytest.fail("No model may initialize in default preview")
    monkeypatch.setattr(cli, "MeshLLMClient", NoProvider)
    target = tmp_path / "not-created.json"
    r = CliRunner().invoke(cli.app, _args(params, "--output", str(target)))
    assert r.exit_code == 0, r.stdout
    assert not target.exists()
    obj = json.loads(r.stdout)
    assert obj["actual_model_calls"] == 0
    assert obj["model_call_upper_bound"] == 2
    assert "導入部分" not in r.stdout
    assert media.read_bytes() == before


def test_execution_requires_explicit_public_authorization_before_ai(tmp_path, monkeypatch):
    params, _, _ = _case(tmp_path)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    monkeypatch.setattr(cli, "MeshLLMClient",
                        lambda: pytest.fail("Must never call unconsented AI"))
    target = tmp_path / "blocked.json"
    res = CliRunner().invoke(cli.app, _args(params,
                                              "--execute", "--output", str(target)))
    assert res.exit_code == 6
    assert json.loads(res.stdout)["status"] == "LOCAL_ORDER_SENSITIVITY_BLOCKED"
    assert not target.exists()


def test_no_api_quota_used_on_existing_report_path(tmp_path, monkeypatch):
    params, _, _ = _case(tmp_path)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    monkeypatch.setattr(cli, "MeshLLMClient",
                        lambda: pytest.fail("Must preflight path before AI"))
    target = tmp_path / "taken.json"
    target.write_text("preserve", encoding="utf-8")
    res = CliRunner().invoke(cli.app, _args(params,
                                              "--execute", "--output", str(target)))
    assert res.exit_code == 6
    assert target.read_text(encoding="utf-8") == "preserve"


def test_fake_qualified_client_writes_redacted_create_only_receipt(tmp_path, monkeypatch):
    params, prepared, media = _case(tmp_path)
    baseline = media.read_bytes()
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")

    class FakeQualifiedClient:
        last_provenance = {"provider": "mock", "model": "mock-model"}
        calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def chat_json(self, messages, **kwargs):
            self.calls += 1
            return _answer(prepared["cases"][0])

    monkeypatch.setattr(cli, "MeshLLMClient", FakeQualifiedClient)
    target = tmp_path / "order-review.json"
    args = _args(params, "--execute", "--output", str(target))
    first = CliRunner().invoke(cli.app, args)
    assert first.exit_code == 0, first.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == json.loads(first.stdout)
    assert data["status"] == "LOCAL_ORDER_SENSITIVITY_TWO_RUNS_UNQUALIFIED"
    assert data["actual_model_calls"] == 2
    assert data["order_invariant_within_two_runs_count"] == 3
    assert "導入部分" not in first.stdout
    assert data["ready"] is False
    repeated = CliRunner().invoke(cli.app, args)
    assert repeated.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == data
    assert media.read_bytes() == baseline


def test_holdout_collision_and_missing_asr_do_not_make_ordered_model_cases(tmp_path):
    params, _, _, _, source = _native(tmp_path)
    source.unlink()
    empty = contrast.prepare_real_semantic_contrasts(**_paths(params))
    preview = ordering.preview_real_order_sensitivity(empty)
    assert preview["model_call_upper_bound"] == 0
    assert preview["cases"] == []
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    blocked = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert blocked["status"] == "LOCAL_REAL_CONTRAST_HOLDOUT_BLOCKED"
    assert ordering.preview_real_order_sensitivity(blocked)["ready"] is False
