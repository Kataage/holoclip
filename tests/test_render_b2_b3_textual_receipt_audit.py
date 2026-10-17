"""Re-audit redacted B2/B3 model receipts against current synthetic ASR evidence."""
from __future__ import annotations

import copy
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_textual_dependency as dep
from holoclip import render_b2_b3_textual_receipt_audit as audit
from test_render_b2_b3_textual_dependency import _prepared


def _saved(tmp_path, monkeypatch, *, possible=True):
    prepared, state = _prepared(monkeypatch)
    quotes = (
        ("POSSIBLE_TEXTUAL_DEPENDENCY", "前の説明", "共通の発言")
        if possible else ("INSUFFICIENT_EVIDENCE", None, None)
    )
    checked = dep._validate_response({
        "relations": [{
            "pair_id": "p01", "status": quotes[0],
            "unique_quote": quotes[1], "shared_quote": quotes[2],
        }],
    }, prepared["reference_pairs"])
    saved = dep.preview_textual_dependency(prepared)
    saved.update({
        "status": "LOCAL_TEXTUAL_DEPENDENCY_HYPOTHESES_UNQUALIFIED",
        "model_request_count": 1,
        "provider": "fixture",
        "model": "mock",
        "model_hypotheses": checked,
        "source_asr_still_not_ground_truth": True,
        "creative_preference_or_audible_speech_unmeasured": True,
    })
    file = tmp_path / "model.json"
    file.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    return prepared, state, saved, file


@pytest.mark.parametrize("possible", [True, False])
def test_valid_saved_hypothesis_or_abstention_reaudits_offline(
    tmp_path, monkeypatch, possible,
):
    prepared, _, saved, path = _saved(tmp_path, monkeypatch, possible=possible)
    out = audit.audit_textual_receipt(path)
    assert out["status"] == "LOCAL_TEXTUAL_RECEIPT_CONSISTENT_UNQUALIFIED"
    assert out["reference_pairs_sha256"] == dep._fingerprint(prepared["reference_pairs"])
    assert out["pair_count"] == 1
    assert out["possible_textual_dependency_count"] == int(possible)
    assert out["insufficient_evidence_count"] == int(not possible)
    assert out["automatic_adoption_allowed"] is False
    assert out["ready"] is False and out["production_ready"] is False
    assert out["narrative_continuity_score"] is None
    assert out["receipt_authenticity_or_author_not_verified"] is True
    assert "前の説明" not in json.dumps(out, ensure_ascii=False)
    assert path.read_text(encoding="utf-8") == json.dumps(saved, ensure_ascii=False)


@pytest.mark.parametrize("mutate", [
    lambda saved: saved["model_hypotheses"][0].update(unique_quote_sha256="0" * 64),
    lambda saved: saved["model_hypotheses"][0].update(unique_source_asr_id="9" * 20),
    lambda saved: saved["model_hypotheses"][0].update(unique_source_start_seconds=0),
    lambda saved: saved["model_hypotheses"][0].update(score=9),
    lambda saved: saved.update(ready=True),
    lambda saved: saved.update(automatic_adoption_allowed=True),
    lambda saved: saved.update(model_request_count=2),
    lambda saved: saved.update(reference_pairs_sha256="a" * 64),
    lambda saved: saved.update(transcript_text="secret"),
    lambda saved: saved["model_hypotheses"].append(saved["model_hypotheses"][0]),
])
def test_modified_receipt_blocks(tmp_path, monkeypatch, mutate):
    _, _, saved, path = _saved(tmp_path, monkeypatch)
    invalid = copy.deepcopy(saved)
    mutate(invalid)
    path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="TEXTUAL_AUDIT"):
        audit.audit_textual_receipt(path)


def test_source_asr_change_invalidates_saved_quote(tmp_path, monkeypatch):
    _, state, _, path = _saved(tmp_path, monkeypatch)
    state["basis"]["references"]["b3"][1]["text"] = "違う話"
    with pytest.raises(ValueError, match="TEXTUAL_AUDIT"):
        audit.audit_textual_receipt(path)


def test_missing_or_symlinked_receipt_rejected(tmp_path, monkeypatch):
    _saved(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="TEXTUAL_AUDIT"):
        audit.audit_textual_receipt(tmp_path / "missing.json")
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(tmp_path / "model.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlink unsupported by host")
    with pytest.raises((ValueError, OSError)):
        audit.audit_textual_receipt(linked)


def test_optional_output_is_immutable_and_never_promotes(tmp_path, monkeypatch):
    _, _, _, path = _saved(tmp_path, monkeypatch)
    target = tmp_path / "audited.json"
    report = audit.audit_textual_receipt(path, output=target)
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == report
    original = target.read_bytes()
    with pytest.raises(ValueError):
        audit.audit_textual_receipt(path, output=target)
    assert target.read_bytes() == original


def test_cli_read_only_and_fail_closed(tmp_path, monkeypatch):
    _, _, _, path = _saved(tmp_path, monkeypatch)
    argv = ["render-b2-b3-textual-receipt-audit", "--receipt", str(path)]
    run = CliRunner().invoke(cli.app, argv)
    assert run.exit_code == 0, run.stdout
    assert json.loads(run.stdout)["status"] == "LOCAL_TEXTUAL_RECEIPT_CONSISTENT_UNQUALIFIED"
    assert not (tmp_path / "audit.json").exists()
    invalid = json.loads(path.read_text(encoding="utf-8"))
    invalid["model_hypotheses"][0]["shared_quote_sha256"] = "0" * 64
    path.write_text(json.dumps(invalid), encoding="utf-8")
    denied = CliRunner().invoke(cli.app, argv)
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
