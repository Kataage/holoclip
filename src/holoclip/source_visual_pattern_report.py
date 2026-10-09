"""Durable, source-diversity-aware reports for constructed public-video patterns.

These are NOT real editor negative labels or source-match precision/recall.
Unknown codec/decode statuses have no evaluated denominator. Observations
of the same bounded source media hash are counted once across different clips.
"""
from __future__ import annotations

import re
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.source_visual_pattern_controls import (
    VERSION as PATTERN_VERSION, PATTERNS, EVALUABLE_NONMATCH, APPROVED,
)

VERSION = "source-video-pattern-diversity-observer-v1"
MAX_HISTORY = 24
_SHA = re.compile(r"[0-9a-f]{64}\Z")
FLAGS = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "used_in_learning_or_holdout": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
}


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def checked_pattern_history(value, *, clip_id: str, candidate_ids: list[str]):
    """Strictly fail-closed on spoofed, incomplete or promoted saved controls."""
    if value is None:
        return []
    if (not isinstance(clip_id, str) or not clip_id
            or not isinstance(candidate_ids, list) or not candidate_ids
            or any(not isinstance(x, str) or not x for x in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or not isinstance(value, list) or len(value) > MAX_HISTORY):
        raise ValueError("Invalid pattern history owner or budget")
    seen = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {
            "version", "clip_id", "source_stream_id",
            "bounded_source_media_sha256", "source_kind", "result",
        } or row.get("version") != VERSION
                or row.get("clip_id") != clip_id
                or row.get("source_stream_id") not in candidate_ids
                or row.get("source_kind")
                    != "YT_DLP_PUBLIC_BOUNDED_SOURCE_VIDEO_UNREVIEWED"
                or not _sha(row.get("bounded_source_media_sha256"))):
            raise ValueError("Forged pattern sample provenance")
        result = row.get("result")
        if (not isinstance(result, dict)
                or set(result) != {
                    "version", "status", "reference_type",
                    "real_human_editor_labels", "official_hololive_source_verified",
                    "source_video_sha256", "measured_frame_sha256s",
                    "measured_frame_count", "patterns", *FLAGS,
                }
                or result.get("version") != PATTERN_VERSION
                or result.get("status")
                    != "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE"
                or result.get("reference_type")
                    != "CONSTRUCTED_FROM_ACTUAL_MEASURED_SOURCE_FRAMES"
                or result.get("real_human_editor_labels") is not False
                or result.get("official_hololive_source_verified") is not False
                or result.get("source_video_sha256")
                    != row["bounded_source_media_sha256"]
                or type(result.get("measured_frame_count")) is not int
                or not 3 <= result["measured_frame_count"] <= 4
                or not isinstance(result.get("measured_frame_sha256s"), list)
                or len(result["measured_frame_sha256s"])
                    != result["measured_frame_count"]
                or len(set(result["measured_frame_sha256s"])) < 3
                or any(not _sha(x) for x in result["measured_frame_sha256s"])
                or any(result.get(k) is not v for k, v in FLAGS.items())):
            raise ValueError("Invalid or promoted pattern measurement")
        cases = result.get("patterns")
        if not isinstance(cases, dict) or set(cases) != set(PATTERNS):
            raise ValueError("Missing or invented video control patterns")
        for name, expected in PATTERNS.items():
            case = cases[name]
            if (not isinstance(case, dict)
                    or set(case) not in (
                        {"expected", "observed_status", "behaved_as_constructed"},
                        {"expected", "observed_status", "behaved_as_constructed",
                         "error_class"},
                    )
                    or case.get("expected") != expected
                    or not isinstance(case.get("observed_status"), str)
                    or not 1 <= len(case["observed_status"]) <= 128
                    or type(case.get("behaved_as_constructed"))
                        not in (bool, type(None))):
                raise ValueError("Unsupported measured pattern outcome")
            actual = case["observed_status"]
            if actual == APPROVED:
                anticipated = expected == "constructed_positive"
            elif actual in EVALUABLE_NONMATCH:
                anticipated = expected == "constructed_temporal_decoy"
            else:
                anticipated = None
            if case["behaved_as_constructed"] is not anticipated:
                raise ValueError("Forged or contradictory pattern metric")
            if (("error_class" in case)
                    != (actual == "UNKNOWN_CONTROL_EXECUTION_FAILURE")):
                raise ValueError("Bad pattern exception provenance")
            if "error_class" in case and (
                not isinstance(case["error_class"], str)
                or not 1 <= len(case["error_class"]) <= 120
            ):
                raise ValueError("Malformed failure class")
        key = (row["source_stream_id"], row["bounded_source_media_sha256"])
        if key in seen:
            raise ValueError("Duplicate original source-media pattern sample")
        seen.add(key)
    return value


