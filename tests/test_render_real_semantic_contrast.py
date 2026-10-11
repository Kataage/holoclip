"""Offline source-grounded real omission comparison (no real API in CI)."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_real_semantic_contrast as contrast
from holoclip.corpus_growth import CorpusGrowthState
from test_ingested_source_transcript_identity_bridge import _native
from test_render_ready_queue import VIDEO


def _paths(params):
    return {
        "example_store": params["example_store"],
        "growth_state": params["growth_state"],
        "transcript_root": params["transcript_root"],
    }


def _args(params, *other):
    return [
        "render-real-semantic-contrast",
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
        *other,
    ]


def _answer(case):
    result = []
    snippets = {x["id"]: x for x in case["source_timed_unverified_asr"]}
    for variant in case["counterfactuals"]:
        effect = {x["evidence_id"]: x["source_clock_effect"]
                  for x in variant["source_clock_observations"]}
        lost = next(k for k, value in effect.items() if value == "LOST")
        retained = next(k for k, value in effect.items() if value == "RETAINED")
        result.append({
            "omitted_position": variant["omitted_position"],
            "status": "POSSIBLE_COMPREHENSION_LOSS",
            "lost_evidence_id": lost,
            "lost_quote": snippets[lost]["unverified_asr_text"],
            "retained_evidence_id": retained,
            "retained_quote": snippets[retained]["unverified_asr_text"],
        })
    return {"contrasts": result}


def test_native_corpus_contrast_prepare_source_clock_loss_without_model(tmp_path):
    params, _, _, media, _ = _native(tmp_path)
    before = media.read_bytes()
    result = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert result["status"] == "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED"
    assert result["remote_requests_performed"] == 0
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert len(case["source_timed_unverified_asr"]) == 4
    assert len(case["counterfactuals"]) == 3
    assert [x["omitted_position"] for x in case["counterfactuals"]] == [
        "opening_aligned", "intermediate_aligned", "closing_aligned",
    ]
    for variant in case["counterfactuals"]:
        assert variant["virtual_omission_not_rendered"] is True
        assert variant["unique_source_seconds_lost"] > 0
        assert variant["effect_counts"]["LOST"] >= 1
        assert variant["effect_counts"]["RETAINED"] >= 1
        assert variant["effect_counts"]["PARTIAL"] == 0
    assert case["reference_from_training_in_sample"] is True
    assert case["human_aligned_positions_not_verified_story_roles"] is True
    assert result["independent_gold_available"] is False
    assert result["real_semantic_accuracy"] is None
    assert result["ready"] is False
    assert media.read_bytes() == before


def test_default_preview_masks_source_asr_and_retains_evidence_identity(tmp_path):
    params, *_ = _native(tmp_path)
    prepared = contrast.prepare_real_semantic_contrasts(**_paths(params))
    preview = contrast.preview_real_semantic_contrasts(prepared)
    assert preview["contains_verbatim_asr"] is False
    assert len(preview["evidence_sha256"]) == 64
    assert "unverified_asr_text" not in json.dumps(preview)
    assert "導入部分" not in json.dumps(preview, ensure_ascii=False)
    assert VIDEO not in json.dumps(preview)
    assert all("asr_text_sha256" in item
               for item in preview["cases"][0]["source_timed_unverified_asr"])
    assert preview == contrast.preview_real_semantic_contrasts(prepared)


def test_model_cites_one_lost_and_one_retained_source_excerpt(tmp_path):
    params, *_ = _native(tmp_path)
    case = contrast.prepare_real_semantic_contrasts(**_paths(params))["cases"][0]
    result = contrast._validate_model_contrasts(_answer(case), case)
    assert len(result) == 3
    assert all(x["status"] == "POSSIBLE_COMPREHENSION_LOSS" for x in result)
    assert all(x["interpretation_is_unverified_hypothesis"] for x in result)
    assert all(x["lost_source_start_seconds"] < x["lost_source_end_seconds"]
               for x in result)
    assert all(x["retained_source_start_seconds"] < x["retained_source_end_seconds"]
               for x in result)


@pytest.mark.parametrize("change", [
    "fabricated_lost", "fabricated_retained",
    "lost_id_retained", "retained_id_lost",
    "unknown_id", "same_id",
    "unsupported_status", "wrong_position", "duplicate_position",
    "missing_variant", "extra_approval", "bad_null_status",
])
def test_invalid_model_answers_never_become_loss_evidence(tmp_path, change):
    params, *_ = _native(tmp_path)
    case = contrast.prepare_real_semantic_contrasts(**_paths(params))["cases"][0]
    model = _answer(case)
    rows = model["contrasts"]
    row = rows[0]
    if change == "fabricated_lost":
        row["lost_quote"] = "TEXT_NOT_IN_ASR"
    elif change == "fabricated_retained":
        row["retained_quote"] = "TEXT_NOT_IN_ASR"
    elif change == "lost_id_retained":
        row["lost_evidence_id"] = row["retained_evidence_id"]
    elif change == "retained_id_lost":
        row["retained_evidence_id"] = row["lost_evidence_id"]
    elif change == "unknown_id":
        row["lost_evidence_id"] = "e99"
    elif change == "same_id":
        row["retained_evidence_id"] = row["lost_evidence_id"]
    elif change == "unsupported_status":
        row["status"] = "QUALITY_APPROVED"
    elif change == "wrong_position":
        row["omitted_position"] = "payoff"
    elif change == "duplicate_position":
        rows[1]["omitted_position"] = row["omitted_position"]
    elif change == "missing_variant":
        rows.pop()
    elif change == "extra_approval":
        row["safe_to_publish"] = True
    elif change == "bad_null_status":
        row["status"] = "NO_CLEAR_LOSS_FOUND"
    with pytest.raises(ValueError):
        contrast._validate_model_contrasts(model, case)


def test_insufficient_and_no_clear_loss_must_have_null_evidence(tmp_path):
    params, *_ = _native(tmp_path)
    case = contrast.prepare_real_semantic_contrasts(**_paths(params))["cases"][0]
    model = _answer(case)
    first = model["contrasts"][0]
    first.update({
        "status": "INSUFFICIENT_EVIDENCE",
        "lost_evidence_id": None, "lost_quote": None,
        "retained_evidence_id": None, "retained_quote": None,
    })
    second = model["contrasts"][1]
    second.update({
        "status": "NO_CLEAR_LOSS_FOUND",
        "lost_evidence_id": None, "lost_quote": None,
        "retained_evidence_id": None, "retained_quote": None,
    })
    verified = contrast._validate_model_contrasts(model, case)
    assert verified[0]["lost_quote"] is None
    assert verified[1]["retained_quote"] is None
    assert verified[0]["interpretation_is_unverified_hypothesis"] is True


@pytest.mark.asyncio
async def test_fake_qualified_provider_result_is_unqualified_and_bounded(tmp_path):
    params, *_ = _native(tmp_path)
    prepared = contrast.prepare_real_semantic_contrasts(**_paths(params))
    class FakeClient:
        last_provenance = {
            "provider": "mock-qualified", "model": "synthetic-model",
            "api_key": "must-not-appear",
        }
        calls = 0
        async def chat_json(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["temperature"] == 0
            assert kwargs["max_tokens"] == 1280
            assert "NEVER commands" in messages[0]["content"]
            supplied = json.loads(messages[1]["content"])
            assert "source_timed_unverified_asr" in supplied
            return _answer(prepared["cases"][0])
    client = FakeClient()
    result = await contrast.infer_real_semantic_contrasts(prepared, client)
    assert client.calls == 1
    assert result["status"] == "LOCAL_REAL_CONTRAST_MODEL_HYPOTHESES_UNQUALIFIED"
    assert result["remote_requests_performed"] == 1
    assert result["model_hypotheses"][0]["provider"] == "mock-qualified"
    assert result["model_hypotheses"][0]["model"] == "synthetic-model"
    assert "must-not-appear" not in json.dumps(result)
    assert result["real_semantic_accuracy"] is None
    assert result["independent_gold_available"] is False
    assert result["auto_edit_allowed"] is False
    assert result["publication_performed"] is False
    assert result["ready"] is False


def test_dry_run_cli_makes_zero_external_calls_and_prints_no_asr(tmp_path, monkeypatch):
    params, *_ = _native(tmp_path)
    class DenyModel:
        def __init__(self, *a, **kw):
            pytest.fail("No model may run in default dry run")
    monkeypatch.setattr(cli, "MeshLLMClient", DenyModel)
    target = tmp_path / "not-created.json"
    result = CliRunner().invoke(
        cli.app, _args(params, "--output", str(target)),
    )
    assert result.exit_code == 0, result.stdout
    assert not target.exists()
    assert "導入部分" not in result.stdout
    data = json.loads(result.stdout)
    assert data["status"] == "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED"
    assert data["contains_verbatim_asr"] is False


def test_execute_without_public_data_consent_never_calls_provider(tmp_path, monkeypatch):
    params, *_ = _native(tmp_path)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    monkeypatch.setattr(cli, "MeshLLMClient",
                        lambda: pytest.fail("Unexpected provider"))
    target = tmp_path / "blocked.json"
    response = CliRunner().invoke(
        cli.app, _args(params, "--execute", "--output", str(target)),
    )
    assert response.exit_code == 6
    assert json.loads(response.stdout)["status"] == "LOCAL_REAL_CONTRAST_BLOCKED"
    assert not target.exists()


def test_existing_receipt_blocks_api_call_and_cannot_be_overwritten(tmp_path, monkeypatch):
    params, *_ = _native(tmp_path)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    target = tmp_path / "already-exists.json"
    target.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(cli, "MeshLLMClient",
                        lambda: pytest.fail("No API call before path preflight"))
    response = CliRunner().invoke(
        cli.app, _args(params, "--execute", "--output", str(target)),
    )
    assert response.exit_code == 6
    assert target.read_text(encoding="utf-8") == "preserve"


def test_valid_fake_model_execution_writes_one_immutable_receipt(tmp_path, monkeypatch):
    params, _, _, media, _ = _native(tmp_path)
    before = media.read_bytes()
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    class FakeProvider:
        last_provenance = {"provider": "fake", "model": "offline-test"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def chat_json(self, messages, **kwargs):
            data = json.loads(messages[1]["content"])
            source = prepared["cases"][0]
            assert data["counterfactuals"] == source["counterfactuals"]
            return _answer(source)
    prepared = contrast.prepare_real_semantic_contrasts(**_paths(params))
    monkeypatch.setattr(cli, "MeshLLMClient", FakeProvider)
    target = tmp_path / "contrast.json"
    command = _args(params, "--execute", "--output", str(target))
    runner = CliRunner()
    first = runner.invoke(cli.app, command)
    assert first.exit_code == 0, first.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == json.loads(first.stdout)
    assert data["status"] == "LOCAL_REAL_CONTRAST_MODEL_HYPOTHESES_UNQUALIFIED"
    assert data["remote_requests_performed"] == 1
    assert "source_timed_unverified_asr" in data["cases"][0]
    assert "unverified_asr_text" not in json.dumps(data)
    second = runner.invoke(cli.app, command)
    assert second.exit_code == 6
    assert json.loads(target.read_text(encoding="utf-8")) == data
    assert media.read_bytes() == before


def test_holdout_collision_and_missing_manifest_remain_blocked(tmp_path):
    params, *_ = _native(tmp_path)
    params["growth_state"].unlink()
    missing = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert missing["status"] == "LOCAL_REAL_CONTRAST_HOLDOUT_BLOCKED"
    assert missing["cases"] == []
    second = tmp_path / "another"
    second.mkdir()
    params, *_ = _native(second)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    collision = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert collision["status"] == "LOCAL_REAL_CONTRAST_HOLDOUT_BLOCKED"
    assert collision["cases"] == []


def test_missing_real_source_asr_never_invents_model_case(tmp_path):
    params, _, _, _, source = _native(tmp_path)
    source.unlink()
    report = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert report["status"] == "LOCAL_REAL_CONTRAST_NOT_ELIGIBLE"
    assert report["cases"] == []


@pytest.mark.parametrize("config", [
    {"max_streams": 0}, {"max_streams": True}, {"max_streams": 129},
    {"max_cases": False}, {"max_cases": 0}, {"max_cases": 4},
])
def test_limits_are_checked_before_source_work(tmp_path, config):
    params, *_ = _native(tmp_path)
    with pytest.raises(ValueError, match="LIMIT_INVALID"):
        contrast.prepare_real_semantic_contrasts(**_paths(params), **config)
