"""Non-executable edit-review triage proposals from immutable local evidence.

Priorities represent inspection *order*, not likelihood of an edit defect.
There is no repair executable, FFmpeg command, video mutation, or publication.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from holoclip.render_plan import _no_links
from holoclip.render_quality_review import REVIEW_VERSION
from holoclip.render_source_context import SOURCE_CONTEXT_VERSION

TRIAGE_VERSION = "holoclip-nonexecuting-edit-triage-v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_INPUT = 65536
_MAX_OUTPUT = 65536
_MAX_ITEMS = 300

_CODES = {
    "blackout": "COMPARE_DARK_FRAMES_WITH_ORIGINAL_AND_CHECK_INTENDED_FADE",
    "freeze": "COMPARE_STATIC_FRAMES_AND_CHECK_INTENDED_HOLD",
    "fast_subtitle": "INSPECT_SRT_CUE_TIMING_AND_READABILITY",
    "wide_subtitle": "INSPECT_SUBTITLE_WRAP_IN_PREVIEW",
    "audio_peak": "LISTEN_TO_SAMPLED_AUDIO_BEFORE_PROPOSING_GAIN_CHANGE",
    "audio_low_mean": "LISTEN_TO_SAMPLED_AUDIO_BEFORE_PROPOSING_GAIN_CHANGE",
}
_CONTEXT_CODES = {
    "SIMILAR_SIGNAL_OBSERVED_IN_SOURCE_WINDOW": (
        "SOURCE_SIGNAL_ALSO_OBSERVED", "CHECK_ORIGINAL_ARTISTIC_INTENT", 2
    ),
    "NO_SIMILAR_SIGNAL_IN_SAMPLED_SOURCE_WINDOW": (
        "SOURCE_SIGNAL_NOT_OBSERVED", "COMPARE_RENDER_AND_SOURCE_SIDE_BY_SIDE", 0
    ),
    "SKIPPED_BOUNDARY_OR_SHORT_OBSERVATION": (
        "INCONCLUSIVE_SOURCE_SAMPLE", "INSPECT_CUT_BOUNDARY_WITHOUT_AUTOFIX", 1
    ),
    "SOURCE_WINDOW_UNAVAILABLE": (
        "INCONCLUSIVE_SOURCE_SAMPLE", "INSPECT_SOURCE_AVAILABILITY_WITHOUT_AUTOFIX", 1
    ),
}
_BUCKETS = ("REVIEW_FIRST", "REVIEW_NEEDED", "REVIEW_WITH_SOURCE_CONTEXT")


def _load_hash_verified(path: Path, expected: str) -> dict:
    path = _no_links(path)
    if (not path.is_file() or not _SHA.fullmatch(expected)
            or not 0 < path.stat().st_size <= _MAX_INPUT):
        raise ValueError("LOCAL_EDIT_TRIAGE_EVIDENCE_MISSING")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("LOCAL_EDIT_TRIAGE_EVIDENCE_CHANGED")
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("LOCAL_EDIT_TRIAGE_EVIDENCE_INVALID")
    return obj


def _validated_review(review: dict) -> list[dict]:
    candidates = review.get("candidates")
    if (review.get("version") != REVIEW_VERSION
            or review.get("ready") is not False
            or review.get("production_ready") is not False
            or review.get("human_editorial_qa_completed") is not False
            or review.get("publication_performed") is not False
            or not isinstance(review.get("base_sha256"), str)
            or not _SHA.fullmatch(review["base_sha256"])
            or not isinstance(review.get("final_sha256"), str)
            or not _SHA.fullmatch(review["final_sha256"])
            or not isinstance(candidates, list)
            or len(candidates) > _MAX_ITEMS
            or type(review.get("candidate_count")) is not int
            or review["candidate_count"] != len(candidates)):
        raise ValueError("LOCAL_EDIT_TRIAGE_REVIEW_INVALID")
    checked = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("LOCAL_EDIT_TRIAGE_CANDIDATE_INVALID")
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        segments = item.get("segment_ids")
        if (item.get("kind") not in _CODES
                or type(start) not in (float, int)
                or not math.isfinite(start) or not 0 <= start <= 181
                or end is not None and (
                    type(end) not in (float, int) or not math.isfinite(end)
                    or not start <= end <= 182
                )
                or not isinstance(segments, list)
                or not 1 <= len(segments) <= 32
                or any(not isinstance(seg, str) or not 1 <= len(seg) <= 128
                       for seg in segments)
                or item.get("position") not in {"inside_cut", "cut_boundary"}
                or item.get("auto_repair_allowed") is not False
                or item.get("human_editorial_qa_completed") is not False):
            raise ValueError("LOCAL_EDIT_TRIAGE_CANDIDATE_INVALID")
        checked.append(item)
    return checked


def _context_index(context: dict | None, *, review_sha: str,
                   review: dict, candidates: list[dict]) -> dict[int, tuple]:
    if context is None:
        return {}
    comparisons = context.get("comparisons")
    if (context.get("version") != SOURCE_CONTEXT_VERSION
            or context.get("review_sha256") != review_sha
            or context.get("base_sha256_from_review") != review["base_sha256"]
            or context.get("auto_repair_allowed") is not False
            or context.get("ready") is not False
            or context.get("production_ready") is not False
            or context.get("human_editorial_qa_completed") is not False
            or context.get("publication_performed") is not False
            or context.get("source_bytes_independently_rehashed") is not False
            or not isinstance(comparisons, list)
            or len(comparisons) > 3
            or type(context.get("sampled_event_count")) is not int
            or len(comparisons) != context["sampled_event_count"]):
        raise ValueError("LOCAL_EDIT_TRIAGE_SOURCE_CONTEXT_INVALID")
    result = {}
    for row in comparisons:
        if not isinstance(row, dict):
            raise ValueError("LOCAL_EDIT_TRIAGE_CONTEXT_ROW_INVALID")
        i = row.get("observation_index")
        if type(i) is not int or not 0 <= i < len(candidates) or i in result:
            raise ValueError("LOCAL_EDIT_TRIAGE_CONTEXT_INDEX_INVALID")
        candidate = candidates[i]
        ts = row.get("start_seconds")
        if (candidate["kind"] not in {"blackout", "freeze"}
                or row.get("kind") != candidate["kind"]
                or type(ts) not in (int, float)
                or not math.isfinite(ts)
                or abs(ts - candidate["start_seconds"]) > 0.002
                or row.get("segment_id") not in candidate["segment_ids"]
                or row.get("auto_repair_allowed") is not False
                or row.get("human_editorial_qa_completed") is not False
                or row.get("status") not in _CONTEXT_CODES):
            raise ValueError("LOCAL_EDIT_TRIAGE_CONTEXT_ROW_INVALID")
        result[i] = _CONTEXT_CODES[row["status"]]
    return result


def build_review_triage(
    *, review: dict, review_sha: str, context: dict | None,
    context_sha: str,
) -> dict:
    """Produce only fixed advisory codes. No machine-actionable patch plan."""
    candidates = _validated_review(review)
    if not _SHA.fullmatch(review_sha):
        raise ValueError("LOCAL_EDIT_TRIAGE_REVIEW_HASH_INVALID")
    if context is None and context_sha or context is not None and not _SHA.fullmatch(context_sha):
        raise ValueError("LOCAL_EDIT_TRIAGE_CONTEXT_HASH_INVALID")
    origin = _context_index(
        context, review_sha=review_sha, review=review, candidates=candidates,
    )
    rows = []
    for i, candidate in enumerate(candidates):
        kind = candidate["kind"]
        if i in origin:
            classification, followup, level = origin[i]
        else:
            classification = "SOURCE_CONTEXT_NOT_ESTABLISHED"
            followup = "INSPECT_REVIEW_EVENT_WITHOUT_ASSUME_CAUSE"
            level = 1
        # A source signal's presence lowers inspection urgency, NEVER removes
        # the warning or proves this was an intended/good scene.
        rows.append({
            "review_index": i,
            "kind": kind,
            "start_seconds": candidate["start_seconds"],
            "end_seconds": candidate["end_seconds"],
            "segment_ids": candidate["segment_ids"],
            "inspection_bucket": _BUCKETS[level],
            "source_context": classification,
            "inspection_action": _CODES[kind],
            "followup_action": followup,
            "repair_draft": {
                "kind": "NON_EXECUTABLE_INSPECTION_PROPOSAL",
                "require_before_any_media_edit": [
                    "INDEPENDENT_VISUAL_OR_AUDIO_VALIDATION",
                    "RENDER_PREVIEW_AND_COMPARE_TO_UNMODIFIED_BASELINE",
                    "VERIFY_POLICY_AND_EDITORIAL_CONTINUITY",
                ],
                "operation_authorized": False,
                "requires_rollback_if_ever_implemented": True,
            },
            "human_editorial_qa_completed": False,
            "auto_repair_allowed": False,
        })
    order = {bucket: index for index, bucket in enumerate(_BUCKETS)}
    rows.sort(key=lambda x: (order[x["inspection_bucket"]], x["start_seconds"],
                              x["review_index"]))
    counts = {bucket: sum(row["inspection_bucket"] == bucket for row in rows)
              for bucket in _BUCKETS}
    return {
        "version": TRIAGE_VERSION,
        "review_sha256": review_sha,
        "source_context_sha256": context_sha or None,
        "base_sha256_from_review": review["base_sha256"],
        "final_sha256_from_review": review["final_sha256"],
        "media_sha256_independently_reverified": False,
        "candidate_count": len(rows),
        "context_mapped_count": len(origin),
        "inspection_counts": counts,
        "candidate_queue": rows,
        "editorial_quality_metrics_available": False,
        "repair_preview_generated": False,
        "repair_execution_performed": False,
        "publication_performed": False,
        "human_editorial_qa_completed": False,
        "auto_repair_allowed": False,
        "ready": False,
        "production_ready": False,
    }


def store_local_review_triage(
    *, mp4_output: Path, review_sha256: str,
    source_context_sha256: str = "",
) -> dict[str, object]:
    """Create-only SHA-pinned triage receipt or explicit unavailable status."""
    result: dict[str, object] = {
        "status": "LOCAL_EDIT_TRIAGE_UNAVAILABLE",
        "sha256": "",
        "candidate_count": 0,
        "review_first_count": 0,
        "ready": False, "production_ready": False,
    }
    try:
        media = _no_links(mp4_output)
        if not media.is_file() or media.suffix.lower() != ".mp4":
            raise ValueError("LOCAL_EDIT_TRIAGE_MEDIA_UNAVAILABLE")
        review_path = _no_links(media.with_name(f"{media.stem}.review.json"))
        context_path = _no_links(media.with_name(f"{media.stem}.source-context.json"))
        target = _no_links(media.with_name(f"{media.stem}.triage.json"))
        if target.exists() or (source_context_sha256 and
                               not _SHA.fullmatch(source_context_sha256)):
            raise ValueError("LOCAL_EDIT_TRIAGE_OUTPUT_UNSAFE")
        review = _load_hash_verified(review_path, review_sha256)
        context = (_load_hash_verified(context_path, source_context_sha256)
                   if source_context_sha256 else None)
        triage = build_review_triage(
            review=review, review_sha=review_sha256,
            context=context, context_sha=source_context_sha256,
        )
        encoded = (json.dumps(
            triage, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(encoded) > _MAX_OUTPUT:
            raise ValueError("LOCAL_EDIT_TRIAGE_OUTPUT_LIMIT")
        with tempfile.TemporaryDirectory(
            prefix=".holoclip-edit-triage-", dir=media.parent,
        ) as tmp:
            stage = Path(tmp) / "triage.json"
            stage.write_bytes(encoded)
            # Do not commit advice based on changing event receipts.
            if hashlib.sha256(review_path.read_bytes()).hexdigest() != review_sha256:
                raise ValueError("LOCAL_EDIT_TRIAGE_REVIEW_CHANGED")
            if source_context_sha256 and (
                hashlib.sha256(context_path.read_bytes()).hexdigest()
                != source_context_sha256
            ):
                raise ValueError("LOCAL_EDIT_TRIAGE_CONTEXT_CHANGED")
            try:
                os.link(stage, target)
            except OSError as exc:
                raise ValueError("LOCAL_EDIT_TRIAGE_COMMIT_UNAVAILABLE") from exc
        result.update({
            "status": "LOCAL_EDIT_TRIAGE_CREATED_UNVERIFIED_EDITORIAL",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "candidate_count": triage["candidate_count"],
            "review_first_count": triage["inspection_counts"]["REVIEW_FIRST"],
        })
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        pass
    return result
