"""Offline triage of persisted reserved source↔finished media holdout trials.

No network, FFmpeg or model inference; validates audit provenance and emits
only fixed classification codes and public clip IDs. Never awards human gold.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.holdout_e2e_trial import MULTI_BATCH_VERSION, STAGES, VERSION
from holoclip.holdout_preparation import _candidates
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import atomic_json

DIAGNOSIS_VERSION = "holdout-real-media-offline-diagnosis-v1"
STAGE_NAMES = {s.name for s in STAGES}
_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_FALSE_FLAGS = (
    "production_ready", "ready", "qualification_allowed", "benchmark_held_out",
    "event_split_verified", "reviewed_gold_available",
    "frame_accurate_cuts_verified", "dense_output_source_map_verified",
)


def _fixed_status(value: object) -> str:
    # The legacy stage-summary report uses this one lower-case literal.
    # Normalize only the known constant; never reflect arbitrary provider text.
    if value == "missing_report":
        return "MISSING_REPORT"
    if not isinstance(value, str) or not _SAFE_STATUS.fullmatch(value):
        raise ValueError("Invalid status code in evidence report")
    return value


def _status_tallies(payload: object, count: int) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid per-stage status counts")
    result = {}
    for stage, statuses in payload.items():
        if stage not in STAGE_NAMES or not isinstance(statuses, dict):
            raise ValueError("Unknown stage or invalid stage counts")
        rows = {}
        for status, quantity in statuses.items():
            name = _fixed_status(status)
            if type(quantity) is not int or not 0 <= quantity <= count:
                raise ValueError("Invalid recorded status quantity")
            rows[name] = quantity
        if sum(rows.values()) != count:
            raise ValueError("Stage status tally disagrees with selected clip count")
        result[stage] = rows
    return result


def _records_from_audit(audit: dict, reserved: set[str]) -> tuple[list, str]:
    version = audit.get("version")
    if version == MULTI_BATCH_VERSION:
        rows = audit.get("batches", [])
        if not isinstance(rows, list) or len(rows) > 12:
            raise ValueError("Invalid bounded batch records")
    elif version == VERSION:
        statuses = audit.get("observations", {}).get("stage_status_counts", {})
        rows = [{
            "number": 1,
            "status": audit["status"],
            "selected_clip_ids": audit.get("selected_clip_ids", []),
            "completed_stage_count": len(audit.get("stages", [])),
            "blocked_stage": audit.get("blocked_stage"),
            "stage_status_counts": statuses,
        }] if audit.get("selected_clip_ids") else []
    else:
        raise ValueError("Unsupported holdout audit schema")

    seen: set[str] = set()
    deferred_audio: list[str] = []
    cleaned = []
    for expected_number, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("number") != expected_number:
            raise ValueError("Audit batch sequence is invalid")
        selected = row.get("selected_clip_ids")
        if (not isinstance(selected, list) or not selected
                or not all(isinstance(k, str) and k in reserved for k in selected)
                or len(set(selected)) != len(selected) or set(selected) & seen):
            raise ValueError("Unknown or duplicated reserved clip identity in audit")
        seen.update(selected)
        current = _fixed_status(row.get("status"))
        stage_counts = _status_tallies(row.get("stage_status_counts", {}), len(selected))
        blocked = row.get("blocked_stage")
        if blocked is not None and blocked not in (STAGE_NAMES | {"postflight"}):
            raise ValueError("Invalid blocked stage")
        stage_number = row.get("completed_stage_count")
        if type(stage_number) is not int or not 0 <= stage_number <= len(STAGES):
            raise ValueError("Invalid stage completion count")
        if current in ("UNREVIEWED_MEASUREMENTS_COMPLETE",
                       "SIMULATED_STAGE_TEST_COMPLETE"):
            if (stage_number != len(STAGES)
                    or len(stage_counts) != len(STAGES)
                    or any(
                        stage_counts.get(stage.name, {}).get(stage.success, 0)
                        != len(selected)
                        for stage in STAGES
                    )):
                raise ValueError("A complete run lacks all expected stage successes")
        defer = row.get("deferred_insufficient_audio", False)
        if type(defer) is not bool:
            raise ValueError("Invalid audio deferral marker")
        if defer:
            if (version != MULTI_BATCH_VERSION or len(selected) != 1
                    or current != "BLOCKED_UPSTREAM_STAGE"
                    or blocked != "original_audio"
                    or stage_counts.get("original_audio") not in (
                        {"INSUFFICIENT_AUDIO": 1},
                        {"INSUFFICIENT_UNIQUE_ANCHORS": 1},
                    )):
                raise ValueError("Audio deferral bypassed a safety condition")
            deferred_audio.append(selected[0])
        cleaned.append({
            "number": expected_number, "status": current,
            "selected_clip_ids": selected, "stage_status_counts": stage_counts,
            "blocked_stage": blocked, "completed_stage_count": stage_number,
            "deferred_insufficient_audio": defer,
        })
    if version == MULTI_BATCH_VERSION:
        if (audit.get("deferred_audio_clip_ids", []) != sorted(deferred_audio)
                or audit.get("deferred_audio_clips", 0) != len(deferred_audio)):
            raise ValueError("Cumulative deferral list is not stage-grounded")
    return cleaned, version


def _classify_blocker(stage: str | None, statuses: dict) -> str:
    codes = set(statuses)
    if any(k in codes for k in ("BLOCKED_SPLIT_CONFLICT", "BLOCKED_TRAIN_HOLDOUT_SPLIT")):
        return "REVIEW_TRAIN_HOLDOUT_SPLIT"
    if ("QUARANTINED_POLICY" in codes or "QUARANTINED_RANGE_POLICY" in codes
            or any("POLICY" in k for k in codes)):
        return "REVIEW_RIGHTS_AND_CLIP_POLICY"
    if (stage == "original_audio" or any(
        x in codes for x in ("INSUFFICIENT_AUDIO", "INSUFFICIENT_UNIQUE_ANCHORS")
    )):
        return "CHECK_ORIGINAL_AUDIO_CORRESPONDENCE"
    if any(x in codes for x in (
        "QUARANTINED_SOURCE_CHANGED", "QUARANTINED_SOURCE_AMBIGUOUS",
        "QUARANTINED_ACQUIRED_SOURCE_IDENTITY", "QUARANTINED_ACQUIRED_IDENTITY",
    )):
        return "REVERIFY_SOURCE_IDENTITY"
    if "MISSING_REPORT" in codes or any(
        "MEASUREMENT" in x or "UNVERIFIED" in x for x in codes
    ):
        return "VERIFY_PINNED_MEDIA_AND_REPORT_CHECKSUMS"
    if "RETRYABLE_FAILURE" in codes or "STAGE_FAILED" in codes:
        return "INSPECT_LOCAL_MEDIA_FAILURE_AND_RETRY_ONCE"
    return "INSPECT_FAILED_STAGE_WITHOUT_GOLD_PROMOTION"


def diagnose_holdout_e2e(
    *, store: Path, state_path: Path, root: Path,
    output: Path | None = None,
) -> dict:
    """Summarize stored *run* audits without reading/downloading original media.

    Rechecks current holdout-vs-training collision metadata and SHA of original
    reservation file. Never counts cached reports as a new empirical run.
    """
    path = output or root / "holdout-e2e-diagnosis.json"
    result = {
        "version": DIAGNOSIS_VERSION,
        "status": "BLOCKED_MISSING_RESERVATION_STATE",
        "source": "OFFLINE_SAVED_AUDIT_ONLY",
        "report_path": str(path),
        "network_or_media_examined": False,
        "real_media_run_completed": False,
        "ready": False, "production_ready": False,
        "qualification_allowed": False, "benchmark_held_out": False,
        "event_split_verified": False, "reviewed_gold_available": False,
        "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "editor_cut_precision": None, "editor_cut_recall": None,
        "native_timestamp_error_seconds": None,
        "event_holdout_accuracy": None,
        "reserved_clips": 0, "attempted_clips": 0,
        "unattempted_clip_ids": [], "batches": [],
        "deferred_audio_clip_ids": [], "deferred_audio_clips": 0,
        "stage_status_totals": {}, "first_blocker": None,
        "next_action": "RESTORE_DURABLE_RESERVATION_STATE",
    }

    def save():
        atomic_json(path, result)

    if not state_path.is_file():
        save()
        return result
    state_bytes = state_path.read_bytes()
    state = CorpusGrowthState.model_validate_json(state_bytes)
    candidates = _candidates(state)
    reserved = {human_clip_identity(x.clip_url) for x in candidates}
    result["reserved_clips"] = len(candidates)
    if len(reserved) != len(candidates):
        result["status"] = "BLOCKED_DUPLICATE_RESERVATION"
        result["next_action"] = "REPAIR_DUPLICATE_RESERVED_CLIP_IDENTITIES"
        save()
        return result
    result["unattempted_clip_ids"] = sorted(reserved)
    inventory = holdout_inventory(store, state_path)
    result["conflicted_original_stream_count"] = len(inventory["conflicted_streams"])
    if inventory["conflicted_streams"]:
        result["status"] = "BLOCKED_TRAIN_HOLDOUT_SPLIT"
        result["next_action"] = "REVIEW_TRAIN_HOLDOUT_SPLIT"
        save()
        return result
    if not reserved:
        result["status"] = "BLOCKED_NO_RESERVED_CLIPS"
        result["next_action"] = "DISCOVER_AND_RESERVE_NEVER_LEARNED_SOURCE_STREAMS"
        save()
        return result

    # Prefer cumulative audit if present; never mix its results with the
    # latest individual batch audit and count clips twice.
    audit_path = root / "holdout-e2e-all-batches.json"
    if not audit_path.is_file():
        audit_path = root / "holdout-e2e-audit.json"
    if not audit_path.is_file():
        result["status"] = "NO_REAL_MEDIA_TRIAL_RECORDED"
        result["next_action"] = "RUN_BOUNDED_HOLDOUT_MEDIA_TRIAL"
        save()
        return result
    result["audit_kind"] = (
        "MULTI_BATCH" if audit_path.name.endswith("all-batches.json")
        else "SINGLE_BATCH"
    )
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if not isinstance(audit, dict):
            raise ValueError("Invalid audit")
        if any(audit.get(key) is not False for key in _FALSE_FLAGS):
            raise ValueError("Illegal promotional evidence flag")
        recorded_state_hash = audit.get("reservation_state_sha256")
        # Legacy audits are legible, but cannot verify that their reservations
        # still match the present source dataset: do not trust the coverage.
        if (not isinstance(recorded_state_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", recorded_state_hash)):
            result["status"] = "BLOCKED_AUDIT_MISSING_RESERVATION_DIGEST"
            result["next_action"] = "REEXECUTE_TRIAL_TO_BIND_RESERVATIONS"
            save()
            return result
        if recorded_state_hash != hashlib.sha256(state_bytes).hexdigest():
            result["status"] = "BLOCKED_STALE_RESERVATION_STATE"
            result["next_action"] = "RERUN_TRIAL_AFTER_RESERVATION_CHANGE"
            save()
            return result
        rows, _ = _records_from_audit(audit, reserved)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        result["status"] = "BLOCKED_INVALID_AUDIT"
        result["next_action"] = "INSPECT_AND_REBUILD_INVALID_AUDIT"
        save()
        return result

    seen: set[str] = set()
    counts = {s.name: Counter() for s in STAGES}
    first_blocker = None
    for row in rows:
        selected = row["selected_clip_ids"]
        seen.update(selected)
        for stage, statuses in row["stage_status_counts"].items():
            counts[stage].update(statuses)
        failed = row["status"] not in (
            "UNREVIEWED_MEASUREMENTS_COMPLETE", "SIMULATED_STAGE_TEST_COMPLETE",
        )
        issue_stage = row["blocked_stage"]
        relevant = row["stage_status_counts"].get(issue_stage, {}) if issue_stage else {}
        if failed and first_blocker is None:
            first_blocker = {
                "batch": row["number"], "stage": issue_stage,
                "run_status": row["status"],
                "observed_status_counts": relevant,
                "diagnostic_action": _classify_blocker(issue_stage, relevant),
            }
        result["batches"].append({
            "number": row["number"], "status": row["status"],
            "selected_clip_ids": selected,
            "completed_stage_count": row["completed_stage_count"],
            "blocked_stage": issue_stage,
        })
    result["attempted_clips"] = len(seen)
    result["unattempted_clip_ids"] = sorted(reserved - seen)
    result["deferred_audio_clip_ids"] = sorted(
        row["selected_clip_ids"][0] for row in rows
        if row["deferred_insufficient_audio"]
    )
    result["deferred_audio_clips"] = len(result["deferred_audio_clip_ids"])
    result["stage_status_totals"] = {
        key: dict(values) for key, values in counts.items() if values
    }
    result["first_blocker"] = first_blocker
    result["audit_status"] = _fixed_status(audit.get("status"))
    simulated = (
        audit.get("real_media_run_completed") is not True
        or any(row["status"] == "SIMULATED_STAGE_TEST_COMPLETE" for row in rows)
        or result["audit_status"] in (
            "SIMULATED_ALL_BATCHES_COMPLETE",
            "SIMULATED_INSUFFICIENT_AUDIO_RECORDED",
        )
    )
    result["simulated_only"] = simulated
    if (result["audit_status"] == "BOUNDED_BATCH_LIMIT_REACHED"
            and result["deferred_audio_clips"]):
        result["status"] = "TRIAL_INCOMPLETE_BATCH_BUDGET"
        result["next_action"] = "INCREASE_FINITE_BATCH_BUDGET_AND_RERUN"
    elif (result["deferred_audio_clips"]
          and result["audit_status"] in (
              "UNREVIEWED_INSUFFICIENT_AUDIO_RECORDED",
              "SIMULATED_INSUFFICIENT_AUDIO_RECORDED",
          )):
        result["status"] = "TRIAL_PARTIAL_WITH_AUDIO_GAPS"
        result["next_action"] = "CHECK_ORIGINAL_AUDIO_CORRESPONDENCE"
    elif first_blocker:
        result["status"] = "TRIAL_BLOCKED_REVIEW_STAGE"
        result["next_action"] = first_blocker["diagnostic_action"]
    elif result["audit_status"] == "BOUNDED_BATCH_LIMIT_REACHED":
        result["status"] = "TRIAL_INCOMPLETE_BATCH_BUDGET"
        result["next_action"] = "INCREASE_FINITE_BATCH_BUDGET_AND_RERUN"
    elif simulated:
        result["status"] = "SIMULATED_TRIAL_ONLY"
        result["next_action"] = "RUN_WITH_REAL_MEDIA_PROVIDER"
    elif (result["audit_status"] in (
        "UNREVIEWED_ALL_BATCHES_COMPLETE",
        "UNREVIEWED_MEASUREMENTS_COMPLETE",
    ) and len(seen) == len(reserved)):
        result["status"] = "UNREVIEWED_TRIAL_RECORDED"
        result["next_action"] = "REVIEW_REAL_MEDIA_AND_INDEPENDENT_EDITOR_GOLD"
        # Reported from existing audit only, never direct reprocessing.
        result["real_media_run_completed"] = True
    elif result["audit_status"] in (
        "UNREVIEWED_ALL_BATCHES_COMPLETE",
        "UNREVIEWED_MEASUREMENTS_COMPLETE",
    ):
        result["status"] = "TRIAL_INCOMPLETE_RESERVED_CLIPS"
        result["next_action"] = "RUN_REMAINING_RESERVED_CLIPS"
    else:
        result["status"] = "TRIAL_BLOCKED_REVIEW_STAGE"
        result["next_action"] = _classify_blocker(None, {result["audit_status"]: 1})
    save()
    return result
