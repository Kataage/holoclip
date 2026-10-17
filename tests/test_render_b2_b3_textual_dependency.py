"""No-network tests for source-cited B2/B3 textual hypothesis boundaries."""
from __future__ import annotations

import json
import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_textual_dependency as dep


_A = "a" * 20
_B = "b" * 20


def _reference(identity: str, text: str) -> dict:
    return {
        "id": identity, "text": text,
        "source_start_seconds": 10.0, "source_end_seconds": 11.0,
    }


def _prepared(monkeypatch):
    monkeypatch.setattr(dep, "run_editorial_evidence", lambda **kw: {
        "status": "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED",
        "ready": False, "automatic_winner": None,
    })
    state = {
        "status": "SUPPORTED",
        "basis": {"references": {
            "b2": [_reference(_A, "共通の発言")],
            "b3": [_reference(_A, "共通の発言"), _reference(_B, "前の説明")],
        }},
    }
    monkeypatch.setattr(dep, "_basis", lambda *a, **kw: state)
    prepared = dep.prepare_textual_dependency()
    assert prepared["pair_count"] == 1
    return prepared, state


def test_preparation_and_dry_run_have_no_verbatim_source_asr(monkeypatch):
    prepared, _ = _prepared(monkeypatch)
    assert prepared["status"] == "LOCAL_TEXTUAL_DEPENDENCY_ASR_PAIRS_PREPARED"
    assert prepared["reference_pairs"][0]["focus_variant_with_unique_reference"] == "b3"
    preview = dep.preview_textual_dependency(prepared)
    wire = json.dumps(preview, ensure_ascii=False)
    assert "前の説明" not in wire and "共通の発言" not in wire
    assert "unverified_source_asr_text" not in wire
    assert preview["narrative_continuity_score"] is None
    assert preview["speech_naturalness_score"] is None
    assert preview["payoff_completion_score"] is None
    assert preview["automatic_adoption_allowed"] is False


@pytest.mark.asyncio
async def test_one_cited_hypothesis_redacts_source_quotes(monkeypatch):
    prepared, _ = _prepared(monkeypatch)

    class Model:
        last_provenance = {"provider": "mock-zero-cost", "model": "fixture"}
        calls = 0

        async def chat_json(self, messages, **params):
            self.calls += 1
            assert params["temperature"] == 0
            assert len(messages) == 2
            return {"relations": [{
                "pair_id": "p01",
                "status": "POSSIBLE_TEXTUAL_DEPENDENCY",
                "unique_quote": "前の説明",
                "shared_quote": "共通の発言",
            }]}

    model = Model()
    result = await dep.infer_textual_dependency(prepared, model)
    assert model.calls == 1
    assert result["status"] == "LOCAL_TEXTUAL_DEPENDENCY_HYPOTHESES_UNQUALIFIED"
    assert result["model_request_count"] == 1
    assert result["model_hypotheses"][0]["focus_variant_with_unique_reference"] == "b3"
    assert result["model_hypotheses"][0]["unique_quote_sha256"]
    assert "前の説明" not in json.dumps(result, ensure_ascii=False)
    assert result["automatic_winner"] is None
    assert result["automatic_adoption_allowed"] is False
    assert result["ready"] is False and result["production_ready"] is False


@pytest.mark.parametrize("data", [
    {"relations": [{
        "pair_id": "p01", "status": "POSSIBLE_TEXTUAL_DEPENDENCY",
        "unique_quote": "発話にはない文字", "shared_quote": "共通の発言",
    }]},
    {"relations": [{
        "pair_id": "p02", "status": "INSUFFICIENT_EVIDENCE",
        "unique_quote": None, "shared_quote": None,
    }]},
    {"relations": [{
        "pair_id": "p01", "status": "INSUFFICIENT_EVIDENCE",
        "unique_quote": "前の説明", "shared_quote": None,
    }]},
    {"relations": []},
    {"relations": [{"pair_id": "p01", "status": "INSUFFICIENT_EVIDENCE",
                    "unique_quote": None, "shared_quote": None, "score": 5}]},
])
def test_reject_untrusted_model_json(monkeypatch, data):
    prepared, _ = _prepared(monkeypatch)
    with pytest.raises(ValueError, match="TEXTUAL_DEPENDENCY"):
        dep._validate_response(data, prepared["reference_pairs"])


def test_missing_common_source_reference_abstains(monkeypatch):
    _, state = _prepared(monkeypatch)
    state["basis"]["references"]["b2"] = [_reference("c" * 20, "別の文")]
    result = dep.prepare_textual_dependency()
    assert result["pair_count"] == 0
    assert result["status"] == "LOCAL_TEXTUAL_DEPENDENCY_NOT_ELIGIBLE"


def test_conflicting_same_id_source_text_rejected(monkeypatch):
    _, state = _prepared(monkeypatch)
    state["basis"]["references"]["b2"][0]["text"] = "不整合"
    with pytest.raises(ValueError, match="SHARED_SOURCE_INCONSISTENT"):
        dep.prepare_textual_dependency()


def test_cli_dry_run_and_explicit_public_data_gate(tmp_path, monkeypatch):
    prepared, _ = _prepared(monkeypatch)
    monkeypatch.setattr(cli, "prepare_textual_dependency", lambda **kw: prepared)
    out = tmp_path / "private.json"
    argv = ["render-b2-b3-textual-dependency", "--output", str(out)]
    preview = CliRunner().invoke(cli.app, argv)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["contains_verbatim_asr"] is False
    denied = CliRunner().invoke(cli.app, [*argv, "--execute"], env={
        "HOLOCLIP_DATA_CLASS": "private",
    })
    assert denied.exit_code == 6
    assert not out.exists()


def test_cli_zero_cost_client_is_explicitly_invoked_once(tmp_path, monkeypatch):
    prepared, _ = _prepared(monkeypatch)
    monkeypatch.setattr(cli, "prepare_textual_dependency", lambda **kw: prepared)

    class FakeClient:
        last_provenance = {"provider": "fixture", "model": "mock"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def chat_json(self, messages, **kw):
            return {"relations": [{
                "pair_id": "p01", "status": "INSUFFICIENT_EVIDENCE",
                "unique_quote": None, "shared_quote": None,
            }]}

    monkeypatch.setattr(cli, "MeshLLMClient", FakeClient)
    output = tmp_path / "report.json"
    args = [
        "render-b2-b3-textual-dependency", "--execute", "--output", str(output),
    ]
    run = CliRunner().invoke(cli.app, args, env={"HOLOCLIP_DATA_CLASS": "public"})
    assert run.exit_code == 0, run.stdout
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["model_hypotheses"][0]["status"] == "INSUFFICIENT_EVIDENCE"
    assert stored["ready"] is False
    assert "前の説明" not in json.dumps(stored, ensure_ascii=False)
    repeat = CliRunner().invoke(cli.app, args, env={"HOLOCLIP_DATA_CLASS": "public"})
    assert repeat.exit_code == 6
    assert json.loads(output.read_text(encoding="utf-8")) == stored
