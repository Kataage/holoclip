"""Re-audit SHA-redacted two-order receipts using original local ASR only."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_real_semantic_contrast as contrast
from holoclip import render_real_semantic_order_audit as audit
from holoclip import render_real_semantic_order_sensitivity as ordering
from holoclip.corpus_growth import CorpusGrowthState
from test_render_real_semantic_order_sensitivity import _case, _fake_client, _args
from test_render_real_semantic_contrast import _paths
from test_render_ready_queue import VIDEO


async def _saved(tmp_path, **model_args):
    params, prepared, media = _case(tmp_path)
    receipt = await ordering.infer_real_order_sensitivity(
        prepared, _fake_client(prepared, **model_args),
    )
    return params, prepared, media, receipt


@pytest.mark.asyncio
async def test_offline_audit_reproduces_source_quotes_without_disclosing_text(tmp_path):
    params, prepared, media, receipt = await _saved(tmp_path)
    before = media.read_bytes()
    got = audit.audit_real_order_receipt(receipt, prepared)
    assert got["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_CONSISTENT_UNQUALIFIED"
    assert got["verified_source_count"] == 1
    assert got["verified_variant_count"] == 3
    assert got["verbatim_quote_hashes_locally_reproducible"] is True
    assert got["source_evidence_checked_against_current_local_files"] is True
    assert got["receipt_hash_does_not_authenticate_author"] is True
    assert got["two_order_samples_not_semantic_truth"] is True
    assert got["real_semantic_accuracy"] is None
    assert got["independent_gold_available"] is False
    assert got["ready"] is False and got["production_ready"] is False
    assert got["auto_edit_allowed"] is False
    assert got["publication_performed"] is False
    assert "導入部分" not in json.dumps(got, ensure_ascii=False)
    assert VIDEO not in json.dumps(got)
    assert media.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "different_status", "different_quote", "different_route",
])
async def test_all_valid_two_run_sensitivity_categories_reaudit(tmp_path, mode):
    _, prepared, _, receipt = await _saved(tmp_path, **{mode: True})
    got = audit.audit_real_order_receipt(receipt, prepared)
    assert got["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_CONSISTENT_UNQUALIFIED"
    assert got["verified_variant_count"] == 3
    assert got["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [
    "top_level_approval", "ready_flag", "max_cases", "run_count",
    "source_hash", "source_transcript_digest", "holdout_digest",
    "source_excerpt_digest", "model_route", "route_comparable",
    "route_unknown_field", "status", "lost_evidence", "retained_evidence",
    "quote_digest", "quote_digest_format", "quality_flag", "role_extra",
    "comparison", "comparison_truth", "aggregate_stable",
    "aggregate_comparable", "aggregate_status_flip",
    "duplicate_variant", "removed_variant", "omitted_position",
    "source_identity", "case_count", "no_quote_with_positive_status",
])
async def test_receipt_tampering_never_passes_offline_reaudit(tmp_path, tamper):
    _, prepared, _, base = await _saved(tmp_path)
    receipt = copy.deepcopy(base)
    data = receipt["model_comparison_results"][0]
    variant = data["variants"][0]
    left = variant["original"]
    if tamper == "top_level_approval":
        receipt["creative_quality_approved"] = True
    elif tamper == "ready_flag":
        receipt["ready"] = True
    elif tamper == "max_cases":
        receipt["max_cases"] = 3
    elif tamper == "run_count":
        receipt["actual_model_calls"] = 1
    elif tamper == "source_hash":
        receipt["source_evidence_sha256"] = "f" * 64
    elif tamper == "source_transcript_digest":
        receipt["cases"][0]["source_transcript_sha256"] = "a" * 64
    elif tamper == "holdout_digest":
        receipt["holdout_state_sha256"] = "a" * 64
    elif tamper == "source_excerpt_digest":
        receipt["cases"][0]["source_timed_unverified_asr"][0][
            "asr_text_sha256"] = "a" * 64
    elif tamper == "model_route":
        data["reversed_provider_route"]["model"] = "other"
    elif tamper == "route_comparable":
        data["provider_route_identity_verified"] = False
    elif tamper == "route_unknown_field":
        data["original_provider_route"]["api_key"] = "secret"
    elif tamper == "status":
        left["status"] = "NO_CLEAR_LOSS_FOUND"
    elif tamper == "lost_evidence":
        left["lost_evidence_id"] = left["retained_evidence_id"]
    elif tamper == "retained_evidence":
        left["retained_evidence_id"] = left["lost_evidence_id"]
    elif tamper == "quote_digest":
        left["lost_quote_sha256"] = hashlib.sha256(b"invented").hexdigest()
    elif tamper == "quote_digest_format":
        left["lost_quote_sha256"] = "unhashed"
    elif tamper == "quality_flag":
        left["semantic_role_truth_verified"] = True
    elif tamper == "role_extra":
        left["quality_approved"] = True
    elif tamper == "comparison":
        variant["comparison"] = "ORDER_INVARIANT_WITHIN_TWO_RUNS_FAKE"
    elif tamper == "comparison_truth":
        variant["quote_hashes_agree"] = False
    elif tamper == "aggregate_stable":
        receipt["order_invariant_within_two_runs_count"] = 999
    elif tamper == "aggregate_comparable":
        receipt["comparable_variant_count"] = 0
    elif tamper == "aggregate_status_flip":
        receipt["status_change_count"] = 1
    elif tamper == "duplicate_variant":
        data["variants"].append(copy.deepcopy(variant))
    elif tamper == "removed_variant":
        data["variants"].pop()
    elif tamper == "omitted_position":
        variant["omitted_position"] = "unexpected"
    elif tamper == "source_identity":
        data["source_stream_sha256"] = "a" * 64
    elif tamper == "case_count":
        receipt["model_comparison_results"] = []
    elif tamper == "no_quote_with_positive_status":
        left["lost_quote_sha256"] = None
    checked = audit.audit_real_order_receipt(receipt, prepared)
    assert checked["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    assert checked["rejection_reasons"]
    assert checked["source_evidence_checked_against_current_local_files"] is False
    assert checked["ready"] is False
    assert "invented" not in json.dumps(checked)


@pytest.mark.asyncio
async def test_local_asr_content_revision_invalidates_saved_receipt(tmp_path):
    params, old, _, saved = await _saved(tmp_path)
    path = params["transcript_root"] / "sources" / f"{VIDEO}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["segments"][0]["text"] = "別の文字起こしに変更"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    fresh = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert fresh["cases"]
    assert contrast._fingerprint(fresh["cases"]) != (
        contrast._fingerprint(old["cases"])
    )
    got = audit.audit_real_order_receipt(saved, fresh)
    assert got["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    assert "SOURCE_EVIDENCE_CHANGED_OR_RECEIPT_MODIFIED" in (
        got["rejection_reasons"]
    )


@pytest.mark.asyncio
async def test_new_holdout_or_missing_transcript_blocks_existing_receipt(tmp_path):
    params, _, _, saved = await _saved(tmp_path)
    path = params["transcript_root"] / "sources" / f"{VIDEO}.json"
    path.unlink()
    missing = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert audit.audit_real_order_receipt(saved, missing)["rejection_reasons"] == [
        "FRESH_SOURCE_EVIDENCE_NOT_ELIGIBLE"
    ]
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    blocked = contrast.prepare_real_semantic_contrasts(**_paths(params))
    assert audit.audit_real_order_receipt(saved, blocked)["status"] == (
        "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    )


@pytest.mark.parametrize("text,quote", [
    ("abc", "bc"),
    ("ab ab", "ab"),
    ("かなたの会話", "なた"),
])
def test_hash_reproduces_only_actual_source_substrings(text, quote):
    sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    assert audit._quoted_digest_present(text, sha) is True
    assert audit._quoted_digest_present(text, "f" * 64) is False
    assert audit._quoted_digest_present(text, sha.upper()) is False


@pytest.mark.asyncio
async def test_cli_default_read_only_reaudit_and_create_only_sidecar(tmp_path, monkeypatch):
    params, prepared, media, saved = await _saved(tmp_path)
    original = media.read_bytes()
    receipt_path = tmp_path / "two-runs.json"
    receipt_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    sidecar = tmp_path / "new-audit.json"
    class NoAI:
        def __init__(self, *args, **kwargs):
            pytest.fail("Order receipt audit must never call AI")
    monkeypatch.setattr(cli, "MeshLLMClient", NoAI)
    args = _args(params, "--receipt", str(receipt_path))
    # Audit reads a previously saved report, without a second model call.
    audit_args = ["render-real-semantic-order-audit", *args[1:]]
    read = CliRunner().invoke(cli.app, audit_args)
    assert read.exit_code == 0, read.stdout
    assert not sidecar.exists()
    result = json.loads(read.stdout)
    assert result["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_CONSISTENT_UNQUALIFIED"
    assert result["receipt_sha256"] is not None
    created = CliRunner().invoke(cli.app, [*audit_args, "--output", str(sidecar)])
    assert created.exit_code == 0, created.stdout
    assert json.loads(sidecar.read_text(encoding="utf-8")) == result
    repeated = CliRunner().invoke(cli.app, [*audit_args, "--output", str(sidecar)])
    assert repeated.exit_code == 6
    assert json.loads(sidecar.read_text(encoding="utf-8")) == result
    assert media.read_bytes() == original
    assert prepared["ready"] is False


@pytest.mark.asyncio
async def test_cli_tampered_report_is_nonzero_and_does_not_echo_quotes(tmp_path):
    params, _, _, saved = await _saved(tmp_path)
    saved["model_comparison_results"][0]["variants"][0][
        "original"]["lost_quote_sha256"] = hashlib.sha256(
            b"fictional wording"
        ).hexdigest()
    file = tmp_path / "tampered.json"
    file.write_text(json.dumps(saved), encoding="utf-8")
    args = [
        "render-real-semantic-order-audit", "--receipt", str(file),
        "--example-store", str(params["example_store"]),
        "--growth-state", str(params["growth_state"]),
        "--transcript-root", str(params["transcript_root"]),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 6
    diag = json.loads(result.stdout)
    assert diag["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    assert diag["ready"] is False
    assert "導入部分" not in result.stdout


@pytest.mark.asyncio
async def test_missing_invalid_and_oversized_receipts_fail_closed(tmp_path):
    params, prepared, _, _ = await _saved(tmp_path)
    missing = tmp_path / "missing.json"
    assert audit.audit_real_order_receipt_file(missing, prepared)["status"] == (
        "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    )
    broken = tmp_path / "bad.json"
    broken.write_text("not JSON", encoding="utf-8")
    assert audit.audit_real_order_receipt_file(broken, prepared)["status"] == (
        "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    )
    broken.write_bytes(b"x" * 300_000)
    assert audit.audit_real_order_receipt_file(broken, prepared)["status"] == (
        "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED"
    )


@pytest.mark.asyncio
async def test_report_not_self_authenticating_by_file_digest_alone(tmp_path):
    _, prepared, _, saved = await _saved(tmp_path)
    got = audit.audit_real_order_receipt(
        saved, prepared, receipt_sha256="f" * 64,
    )
    # A supplied file checksum is an identifier, not an authenticated signature.
    assert got["status"] == "LOCAL_ORDER_RECEIPT_AUDIT_CONSISTENT_UNQUALIFIED"
    assert got["receipt_hash_does_not_authenticate_author"] is True
    assert got["real_semantic_accuracy"] is None
