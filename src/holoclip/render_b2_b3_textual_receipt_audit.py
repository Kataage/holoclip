"""Offline re-audit of redacted B2/B3 textual-dependency model receipts.

Rebuild current SHA-pinned source/B2/B3 playback and ASR evidence, then
validate every saved source-quote hash as a legal substring of today's
source ASR. This proves only structural consistency, not who wrote the
receipt, audible words, editorial semantics, preference or humor.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.render_b2_b3_textual_dependency import (
    _fingerprint, _STATUSES, prepare_textual_dependency,
    preview_textual_dependency,
)
from holoclip.render_human_edit_context import DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE
from holoclip.render_plan import _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_real_semantic_order_audit import _quoted_digest_present
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    _clean, save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE

VERSION = "holoclip-b2-b3-textual-receipt-audit-v1"
_MAX_RECEIPT = 256 * 1024
_EXTRA = {
    "model_request_count", "provider", "model", "model_hypotheses",
    "source_asr_still_not_ground_truth",
    "creative_preference_or_audible_speech_unmeasured",
}
_COMMON = {
    "pair_id", "status", "focus_variant_with_unique_reference",
    "unique_source_asr_id", "shared_source_asr_id",
    "model_relation_not_verified_semantics",
}
_QUOTE_HASHES = {"unique_quote_sha256", "shared_quote_sha256"}
_QUOTE_CLOCKS = {
    "unique_source_start_seconds", "unique_source_end_seconds",
    "shared_source_start_seconds", "shared_source_end_seconds",
}


def _safe_receipt(path: Path) -> tuple[dict, str]:
    """Bound size and refuse symlink paths before accepting saved AI JSON."""
    source = _no_links(Path(path))
    if (not source.is_file() or source.is_symlink()
            or not 0 < source.stat().st_size <= _MAX_RECEIPT):
        raise ValueError("TEXTUAL_AUDIT_RECEIPT_MISSING_OR_UNSAFE")
    raw = source.read_bytes()
    if not 0 < len(raw) <= _MAX_RECEIPT:
        raise ValueError("TEXTUAL_AUDIT_RECEIPT_TOO_LARGE_OR_CHANGED")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("TEXTUAL_AUDIT_RECEIPT_INVALID_JSON") from exc
    if source.read_bytes() != raw:
        raise ValueError("TEXTUAL_AUDIT_RECEIPT_CHANGED_DURING_READ")
    if not isinstance(obj, dict):
        raise ValueError("TEXTUAL_AUDIT_RECEIPT_NOT_OBJECT")
    return obj, hashlib.sha256(raw).hexdigest()


def _validate_saved_result(saved: dict, prepared: dict) -> dict:
    """Reject changed evidence, fake quote hashes, or claimed editorial passes."""
    if prepared["status"] != "LOCAL_TEXTUAL_DEPENDENCY_ASR_PAIRS_PREPARED":
        raise ValueError("TEXTUAL_AUDIT_CURRENT_EVIDENCE_NOT_ELIGIBLE")
    expected = preview_textual_dependency(prepared)
    if (set(saved) != set(expected) | _EXTRA
            or saved.get("status") != "LOCAL_TEXTUAL_DEPENDENCY_HYPOTHESES_UNQUALIFIED"
            or any(saved[k] != v for k, v in expected.items() if k != "status")
            or saved.get("model_request_count") != 1
            or type(saved.get("model_request_count")) is not int
            or saved.get("source_asr_still_not_ground_truth") is not True
            or saved.get("creative_preference_or_audible_speech_unmeasured") is not True):
        raise ValueError("TEXTUAL_AUDIT_SAVED_LINEAGE_OR_GATES_CHANGED")
    for field, length in (("provider", 64), ("model", 160)):
        value = saved[field]
        if value is not None and (type(value) is not str or _clean(value, length) != value):
            raise ValueError("TEXTUAL_AUDIT_PROVIDER_ROUTE_UNTRUSTED")
    pairs = {x["pair_id"]: x for x in prepared["reference_pairs"]}
    observations = saved.get("model_hypotheses")
    if (not isinstance(observations, list)
            or len(observations) != len(pairs)
            or not 1 <= len(pairs) <= 4):
        raise ValueError("TEXTUAL_AUDIT_MODEL_PAIR_COUNT_CHANGED")
    seen: set[str] = set()
    possible = insufficient = 0
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("TEXTUAL_AUDIT_MODEL_ITEM_INVALID")
        identity = item.get("pair_id")
        status = item.get("status")
        if (type(identity) is not str or identity not in pairs
                or identity in seen or status not in _STATUSES):
            raise ValueError("TEXTUAL_AUDIT_MODEL_ID_OR_STATUS_INVALID")
        seen.add(identity)
        pair = pairs[identity]
        for field, value in (
            ("focus_variant_with_unique_reference",
             pair["focus_variant_with_unique_reference"]),
            ("unique_source_asr_id", pair["unique"]["id"]),
            ("shared_source_asr_id", pair["shared"]["id"]),
            ("model_relation_not_verified_semantics", True),
        ):
            if item.get(field) != value:
                raise ValueError("TEXTUAL_AUDIT_MODEL_EVIDENCE_CHANGED")
        if status == "INSUFFICIENT_EVIDENCE":
            if (set(item) != _COMMON | _QUOTE_HASHES
                    or any(item[field] is not None for field in _QUOTE_HASHES)):
                raise ValueError("TEXTUAL_AUDIT_ABSTENTION_FIELDS_INVALID")
            insufficient += 1
            continue
        if set(item) != _COMMON | _QUOTE_HASHES | _QUOTE_CLOCKS:
            raise ValueError("TEXTUAL_AUDIT_QUOTED_FIELDS_INVALID")
        for kind in ("unique", "shared"):
            excerpt = pair[kind]
            if (item[f"{kind}_source_start_seconds"]
                    != excerpt["source_start_seconds"]
                    or item[f"{kind}_source_end_seconds"]
                    != excerpt["source_end_seconds"]
                    or not _quoted_digest_present(
                        excerpt["unverified_source_asr_text"],
                        item[f"{kind}_quote_sha256"],
                    )):
                raise ValueError("TEXTUAL_AUDIT_QUOTE_NOT_REPRODUCIBLE")
        possible += 1
    return {
        "version": VERSION,
        "status": "LOCAL_TEXTUAL_RECEIPT_CONSISTENT_UNQUALIFIED",
        "editorial_evidence_sha256": prepared["editorial_evidence_sha256"],
        "reference_pairs_sha256": _fingerprint(prepared["reference_pairs"]),
        "pair_count": len(pairs),
        "possible_textual_dependency_count": possible,
        "insufficient_evidence_count": insufficient,
        "provider_route": {
            "provider": saved["provider"], "model": saved["model"],
        },
        "redacted_quotes_consistent_with_current_source_asr": True,
        "receipt_authenticity_or_author_not_verified": True,
        "source_asr_not_ground_truth": True,
        "sampled_focus_absence_not_full_clip_absence": True,
        "textual_dependency_not_verified_story_or_creative_quality": True,
        "speech_naturalness_score": None,
        "narrative_continuity_score": None,
        "payoff_completion_score": None,
        "automatic_winner": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "new_model_requests": 0,
        "new_video_decodes_or_downloads": 0,
        "ready": False, "production_ready": False,
    }


def audit_textual_receipt(
    receipt: Path, *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, output: Path | None = None,
) -> dict:
    """Always audit offline; optionally create an immutable redacted receipt."""
    params = dict(
        queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root, job_id=job_id,
        proposal_index=proposal_index, refinement_index=refinement_index,
        candidate_index=candidate_index, target_index=target_index,
    )
    def inspect() -> dict:
        stored, digest = _safe_receipt(receipt)
        current = prepare_textual_dependency(**params)
        checked = _validate_saved_result(stored, current)
        checked["saved_model_receipt_sha256"] = digest
        return checked

    result = inspect()
    if output is not None:
        target = validate_semantic_output(Path(output))
        if inspect() != result:
            raise ValueError("TEXTUAL_AUDIT_INPUT_CHANGED_BEFORE_SAVE")
        save_semantic_review(result, target)
    return result
