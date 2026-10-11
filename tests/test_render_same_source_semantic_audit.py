"""Offline receipt revalidation; fixtures never invoke network, NIM, or media."""
from __future__ import annotations

import copy
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_same_source_semantic_audit as audit
from holoclip import render_same_source_semantic_review as semantic
from test_render_same_source_semantic_review import _case, _prepare, _model_answer
from test_render_ready_queue import VIDEO


def _saved(params):
    fresh = _prepare(params)
    assert fresh["status"] == "LOCAL_SEMANTIC_EVIDENCE_PREPARED"
    cases = fresh["cases"]
    models = []
    for case in cases:
        roles = semantic._validate_candidate(_model_answer(case), case)
        models.append({
            "job_id": case["job_id"],
            "source_stream_sha256": case["source_stream_sha256"],
            "human_clip_identity_sha256": case["human_clip_identity_sha256"],
            "local_transcript_sha256": case["local_transcript_sha256"],
            "provider": "mock",
            "model": "no-network",
            "role_hypotheses": roles,
            "evidence_is_unverified_asr": True,
            "quality_qualification_allowed": False,
            "auto_edit_allowed": False,
        })
    saved = {
        **fresh, "status": "LOCAL_SEMANTIC_HYPOTHESES_UNQUALIFIED",
        "external_inference_performed": True,
        "hypotheses": models, "evidence_sha256": semantic.prepared_fingerprint(fresh),
    }
    return saved, fresh


def _args(params, receipt, output=None):
    values = [
        "render-same-source-semantic-audit",
        "--receipt", str(receipt),
        "--queue", str(params["queue"]),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--preview-root", str(params["preview_root"]),
        "--transcript-root", str(params["transcript_root"]),
    ]
    if output is not None:
        values += ["--output", str(output)]
    return values


def test_audit_rechecks_real_local_evidence_and_exposes_no_text_or_secret(tmp_path):
    params, _, _, media = _case(tmp_path)
    original = media.read_bytes()
    saved, fresh = _saved(params)
    result = audit.audit_semantic_receipt(saved, fresh)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_CITATIONS_CONSISTENT_UNQUALIFIED"
    assert result["rechecked_against_current_local_source"] is True
    assert result["case_count_checked"] == 1
    assert result["verbatim_source_citations_checked"] == 1
    assert result["insufficient_evidence_roles"] == 2
    assert result["rejection_reasons"] == []
    assert result["fresh_evidence_sha256"] == semantic.prepared_fingerprint(fresh)
    assert result["narrative_quality_precision"] is None
    assert result["narrative_quality_recall"] is None
    assert result["narrative_roles_verified"] is False
    assert result["ready"] is False
    assert result["auto_edit_allowed"] is False
    assert "最初の説明" not in json.dumps(result, ensure_ascii=False)
    assert "ordinary setup" not in json.dumps(result)
    assert VIDEO not in json.dumps(result)
    assert media.read_bytes() == original


@pytest.mark.parametrize("change", [
    "quote", "evidence_id", "source_time", "source_side",
    "role_missing", "role_duplicate", "role_extra_field",
    "role_status", "insufficient_citation", "hypothesis_extra",
    "identity", "model_gate", "role_model_flag",
    "ready_true", "ready_number", "publication_true",
    "source_cases", "evidence_hash", "duplicate_hypothesis",
])
def test_audit_rejects_adversarial_saved_receipt_mutation(tmp_path, change):
    params, *_ = _case(tmp_path)
    saved, fresh = _saved(params)
    item = saved["hypotheses"][0]
    cited = item["role_hypotheses"][0]
    if change == "quote":
        cited["quote"] = "fabricated quotation"
    elif change == "evidence_id":
        cited["evidence_id"] = "e99"
    elif change == "source_time":
        cited["source_start_seconds"] += 0.125
    elif change == "source_side":
        cited["source_window_selection_side"] = "human_only" if (
            cited["source_window_selection_side"] == "generated_only"
        ) else "generated_only"
    elif change == "role_missing":
        item["role_hypotheses"].pop()
    elif change == "role_duplicate":
        item["role_hypotheses"][1]["role"] = "setup"
    elif change == "role_extra_field":
        cited["edit_approved"] = True
    elif change == "role_status":
        cited["status"] = "QUALITY_APPROVED"
    elif change == "insufficient_citation":
        item["role_hypotheses"][1]["evidence_id"] = "e01"
    elif change == "hypothesis_extra":
        item["production_ready"] = True
    elif change == "identity":
        item["source_stream_sha256"] = "f" * 64
    elif change == "model_gate":
        item["quality_qualification_allowed"] = True
    elif change == "role_model_flag":
        cited["model_reference_not_independently_verified"] = False
    elif change == "ready_true":
        saved["ready"] = True
    elif change == "ready_number":
        saved["ready"] = 0
    elif change == "publication_true":
        saved["publication_performed"] = True
    elif change == "source_cases":
        saved["cases"][0]["human_clip_level_descriptions"]["setup"] = "injected"
    elif change == "evidence_hash":
        saved["evidence_sha256"] = "a" * 64
    elif change == "duplicate_hypothesis":
        saved["hypotheses"].append(copy.deepcopy(item))
    result = audit.audit_semantic_receipt(saved, fresh)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert result["rejection_reasons"]
    assert result["ready"] is False
    assert result["rechecked_against_current_local_source"] is False
    assert "fabricated quotation" not in json.dumps(result)


