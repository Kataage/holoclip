"""Strict positive/temporal-decoy *constructed control* measurements.

These only test the health of the automatic video matcher on transforms
constructed from SHA-verified already acquired video. They cannot estimate
real editor cut precision, recall, true negative rate or original ID truth.
"""
from __future__ import annotations

import re
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.source_visual_real_controls import VERSION as CONTROL_VERSION
from holoclip.source_visual_pattern_report import report_saved_patterns

VERSION = "real-acquisition-constructed-control-observer-v1"
MAX_HISTORY = 24
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NO_GOLD = {
    "ready": False,
    "production_ready": False,
    "qualification_allowed": False,
    "reviewed_gold_available": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_source_match_false_positive_rate": None,
    "source_identity_verified": False,
    "used_in_learning_or_holdout": False,
}


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def checked_control_history(value, *, clip_id: str, candidate_ids: list[str]):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_HISTORY:
        raise ValueError("Invalid bounded source-video control history")
    if (not isinstance(clip_id, str) or not clip_id
            or not isinstance(candidate_ids, list) or not candidate_ids
            or len(set(candidate_ids)) != len(candidate_ids)):
        raise ValueError("Invalid source-video control owner")
    known = set(candidate_ids)
    identities = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {
            "version", "clip_id", "source_stream_id", "origin",
            "measured_source_video_sha256", "control",
        } or row.get("version") != VERSION
                or row.get("clip_id") != clip_id
                or row.get("source_stream_id") not in known
                or row.get("origin") != "YT_DLP_BOUNDED_PUBLIC_VIDEO_UNREVIEWED"
                or not _sha(row.get("measured_source_video_sha256"))):
            raise ValueError("Unverified source-video control provenance")
        control = row.get("control")
        if (not isinstance(control, dict)
                or control.get("version") != CONTROL_VERSION
                or control.get("source_window_sha256")
                    != row["measured_source_video_sha256"]
                or control.get("reference_type")
                    != "CONSTRUCTED_FROM_MEASURED_SOURCE_FRAMES"
                or control.get("controls_are_real_human_editor_labels") is not False
                or control.get("source_video_is_official_hololive_verified") is not False
                or control.get("reviewed_gold_available") is not False
                or control.get("qualification_allowed") is not False
                or control.get("used_in_learning_or_holdout") is not False
                or control.get("source_identity_verified") is not False
                or control.get("editor_cut_precision") is not None
                or control.get("editor_cut_recall") is not None
                or control.get("real_source_match_false_positive_rate") is not None
                or control.get("source_native_absolute_pts_verified") is not False
                or control.get("status")
                    != "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED"
                or type(control.get("observed_control_pairs")) is not int
                or not 3 <= control["observed_control_pairs"] <= 4
                or type(control.get("constructed_positive_detected")) is not bool
                or type(control.get("constructed_reversal_rejected")) is not bool
                or not isinstance(control.get("observed_frame_sha256s"), list)
                or len(control["observed_frame_sha256s"])
                    != control["observed_control_pairs"]
                or any(not _sha(v) for v in control["observed_frame_sha256s"])):
            raise ValueError("Forged or promoted constructed source-video result")
        ident = (row["source_stream_id"], row["measured_source_video_sha256"])
        if ident in identities:
            raise ValueError("Duplicate source video sample in constructed controls")
        identities.add(ident)
    return value


def add_control_result(history, *, clip_id, candidate_ids, source_id, control):
    checked = checked_control_history(
        history, clip_id=clip_id, candidate_ids=candidate_ids,
    )
    if not isinstance(control, dict) or control.get("status") != (
        "MEASURED_CONSTRUCTED_VIDEO_CONTROLS_UNREVIEWED"
    ):
        return checked, False
    row = {
        "version": VERSION,
        "clip_id": clip_id,
        "source_stream_id": source_id,
        "origin": "YT_DLP_BOUNDED_PUBLIC_VIDEO_UNREVIEWED",
        "measured_source_video_sha256": control.get("source_window_sha256"),
        "control": control,
    }
    checked_control_history(
        [row], clip_id=clip_id, candidate_ids=candidate_ids,
    )
    identity = (row["source_stream_id"], row["measured_source_video_sha256"])
    if any((x["source_stream_id"], x["measured_source_video_sha256"]) == identity
           for x in checked):
        return checked, False
    updated = [*checked, row][-MAX_HISTORY:]
    checked_control_history(updated, clip_id=clip_id, candidate_ids=candidate_ids)
    return updated, True


def summarize_control_history(histories: list[dict]) -> dict:
    counts = {
        "real_measured_video_control_samples": len(histories),
        "constructed_jpeg_positive_detected": sum(
            r["control"]["constructed_positive_detected"] for r in histories
        ),
        "constructed_reversed_sequence_rejected": sum(
            r["control"]["constructed_reversal_rejected"] for r in histories
        ),
    }
    denominator = counts["real_measured_video_control_samples"]
    return {
        "version": VERSION,
        "status": (
            "MEASURED_CONSTRUCTED_CONTROLS_NOT_EDITOR_ACCURACY"
            if denominator else "UNKNOWN_NO_REAL_VIDEO_CONTROL_SAMPLES"
        ),
        **counts,
        "constructed_positive_detection_fraction": (
            counts["constructed_jpeg_positive_detected"] / denominator
            if denominator else None
        ),
        "constructed_reversed_sequence_rejection_fraction": (
            counts["constructed_reversed_sequence_rejected"] / denominator
            if denominator else None
        ),
        "source_provenance_kind": "YT_DLP_BOUNDED_PUBLIC_VIDEO_UNREVIEWED",
        "has_independent_human_source_labels": False,
        "has_independent_human_negative_samples": False,
        "precision_recall_estimable": False,
        **_NO_GOLD,
    }


def report_saved_source_controls(state_path: Path) -> dict:
    if not state_path.is_file():
        return {**summarize_control_history([]),
                "status": "UNKNOWN_NO_CORPUS_STATE",
                "saved_source_state_checked": False,
                "constructed_pattern_audit": report_saved_patterns(state_path)}
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    histories = []
    for item in state.items.values():
        if item.status != "source_visual_pending":
            continue
        scout = item.source_scout
        if not isinstance(scout, dict):
            raise ValueError("Malformed source-visual pending state")
        candidate_ids = [
            row["source_stream_id"]
            for row in scout.get("candidate_sources", [])
        ]
        records = checked_control_history(
            scout.get("real_video_control_history"),
            clip_id=human_clip_identity(item.clip_url),
            candidate_ids=candidate_ids,
        )
        histories.extend(records)
    return {**summarize_control_history(histories),
            "saved_source_state_checked": True,
            "constructed_pattern_audit": report_saved_patterns(state_path),
            "pending_clip_count": sum(
                item.status == "source_visual_pending"
                for item in state.items.values()
            )}
