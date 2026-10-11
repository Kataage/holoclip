"""Ordering sensitivity of source-cited, unqualified real omission hypotheses.

Reorder two logically identical evidence views, revalidate both responses
against the SAME immutable original case, then compare status and citations.
An ordering-stable hypothesis is not evidence of semantic truth or quality.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.render_real_semantic_contrast import (
    _PROMPT, _MAX_CASES, _fingerprint, _validate_model_contrasts,
    prepare_real_semantic_contrasts, preview_real_semantic_contrasts,
)
from holoclip.render_same_source_semantic_review import _clean

VERSION = "holoclip-real-semantic-order-sensitivity-v1"
DEFAULT_OUTPUT = Path("data/render-previews/real-semantic-order-sensitivity.json")
_MAX_PROVIDER_CALLS = 2 * _MAX_CASES
_ORDERINGS = ("ORIGINAL", "REVERSED")


def _prompt_messages(case: dict, ordering: str) -> list[dict]:
    if ordering not in _ORDERINGS:
        raise ValueError("LOCAL_ORDER_SENSITIVITY_ORDERING_INVALID")
    snippets = case["source_timed_unverified_asr"]
    variants = case["counterfactuals"]
    if ordering == "REVERSED":
        snippets = list(reversed(snippets))
        variants = list(reversed(variants))
    # Evidence ID, source time, literal ASR content and virtual selection
    # effects are NOT altered: only the list presentation order is swapped.
    return [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": json.dumps({
            "source_timed_unverified_asr": snippets,
            "counterfactuals": variants,
            "source_clock_selection_not_audible_truth": True,
        }, ensure_ascii=False, sort_keys=True)},
    ]


def _route(client) -> dict:
    data = getattr(client, "last_provenance", None)
    if not isinstance(data, dict):
        return {"provider": None, "model": None}
    return {
        "provider": _clean(data.get("provider"), 64),
        "model": _clean(data.get("model"), 160),
    }


def _redacted_role(row: dict) -> dict:
    return {
        "omitted_position": row["omitted_position"],
        "status": row["status"],
        "lost_evidence_id": row["lost_evidence_id"],
        "retained_evidence_id": row["retained_evidence_id"],
        "lost_quote_sha256": (
            hashlib.sha256(row["lost_quote"].encode("utf-8")).hexdigest()
            if row["lost_quote"] is not None else None
        ),
        "retained_quote_sha256": (
            hashlib.sha256(row["retained_quote"].encode("utf-8")).hexdigest()
            if row["retained_quote"] is not None else None
        ),
        "source_quote_exactly_verified_against_local_asr": True,
        "semantic_role_truth_verified": False,
    }


def _compare_pair(a: list[dict], b: list[dict], *, route_match: bool) -> list[dict]:
    if len(a) != len(b) or any(
        x["omitted_position"] != y["omitted_position"]
        for x, y in zip(a, b, strict=True)
    ):
        raise ValueError("LOCAL_ORDER_SENSITIVITY_VARIANT_IDENTITY_MISMATCH")
    results = []
    for left, right in zip(a, b, strict=True):
        original = _redacted_role(left)
        reversed_view = _redacted_role(right)
        same_status = left["status"] == right["status"]
        same_ids = (
            left["lost_evidence_id"] == right["lost_evidence_id"]
            and left["retained_evidence_id"] == right["retained_evidence_id"]
        )
        same_quotes = (
            original["lost_quote_sha256"] == reversed_view["lost_quote_sha256"]
            and original["retained_quote_sha256"]
            == reversed_view["retained_quote_sha256"]
        )
        if not route_match:
            verdict = "MODEL_ROUTE_NOT_COMPARABLE"
        elif not same_status:
            verdict = "ORDER_SENSITIVE_STATUS_CHANGE"
        elif not (same_ids and same_quotes):
            verdict = "ORDER_SENSITIVE_CITATION_CHANGE"
        else:
            verdict = "ORDER_INVARIANT_WITHIN_TWO_RUNS"
        results.append({
            "omitted_position": left["omitted_position"],
            "comparison": verdict,
            "status_agrees": same_status if route_match else None,
            "cited_ids_agree": same_ids if route_match else None,
            "quote_hashes_agree": same_quotes if route_match else None,
            "original": original,
            "reversed": reversed_view,
        })
    return results


def preview_real_order_sensitivity(prepared: dict) -> dict:
    preview = preview_real_semantic_contrasts(prepared)
    preview.update({
        "version": VERSION,
        "status": (
            "LOCAL_ORDER_SENSITIVITY_EVIDENCE_PREPARED"
            if prepared.get("status") == "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED"
            else "LOCAL_ORDER_SENSITIVITY_NOT_ELIGIBLE"
        ),
        "source_contrast_version": prepared.get("version"),
        "model_call_upper_bound": min(
            len(prepared.get("cases", [])) * 2, _MAX_PROVIDER_CALLS
        ),
        "ordering_variants": list(_ORDERINGS),
        "actual_model_calls": 0,
        "same_model_route_verified": False,
        "comparable_variant_count": 0,
        "status_change_count": 0,
        "citation_change_count": 0,
        "order_invariant_within_two_runs_count": 0,
        "semantic_accuracy": None,
        "independent_gold_available": False,
        "two_runs_do_not_prove_stability": True,
        "all_quotes_redacted_to_sha256": True,
        "ready": False,
        "production_ready": False,
        "auto_edit_allowed": False,
        "publication_performed": False,
    })
    return preview


async def infer_real_order_sensitivity(prepared: dict, client) -> dict:
    if prepared.get("status") != "LOCAL_REAL_CONTRAST_EVIDENCE_PREPARED":
        return preview_real_order_sensitivity(prepared)
    cases = prepared.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise ValueError("LOCAL_ORDER_SENSITIVITY_CASE_COUNT_INVALID")
    result = preview_real_order_sensitivity(prepared)
    results = []
    for case in cases:
        # If the provider returns malformed quotes in EITHER run, reject the
        # whole result: no partially successful immutable receipt may be saved.
        answers = []
        routes = []
        for ordering in _ORDERINGS:
            response = await client.chat_json(
                _prompt_messages(case, ordering),
                temperature=0, max_tokens=1280,
            )
            answers.append(_validate_model_contrasts(response, case))
            routes.append(_route(client))
        comparable = (
            routes[0]["provider"] is not None
            and routes[0]["model"] is not None
            and routes[0] == routes[1]
        )
        differences = _compare_pair(*answers, route_match=comparable)
        results.append({
            "source_stream_sha256": case["source_stream_sha256"],
            "human_clip_identity_sha256": case["human_clip_identity_sha256"],
            "source_transcript_sha256": case["source_transcript_sha256"],
            "original_provider_route": routes[0],
            "reversed_provider_route": routes[1],
            "provider_route_identity_verified": comparable,
            "variants": differences,
            "comparison_is_not_semantic_accuracy": True,
        })
    rows = [row for case in results for row in case["variants"]]
    result.update({
        "status": "LOCAL_ORDER_SENSITIVITY_TWO_RUNS_UNQUALIFIED",
        # Keep the already redacted SHA-256-only cases from the dry run.
        "actual_model_calls": len(cases) * 2,
        "model_comparison_results": results,
        "same_model_route_verified": all(
            case["provider_route_identity_verified"] for case in results
        ),
        "comparable_variant_count": sum(
            row["comparison"] != "MODEL_ROUTE_NOT_COMPARABLE" for row in rows
        ),
        "status_change_count": sum(
            row["comparison"] == "ORDER_SENSITIVE_STATUS_CHANGE" for row in rows
        ),
        "citation_change_count": sum(
            row["comparison"] == "ORDER_SENSITIVE_CITATION_CHANGE" for row in rows
        ),
        "order_invariant_within_two_runs_count": sum(
            row["comparison"] == "ORDER_INVARIANT_WITHIN_TWO_RUNS" for row in rows
        ),
        "source_evidence_sha256": _fingerprint(cases),
        "ready": False,
        "production_ready": False,
        "auto_edit_allowed": False,
        "publication_performed": False,
    })
    return result