def test_audit_detects_source_transcript_edit_without_model_request(tmp_path):
    params, *_ = _case(tmp_path)
    saved, old = _saved(params)
    file = params["transcript_root"] / f"{VIDEO}.json"
    payload = json.loads(file.read_text(encoding="utf-8"))
    payload["segments"][0]["text"] = "変更された発話"
    file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    new = _prepare(params)
    assert semantic.prepared_fingerprint(new) != semantic.prepared_fingerprint(old)
    result = audit.audit_semantic_receipt(saved, new)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert "SOURCE_EVIDENCE_CHANGED_OR_TAMPERED" in result["rejection_reasons"]


def test_audit_fails_closed_when_holdout_reservation_changes(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthState

    params, *_ = _case(tmp_path)
    saved, _ = _saved(params)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    fresh = _prepare(params)
    result = audit.audit_semantic_receipt(saved, fresh)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert result["rejection_reasons"] == ["LOCAL_EVIDENCE_NOT_ELIGIBLE"]


def test_cli_audits_saved_receipt_readonly_and_optionally_writes_new_report(tmp_path):
    params, _, _, media = _case(tmp_path)
    before = media.read_bytes()
    saved, _ = _saved(params)
    receipt = tmp_path / "semantic-review.json"
    semantic.save_semantic_review(saved, receipt)
    report = tmp_path / "audit.json"
    preview = CliRunner().invoke(cli.app, _args(params, receipt))
    assert preview.exit_code == 0, preview.stdout
    assert not report.exists()
    doc = json.loads(preview.stdout)
    assert doc["status"] == "LOCAL_SEMANTIC_AUDIT_CITATIONS_CONSISTENT_UNQUALIFIED"
    created = CliRunner().invoke(cli.app, _args(params, receipt, report))
    assert created.exit_code == 0, created.stdout
    assert json.loads(report.read_text(encoding="utf-8")) == json.loads(
        created.stdout
    )
    assert doc["receipt_sha256"] is not None
    repeat = CliRunner().invoke(cli.app, _args(params, receipt, report))
    assert repeat.exit_code == 6
    assert json.loads(report.read_text(encoding="utf-8")) == doc
    assert media.read_bytes() == before


def test_cli_rejected_receipt_emits_reason_only_and_nonzero_exit(tmp_path):
    params, *_ = _case(tmp_path)
    saved, _ = _saved(params)
    saved["hypotheses"][0]["role_hypotheses"][0]["quote"] = "never said"
    receipt = tmp_path / "altered.json"
    receipt.write_text(json.dumps(saved), encoding="utf-8")
    result = CliRunner().invoke(cli.app, _args(params, receipt))
    assert result.exit_code == 6
    body = json.loads(result.stdout)
    assert body["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert "never said" not in result.stdout


def test_rejected_missing_or_unsafe_receipt_never_becomes_a_pass(tmp_path):
    params, *_ = _case(tmp_path)
    fresh = _prepare(params)
    absent = tmp_path / "nonexistent.json"
    result = audit.audit_receipt_file(absent, fresh)
    assert result["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
    assert result["rejection_reasons"] == ["RECEIPT_NOT_OBJECT"]
    invalid = tmp_path / "text.json"
    invalid.write_text("not JSON", encoding="utf-8")
    assert audit.audit_receipt_file(invalid, fresh)["status"] == (
        "LOCAL_SEMANTIC_AUDIT_REJECTED"
    )


def test_audit_rejects_stale_when_source_window_page_changes(tmp_path):
    params, *_ = _case(tmp_path)
    saved, _ = _saved(params)
    params["max_jobs"] = 1
    fresh = _prepare(params)
    # A single-job page is identical; this must remain re-checkable.
    assert audit.audit_semantic_receipt(saved, fresh)["status"] == (
        "LOCAL_SEMANTIC_AUDIT_CITATIONS_CONSISTENT_UNQUALIFIED"
    )
    params["after_job_id"] = "renderqueue_" + "f" * 32
    fresh = _prepare(params)
    assert audit.audit_semantic_receipt(saved, fresh)["status"] == (
        "LOCAL_SEMANTIC_AUDIT_REJECTED"
    )


def test_malformed_top_level_and_missing_hypotheses_fail_closed(tmp_path):
    params, *_ = _case(tmp_path)
    saved, fresh = _saved(params)
    for mutation in (
        None,
        [],
        {"status": "LOCAL_SEMANTIC_HYPOTHESES_UNQUALIFIED"},
        {**saved, "hypotheses": "corrupt"},
    ):
        output = audit.audit_semantic_receipt(mutation, fresh)
        assert output["status"] == "LOCAL_SEMANTIC_AUDIT_REJECTED"
        assert output["rejection_reasons"]