def add_pattern_result(
    history, *, clip_id: str, candidate_ids: list[str],
    source_id: str, result: dict,
):
    checked = checked_pattern_history(
        history, clip_id=clip_id, candidate_ids=candidate_ids,
    )
    if (not isinstance(result, dict) or result.get("status")
            != "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE"):
        return checked, False
    row = {
        "version": VERSION, "clip_id": clip_id,
        "source_stream_id": source_id,
        "bounded_source_media_sha256": result.get("source_video_sha256"),
        "source_kind": "YT_DLP_PUBLIC_BOUNDED_SOURCE_VIDEO_UNREVIEWED",
        "result": result,
    }
    checked_pattern_history([row], clip_id=clip_id, candidate_ids=candidate_ids)
    key = (row["source_stream_id"], row["bounded_source_media_sha256"])
    if any((x["source_stream_id"], x["bounded_source_media_sha256"]) == key
           for x in checked):
        return checked, False
    records = [*checked, row][-MAX_HISTORY:]
    checked_pattern_history(records, clip_id=clip_id, candidate_ids=candidate_ids)
    return records, True


def summarize_pattern_results(rows: list[dict]) -> dict:
    """Strictly report constructed pattern outcomes, never real-world accuracy."""
    cases = {}
    for name, expectation in PATTERNS.items():
        collected = [r["result"]["patterns"][name] for r in rows]
        evaluated = [
            x["behaved_as_constructed"]
            for x in collected if x["behaved_as_constructed"] is not None
        ]
        cases[name] = {
            "expected": expectation,
            "observed_source_video_samples": len(collected),
            "evaluable_cases": len(evaluated),
            "behaved_as_constructed_count": sum(evaluated),
            "unexpected_behavior_count": len(evaluated) - sum(evaluated),
            "unevaluable_cases": len(collected) - len(evaluated),
            "constructed_case_success_fraction": (
                sum(evaluated) / len(evaluated) if evaluated else None
            ),
        }
    sources = {r["source_stream_id"] for r in rows}
    return {
        "version": VERSION,
        "status": (
            "UNKNOWN_NO_REAL_VIDEO_PATTERN_SAMPLES" if not rows
            else "MEASURED_CONSTRUCTED_PATTERNS_NOT_EDITOR_ACCURACY"
        ),
        "real_measured_source_video_samples": len(rows),
        "distinct_candidate_source_streams_measured": len(sources),
        "source_diversity_sufficient_for_generalization": False,
        "pattern_results": cases,
        "has_independent_real_source_match_labels": False,
        "has_independent_human_editor_labels": False,
        "precision_recall_estimable": False,
        "real_original_match_false_positive_rate": None,
        "editor_cut_precision": None,
        "editor_cut_recall": None,
        "qualification_allowed": False,
        "reviewed_gold_available": False,
        "source_identity_verified": False,
        "ready": False,
        "production_ready": False,
    }


def report_saved_patterns(state_path: Path):
    if not state_path.is_file():
        return {
            **summarize_pattern_results([]),
            "status": "UNKNOWN_NO_CORPUS_STATE",
            "source_state_checked": False,
        }
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    rows = []
    for item in state.items.values():
        if item.status != "source_visual_pending":
            continue
        scout = item.source_scout
        if not isinstance(scout, dict):
            raise ValueError("Invalid unverified video candidate state")
        sources = [
            x["source_stream_id"]
            for x in scout.get("candidate_sources", [])
        ]
        verified = checked_pattern_history(
            scout.get("real_video_pattern_history"),
            clip_id=human_clip_identity(item.clip_url),
            candidate_ids=sources,
        )
        rows.extend(verified)
    # The same exact source video section cannot count more than once even if
    # it was searched from several separately discovered human clip URLs.
    unique = {}
    for row in rows:
        key = (row["source_stream_id"], row["bounded_source_media_sha256"])
        if key in unique and unique[key]["result"] != row["result"]:
            raise ValueError("Contradictory repeated source-video control result")
        unique[key] = row
    return {
        **summarize_pattern_results(list(unique.values())),
        "source_state_checked": True,
        "source_visual_pending_clips": sum(
            item.status == "source_visual_pending"
            for item in state.items.values()
        ),
        "deduplicated_cross_clip_repeat_samples": len(rows)-len(unique),
    }
