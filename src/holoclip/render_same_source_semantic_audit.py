"""Offline re-audit of an immutable semantic hypothesis receipt against live local evidence.

Claims only that the model's cited string exists in an already-local,
unverified ASR excerpt and that the receipt has not drifted from today's
same-source/holdout-screened evidence. This cannot judge semantic truth.
No network, models, yt-dlp, data mutations, or quality promotion.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from holoclip.render_plan import _no_links
from holoclip.render_human_edit_context import _read_bounded
from holoclip.render_same_source_semantic_review import (
    _MAX_CASES, _SHA, _JOB, _ROLES, _validate_candidate,
    prepared_fingerprint, validate_semantic_output,
)

VERSION = "holoclip-semantic-citation-audit-v1"
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_AUDIT_BYTES = 16 * 1024
_MAX_ERRORS = 12
_IDENTITY_KEYS = (
    "job_id", "source_stream_sha256", "human_clip_identity_sha256",
    "local_transcript_sha256",
)
_RECEIPT_STATES = {
    "LOCAL_SEMANTIC_HYPOTHESES_UNQUALIFIED",
}
_REPORT_ITEMS = {
    "role", "status", "evidence_id", "quote",
    "model_reference_not_independently_verified",
}
_CITATION_ITEMS = _REPORT_ITEMS | {
    "source_start_seconds", "source_end_seconds",
    "source_window_selection_side",
}


def _digest(path: Path) -> tuple[dict, str]:
    """Reject symlinks and oversized or malformed saved responses."""
    path = _no_links(path)
    if path.suffix.lower() != ".json":
        raise ValueError("LOCAL_SEMANTIC_AUDIT_RECEIPT_EXTENSION")
    raw = _read_bounded(path, _MAX_RECEIPT_BYTES)
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("LOCAL_SEMANTIC_AUDIT_RECEIPT_FORMAT")
    return document, hashlib.sha256(raw).hexdigest()


def _reject(out: dict, reason: str) -> None:
    if reason not in out["rejection_reasons"] and len(out["rejection_reasons"]) < _MAX_ERRORS:
        out["rejection_reasons"].append(reason)


def _validate_saved_roles(roles: object, source_case: dict) -> tuple[int, int]:
    if not isinstance(roles, list) or len(roles) != len(_ROLES):
        raise ValueError("LOCAL_SEMANTIC_AUDIT_ROLE_COUNT")
    payload = []
    for row in roles:
        if not isinstance(row, dict):
            raise ValueError("LOCAL_SEMANTIC_AUDIT_ROLE_FORMAT")
        has_citation = row.get("status") == "POSSIBLE_TEXTUAL_REFERENCE"
        expected_keys = _CITATION_ITEMS if has_citation else _REPORT_ITEMS
        if set(row) != expected_keys:
            raise ValueError("LOCAL_SEMANTIC_AUDIT_ROLE_FIELDS")
        payload.append({field: row[field] for field in (
            "role", "status", "evidence_id", "quote"
        )})
    validated = _validate_candidate({"roles": payload}, source_case)
    if validated != roles:
        # Even timestamp and side fields must be reproduced from the CURRENT
        # source evidence rather than accepted from a self-signed receipt.
        raise ValueError("LOCAL_SEMANTIC_AUDIT_ROLE_SOURCE_FIELDS_CHANGED")
    return (
        sum(x["status"] == "POSSIBLE_TEXTUAL_REFERENCE" for x in validated),
        sum(x["status"] == "INSUFFICIENT_EVIDENCE" for x in validated),
    )


def audit_semantic_receipt(
    receipt: object, fresh: dict, *, receipt_sha256: str | None = None,
) -> dict:
    """Read-only exact regeneration comparison; counts are NOT precision/recall."""
    output = {
        "version": VERSION,
        "status": "LOCAL_SEMANTIC_AUDIT_REJECTED",
        "receipt_sha256": receipt_sha256,
        "fresh_evidence_sha256": None,
        "case_count_checked": 0,
        "verbatim_source_citations_checked": 0,
        "insufficient_evidence_roles": 0,
        "rejection_reasons": [],
        "rechecked_against_current_local_source": False,
        "citation_string_match_not_semantic_truth": True,
        "saved_model_output_not_independent_gold": True,
        "human_training_references_not_holdout_gold": True,
        "narrative_quality_precision": None,
        "narrative_quality_recall": None,
        "narrative_roles_verified": False,
        "ready": False,
        "production_ready": False,
        "auto_edit_allowed": False,
        "publication_performed": False,
    }
    if fresh.get("status") != "LOCAL_SEMANTIC_EVIDENCE_PREPARED":
        _reject(output, "LOCAL_EVIDENCE_NOT_ELIGIBLE")
        return output
    if (fresh.get("ready") is not False
            or fresh.get("production_ready") is not False
            or fresh.get("human_training_references_not_holdout_gold") is not True
            or fresh.get("external_inference_performed") is not False
            or not isinstance(fresh.get("cases"), list)
            or not 1 <= len(fresh["cases"]) <= _MAX_CASES):
        _reject(output, "LOCAL_EVIDENCE_GATES_UNVERIFIED")
        return output
    live_hash = prepared_fingerprint(fresh)
    output["fresh_evidence_sha256"] = live_hash
    if not isinstance(receipt, dict):
        _reject(output, "RECEIPT_NOT_OBJECT")
        return output
    gate_fields = {
        "status": _RECEIPT_STATES,
        "ready": {False},
        "production_ready": {False},
        "auto_edit_allowed": {False},
        "publication_performed": {False},
        "human_training_references_not_holdout_gold": {True},
        "model_statements_are_hypotheses_not_quality_labels": {True},
        "external_inference_performed": {True},
    }
    for key, expected in gate_fields.items():
        value = receipt.get(key)
        if type(value) is not bool and value not in expected:
            _reject(output, "RECEIPT_GATES_INVALID")
            break
        if type(value) is bool and value not in expected:
            _reject(output, "RECEIPT_GATES_INVALID")
            break
    if (receipt.get("version") != fresh["version"]
            or receipt.get("source_comparison_version")
            != fresh.get("source_comparison_version")
            or receipt.get("max_cases") != _MAX_CASES):
        _reject(output, "RECEIPT_VERSION_OR_BOUNDS_CHANGED")
    try:
        saved_hash = receipt["evidence_sha256"]
        saved_cases = receipt["cases"]
        hypotheses = receipt["hypotheses"]
        if (not isinstance(saved_hash, str)
                or not _SHA.fullmatch(saved_hash)
                or saved_hash != live_hash
                or saved_cases != fresh["cases"]):
            _reject(output, "SOURCE_EVIDENCE_CHANGED_OR_TAMPERED")
        if (not isinstance(hypotheses, list)
                or len(hypotheses) != len(fresh["cases"])
                or len(hypotheses) > _MAX_CASES):
            _reject(output, "HYPOTHESIS_COUNT_INVALID")
    except (KeyError, TypeError):
        _reject(output, "RECEIPT_STRUCTURE_INVALID")
        return output
    if output["rejection_reasons"]:
        return output
    for index, (case, model) in enumerate(
        zip(fresh["cases"], hypotheses, strict=True),
    ):
        if not isinstance(model, dict):
            _reject(output, "HYPOTHESIS_FORMAT_INVALID")
            continue
        if (set(model) != set(_IDENTITY_KEYS) | {
                "provider", "model", "role_hypotheses",
                "evidence_is_unverified_asr", "quality_qualification_allowed",
                "auto_edit_allowed",
            } or any(model.get(k) != case.get(k) for k in _IDENTITY_KEYS)
                or model.get("evidence_is_unverified_asr") is not True
                or model.get("quality_qualification_allowed") is not False
                or model.get("auto_edit_allowed") is not False
                or not isinstance(model.get("job_id"), str)
                or not _JOB.fullmatch(model["job_id"])):
            _reject(output, "HYPOTHESIS_IDENTITY_OR_GATES_INVALID")
            continue
        for name, max_length in (("provider", 64), ("model", 160)):
            value = model.get(name)
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > max_length
                or any(ord(char) < 32 for char in value)
            ):
                _reject(output, "MODEL_PROVENANCE_UNTRUSTED")
                break
        if output["rejection_reasons"]:
            continue
        try:
            cited, missing = _validate_saved_roles(model.get("role_hypotheses"), case)
        except (ValueError, KeyError, TypeError):
            _reject(output, "CITATION_NOT_REPRODUCIBLE")
            continue
        output["case_count_checked"] += 1
        output["verbatim_source_citations_checked"] += cited
        output["insufficient_evidence_roles"] += missing
    if output["rejection_reasons"]:
        return output
    output["status"] = "LOCAL_SEMANTIC_AUDIT_CITATIONS_CONSISTENT_UNQUALIFIED"
    output["rechecked_against_current_local_source"] = True
    return output


def audit_receipt_file(receipt_path: Path, fresh: dict) -> dict:
    try:
        receipt, digest = _digest(receipt_path)
    except (OSError, ValueError, TypeError, UnicodeError):
        return audit_semantic_receipt(None, fresh)
    return audit_semantic_receipt(receipt, fresh, receipt_sha256=digest)


def write_audit(report: dict, output: Path) -> None:
    """Optional new local JSON only, no overwrite, no external requests."""
    target = validate_semantic_output(output)
    encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_AUDIT_BYTES:
        raise ValueError("LOCAL_SEMANTIC_AUDIT_TOO_LARGE")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".holoclip-semantic-audit-",
                                     dir=target.parent) as work:
        staged = Path(work) / "audit.json"
        staged.write_bytes(encoded)
        try:
            os.link(staged, target)
        except OSError as exc:
            raise ValueError("LOCAL_SEMANTIC_AUDIT_OUTPUT_EXISTS") from exc
