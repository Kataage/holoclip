"""Offline source re-audit of redacted two-order semantic contrast receipts.

Reconstruct source-clock evidence from today's trusted local corpus and
validate the saved redacted result down to source-quote SHA-256 membership,
provider route and aggregate counters. No claims about AI semantic truth
or authentication of the original receipt's author.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from holoclip.render_real_semantic_contrast import (
    _MAX_CASES, _POSITIONS, _STATES, _fingerprint,
)
from holoclip.render_real_semantic_order_sensitivity import (
    _MAX_PROVIDER_CALLS,
    preview_real_order_sensitivity,
)
from holoclip.render_same_source_semantic_audit import _digest, write_audit
from holoclip.render_same_source_semantic_review import _SHA, _clean

VERSION = "holoclip-real-order-receipt-audit-v1"
_ALLOWED_COMPARISONS = {
    "MODEL_ROUTE_NOT_COMPARABLE",
    "ORDER_SENSITIVE_STATUS_CHANGE",
    "ORDER_SENSITIVE_CITATION_CHANGE",
    "ORDER_INVARIANT_WITHIN_TWO_RUNS",
}
_ROLE_KEYS = {
    "omitted_position", "status", "lost_evidence_id", "retained_evidence_id",
    "lost_quote_sha256", "retained_quote_sha256",
    "source_quote_exactly_verified_against_local_asr",
    "semantic_role_truth_verified",
}
_VARIANT_KEYS = {
    "omitted_position", "comparison", "status_agrees",
    "cited_ids_agree", "quote_hashes_agree", "original", "reversed",
}
_CASE_RESULT_KEYS = {
    "source_stream_sha256", "human_clip_identity_sha256",
    "source_transcript_sha256", "original_provider_route",
    "reversed_provider_route", "provider_route_identity_verified",
    "variants", "comparison_is_not_semantic_accuracy",
}
_MUTABLE_TOP = {
    "status", "actual_model_calls", "same_model_route_verified",
    "comparable_variant_count", "status_change_count",
    "citation_change_count", "order_invariant_within_two_runs_count",
}
_RESULT_KEYS = {"model_comparison_results", "source_evidence_sha256"}
_MAX_ERRORS = 10


def _reason(out: dict, message: str) -> None:
    if message not in out["rejection_reasons"] and len(out["rejection_reasons"]) < _MAX_ERRORS:
        out["rejection_reasons"].append(message)


def _quoted_digest_present(text: str, sha: str) -> bool:
    """Find an exact legal quote's SHA in <=160 Unicode source-ASR chars.

    Receipt intentionally does not contain raw model quotes. This proves
    only a substring with the hash could exist, not the model's intention.
    """
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return False
    # _clean bounds ASR text to 160 chars in the original model request.
    if not isinstance(text, str) or len(text) > 160:
        return False
    for left in range(len(text)):
        for right in range(left + 1, min(len(text), left + 100) + 1):
            quote = text[left:right]
            if _clean(quote, 100) != quote:
                continue
            if hashlib.sha256(quote.encode("utf-8")).hexdigest() == sha:
                return True
    return False


def _check_route(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, dict) or set(value) != {"provider", "model"}:
        raise ValueError("ROUTE_SHAPE_INVALID")
    for key, limit in (("provider", 64), ("model", 160)):
        v = value[key]
        if v is not None and _clean(v, limit) != v:
            raise ValueError("ROUTE_UNTRUSTED")
    return value["provider"], value["model"]


def _check_role(role: object, variant: dict, source: dict) -> None:
    if not isinstance(role, dict) or set(role) != _ROLE_KEYS:
        raise ValueError("REDACTED_ROLE_SCHEMA_INVALID")
    if (role["omitted_position"] != variant["omitted_position"]
            or role["status"] not in _STATES
            or role["source_quote_exactly_verified_against_local_asr"] is not True
            or role["semantic_role_truth_verified"] is not False):
        raise ValueError("REDACTED_ROLE_GATES_INVALID")
    observations = {x["evidence_id"]: x["source_clock_effect"]
                    for x in variant["source_clock_observations"]}
    evidence = {x["id"]: x["unverified_asr_text"]
                for x in source["source_timed_unverified_asr"]}
    lost, retained = role["lost_evidence_id"], role["retained_evidence_id"]
    hash_lost, hash_kept = role["lost_quote_sha256"], role["retained_quote_sha256"]
    if role["status"] != "POSSIBLE_COMPREHENSION_LOSS":
        if any(x is not None for x in (lost, retained, hash_lost, hash_kept)):
            raise ValueError("REDACTED_NULL_CITATION_REQUIRED")
        return
    if (not isinstance(lost, str) or not isinstance(retained, str)
            or lost == retained
            or observations.get(lost) != "LOST"
            or observations.get(retained) != "RETAINED"
            or lost not in evidence or retained not in evidence
            or not _quoted_digest_present(evidence[lost], hash_lost)
            or not _quoted_digest_present(evidence[retained], hash_kept)):
        raise ValueError("REDACTED_QUOTE_OR_SOURCE_EFFECT_NOT_REPRODUCIBLE")


def _check_results(receipt: dict, prepared: dict) -> tuple[int, int, int, int, bool]:
    cases = prepared["cases"]
    saved = receipt.get("model_comparison_results")
    if not isinstance(saved, list) or len(saved) != len(cases):
        raise ValueError("MODEL_COMPARISON_COUNT_INVALID")
    comparable = status_changes = citation_changes = stable = 0
    all_routes_match = True
    for source, entry in zip(cases, saved, strict=True):
        if not isinstance(entry, dict) or set(entry) != _CASE_RESULT_KEYS:
            raise ValueError("MODEL_CASE_SCHEMA_INVALID")
        for identity in (
            "source_stream_sha256", "human_clip_identity_sha256",
            "source_transcript_sha256",
        ):
            if entry[identity] != source[identity]:
                raise ValueError("MODEL_CASE_SOURCE_IDENTITY_CHANGED")
        if entry["comparison_is_not_semantic_accuracy"] is not True:
            raise ValueError("MODEL_CASE_QUALITY_GATE_INVALID")
        first = _check_route(entry["original_provider_route"])
        second = _check_route(entry["reversed_provider_route"])
        route_matches = (
            all(value is not None for value in first)
            and first == second
        )
        if entry["provider_route_identity_verified"] is not route_matches:
            raise ValueError("MODEL_ROUTE_EQUIVALENCE_CHANGED")
        all_routes_match &= route_matches
        variants = source["counterfactuals"]
        rows = entry["variants"]
        if not isinstance(rows, list) or len(rows) != len(variants):
            raise ValueError("VARIANT_COUNT_INVALID")
        expected_order = sorted(variants, key=lambda v: _POSITIONS.index(
            v["omitted_position"]))
        for variant, pair in zip(expected_order, rows, strict=True):
            if not isinstance(pair, dict) or set(pair) != _VARIANT_KEYS:
                raise ValueError("VARIANT_RESULT_SCHEMA_INVALID")
            if (pair["omitted_position"] != variant["omitted_position"]
                    or pair["comparison"] not in _ALLOWED_COMPARISONS):
                raise ValueError("VARIANT_IDENTITY_INVALID")
            left, right = pair["original"], pair["reversed"]
            _check_role(left, variant, source)
            _check_role(right, variant, source)
            same_status = left["status"] == right["status"]
            same_ids = (
                left["lost_evidence_id"] == right["lost_evidence_id"]
                and left["retained_evidence_id"] == right["retained_evidence_id"]
            )
            same_hashes = (
                left["lost_quote_sha256"] == right["lost_quote_sha256"]
                and left["retained_quote_sha256"] == right["retained_quote_sha256"]
            )
            expected_comparison = (
                "MODEL_ROUTE_NOT_COMPARABLE" if not route_matches
                else "ORDER_SENSITIVE_STATUS_CHANGE" if not same_status
                else "ORDER_SENSITIVE_CITATION_CHANGE"
                if not (same_ids and same_hashes)
                else "ORDER_INVARIANT_WITHIN_TWO_RUNS"
            )
            if (pair["comparison"] != expected_comparison
                    or pair["status_agrees"] is not (
                        same_status if route_matches else None
                    )
                    or pair["cited_ids_agree"] is not (
                        same_ids if route_matches else None
                    )
                    or pair["quote_hashes_agree"] is not (
                        same_hashes if route_matches else None
                    )):
                raise ValueError("VARIANT_COMPARISON_COUNTERFEIT")
            if route_matches:
                comparable += 1
                status_changes += not same_status
                citation_changes += (
                    same_status and not (same_ids and same_hashes)
                )
                stable += same_status and same_ids and same_hashes
    return comparable, status_changes, citation_changes, stable, all_routes_match


def audit_real_order_receipt(receipt: object, prepared: dict,
                             *, receipt_sha256: str | None = None) -> dict:
    """Strictly fail closed, no source ASR or quotes in diagnostics."""
    out = {
        "version": VERSION,
        "status": "LOCAL_ORDER_RECEIPT_AUDIT_REJECTED",
        "receipt_sha256": receipt_sha256,
        "fresh_source_evidence_sha256": None,
        "verified_source_count": 0,
        "verified_variant_count": 0,
        "verbatim_quote_hashes_locally_reproducible": False,
        "rejection_reasons": [],
        "source_evidence_checked_against_current_local_files": False,
        "receipt_hash_does_not_authenticate_author": True,
        "two_order_samples_not_semantic_truth": True,
        "real_semantic_accuracy": None,
        "independent_gold_available": False,
        "ready": False,
        "production_ready": False,
        "auto_edit_allowed": False,
        "publication_performed": False,
    }
    if (not isinstance(prepared, dict)
            or prepared.get("status") != "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED"
            or prepared.get("ready") is not False
            or prepared.get("auto_edit_allowed") is not False
            or prepared.get("independent_gold_available") is not False
            or not isinstance(prepared.get("cases"), list)
            or not 1 <= len(prepared["cases"]) <= _MAX_CASES):
        _reason(out, "FRESH_SOURCE_EVIDENCE_NOT_ELIGIBLE")
        return out
    expected = preview_real_order_sensitivity(prepared)
    fresh_hash = _fingerprint(prepared["cases"])
    out["fresh_source_evidence_sha256"] = fresh_hash
    if not isinstance(receipt, dict):
        _reason(out, "RECEIPT_NOT_OBJECT")
        return out
    allowed = set(expected) | _RESULT_KEYS
    if set(receipt) != allowed:
        _reason(out, "RECEIPT_FIELDS_INVALID")
        return out
    # Re-derive every non-result field, including every ASR text hash, source
    # transcript hash, holdout state hash, page scope and qualified flags.
    for key, value in expected.items():
        if key in _MUTABLE_TOP:
            continue
        if receipt[key] != value:
            _reason(out, "SOURCE_EVIDENCE_CHANGED_OR_RECEIPT_MODIFIED")
            return out
    if (receipt["status"] != "LOCAL_ORDER_SENSITIVITY_TWO_RUNS_UNQUALIFIED"
            or type(receipt["actual_model_calls"]) is not int
            or receipt["actual_model_calls"] != 2 * len(prepared["cases"])
            or receipt["actual_model_calls"] > _MAX_PROVIDER_CALLS
            or receipt["source_evidence_sha256"] != fresh_hash):
        _reason(out, "RECEIPT_RUN_COUNTS_OR_EVIDENCE_INVALID")
        return out
    try:
        counts = _check_results(receipt, prepared)
    except (ValueError, TypeError, KeyError):
        _reason(out, "MODEL_QUOTATIONS_ROUTES_OR_COMPARISONS_INVALID")
        return out
    comparable, status_flip, citation_flip, stable, route_match = counts
    if (type(receipt["same_model_route_verified"]) is not bool
            or receipt["same_model_route_verified"] is not route_match
            or any(type(receipt[key]) is not int or receipt[key] != count
                   for key, count in (
                       ("comparable_variant_count", comparable),
                       ("status_change_count", status_flip),
                       ("citation_change_count", citation_flip),
                       ("order_invariant_within_two_runs_count", stable),
                   ))):
        _reason(out, "AGGREGATE_COUNTS_OR_ROUTE_STATUS_INVALID")
        return out
    out.update({
        "status": "LOCAL_ORDER_RECEIPT_AUDIT_CONSISTENT_UNQUALIFIED",
        "verified_source_count": len(prepared["cases"]),
        "verified_variant_count": sum(
            len(case["counterfactuals"]) for case in prepared["cases"]
        ),
        "verbatim_quote_hashes_locally_reproducible": True,
        "source_evidence_checked_against_current_local_files": True,
    })
    return out


def audit_real_order_receipt_file(receipt_path: Path, prepared: dict) -> dict:
    try:
        saved, sha = _digest(receipt_path)
    except (OSError, ValueError, TypeError, UnicodeError):
        return audit_real_order_receipt(None, prepared)
    return audit_real_order_receipt(saved, prepared, receipt_sha256=sha)


def save_real_order_audit(report: dict, output: Path) -> None:
    """Delegate to existing bounded, create-only and link-safe audit writer."""
    write_audit(report, output)
