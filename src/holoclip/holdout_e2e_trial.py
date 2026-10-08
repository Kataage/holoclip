"""Run all local/public reserved-video evidence stages and report *observed* coverage.

No synthesized gold, no paid model, no precision/recall without an independent
human reference. Failure at any stage aborts instead of silently skipping.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.holdout_audio_pipeline import align_reserved_holdouts
from holoclip.holdout_cut_pipeline import refine_reserved_cut_candidates
from holoclip.holdout_cut_side_pipeline import inspect_reserved_cut_sides
from holoclip.holdout_frame_links_pipeline import correlate_reserved_source_frames
from holoclip.holdout_partial_time_map_pipeline import build_reserved_partial_maps
from holoclip.holdout_preparation import _candidates, prepare_reserved_holdouts
from holoclip.holdout_reservations import holdout_inventory
from holoclip.holdout_source_clock_pipeline import calibrate_reserved_section_clocks
from holoclip.holdout_visual_pipeline import inspect_reserved_visual_edits
from holoclip.bounded_source_evidence import measure_reserved_source_pairs
from holoclip.provider_benchmark import atomic_json

VERSION = "holdout-real-media-run-audit-v1"
MULTI_BATCH_VERSION = "holdout-real-media-multi-batch-audit-v1"


@dataclass(frozen=True)
class EvidenceStage:
    name: str
    report: str
    version: str
    success: str
    execute: Callable


STAGES = (
    EvidenceStage("finished_video", "holdout-preparation.json",
                  "reserved-holdout-measurement-v1", "PREPARED_UNREVIEWED",
                  prepare_reserved_holdouts),
    EvidenceStage("original_audio", "holdout-audio-alignment.json",
                  "reserved-holdout-audio-alignment-v1", "PARTIAL_AUDIO_MATCH",
                  align_reserved_holdouts),
    EvidenceStage("source_video_pairs", "source_pairs/holdout-source-pairs.json",
                  "reserved-bounded-source-evidence-v1",
                  "MEASURED_UNREVIEWED_SOURCE_PAIRS", measure_reserved_source_pairs),
    EvidenceStage("sampled_visual_geometry",
                  "visual_edits/holdout-visual-observations.json",
                  "reserved-visual-observations-v1",
                  "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY", inspect_reserved_visual_edits),
    EvidenceStage("acoustic_cut_brackets", "cut_brackets/holdout-cut-brackets.json",
                  "reserved-acoustic-cut-brackets-v1",
                  "MEASURED_UNREVIEWED_CUT_BRACKETS", refine_reserved_cut_candidates),
    EvidenceStage("source_audio_clocks", "source_clocks/holdout-source-clock.json",
                  "reserved-source-section-clock-v1",
                  "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS",
                  calibrate_reserved_section_clocks),
    EvidenceStage("sampled_frame_links", "frame_links/holdout-frame-links.json",
                  "reserved-grounded-frame-links-v1",
                  "MEASURED_UNREVIEWED_FRAME_LINKS", correlate_reserved_source_frames),
    EvidenceStage("two_sided_cut_evidence", "cut_sides/holdout-cut-side-evidence.json",
                  "reserved-multimodal-cut-side-evidence-v1",
                  "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE", inspect_reserved_cut_sides),
    EvidenceStage("partial_time_map", "partial_time_maps/holdout-partial-time-map.json",
                  "reserved-partial-output-source-map-v1",
                  "MEASURED_UNREVIEWED_PARTIAL_MAP", build_reserved_partial_maps),
)


def _kwargs(stage: str, *, store: Path, state: Path, root: Path,
            rules: list, max_clips: int, progress) -> dict:
    common = {
        "store": store, "state_path": state, "policy_rules": rules,
        "max_clips": max_clips, "progress": progress,
    }
    if stage == "finished_video":
        return {**common, "output": root}
    args = {**common, "prepared_root": root}
    if stage == "original_audio":
        return {**args, "output": root}
    args["audio_root"] = root
    paths = {
        "source_video_pairs": (),
        "sampled_visual_geometry": ("source_pairs",),
        "acoustic_cut_brackets": ("source_pairs", "visual_edits"),
        "source_audio_clocks": ("source_pairs",),
        "sampled_frame_links": ("source_pairs", "source_clocks"),
        "two_sided_cut_evidence": ("source_pairs", "source_clocks",
                                   "frame_links", "cut_brackets"),
        "partial_time_map": ("source_pairs", "source_clocks", "frame_links",
                             "cut_brackets", "cut_sides"),
    }
    for folder in paths[stage]:
        key = {
            "source_pairs": "pairs_root",
            "visual_edits": "visual_root",
            "source_clocks": "clocks_root",
            "frame_links": "frame_links_root",
            "cut_brackets": "cuts_root",
            "cut_sides": "cut_sides_root",
        }.get(folder)
        if key:
            args[key] = root / folder
    if stage == "sampled_visual_geometry":
        args["output"] = root / "visual_edits"
    elif stage == "acoustic_cut_brackets":
        args["output"] = root / "cut_brackets"
    elif stage == "source_video_pairs":
        args["output"] = root / "source_pairs"
    elif stage == "source_audio_clocks":
        args["output"] = root / "source_clocks"
    elif stage == "sampled_frame_links":
        args["output"] = root / "frame_links"
    elif stage == "two_sided_cut_evidence":
        args["output"] = root / "cut_sides"
    elif stage == "partial_time_map":
        args["output"] = root / "partial_time_maps"
    return args


def summarize_observed_evidence(
    root: Path, expected_ids: set[str], *, completed_stages: set[str] | None = None,
) -> dict:
    """Report only stages executed in this invocation, never stale downstream data."""
    included = {s.name for s in STAGES} if completed_stages is None else completed_stages
    reports = {}
    per_clip = {clip_id: {} for clip_id in sorted(expected_ids)}
    stage_counts = {}
    for stage in STAGES:
        if stage.name not in included:
            stage_counts[stage.name] = {"NOT_EXECUTED_THIS_RUN": len(expected_ids)}
            for clip_id in expected_ids:
                per_clip[clip_id][stage.name] = "NOT_EXECUTED_THIS_RUN"
            continue
        path = root / stage.report
        if not path.is_file():
            stage_counts[stage.name] = {"missing_report": len(expected_ids)}
            for clip_id in expected_ids:
                per_clip[clip_id][stage.name] = "missing_report"
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != stage.version or any(
            data.get(flag) is not False for flag in (
                "qualification_allowed", "event_split_verified",
                "reviewed_gold_available", "benchmark_held_out",
            )
        ):
            raise ValueError("Unsafe or mismatched report: " + stage.name)
        reports[stage.name] = data
        tally = {}
        for clip_id in expected_ids:
            row = data.get("records", {}).get(clip_id, {})
            status = row.get("status", "NOT_MEASURED")
            per_clip[clip_id][stage.name] = status
            tally[status] = tally.get(status, 0) + 1
        stage_counts[stage.name] = tally

    fully_processed = [
        clip_id for clip_id, stages in per_clip.items()
        if len(included) == len(STAGES)
        and all(stages.get(s.name) == s.success for s in STAGES)
    ]
    matched = 0
    visual_pairs = 0
    jump_candidates = 0
    local_segments = 0
    total_map_coverage = 0.
    unknown_maps = 0
    maps = reports.get("partial_time_map", {}).get("records", {})
    for clip_id in expected_ids:
        audio = reports.get("original_audio", {}).get("records", {}).get(clip_id, {})
        if audio.get("status") == "PARTIAL_AUDIO_MATCH":
            matched += 1
        frame = reports.get("sampled_frame_links", {}).get("records", {}).get(clip_id, {})
        visual_pairs += sum(len(group.get("frame_links", [])) for group in
                            frame.get("group_observations", [])
                            if group.get("status") == "PARTIAL_AUDIO_VISUAL_FRAME_LINKS")
        side = reports.get("two_sided_cut_evidence", {}).get("records", {}).get(clip_id, {})
        jump_candidates += sum(
            int(item.get("candidate", {}).get("status") == "MULTIMODAL_EDIT_JUMP_CANDIDATE")
            for item in side.get("transitions", [])
        )
        payload = maps.get(clip_id, {}).get("time_map", {})
        if payload.get("status") == "PARTIAL_LOCAL_INTERVAL_CANDIDATES":
            local_segments += len(payload.get("segments", []))
            total_map_coverage += float(payload.get("candidate_coverage_seconds", 0.))
        elif maps.get(clip_id, {}).get("status") == "MEASURED_UNREVIEWED_PARTIAL_MAP":
            unknown_maps += 1

    return {
        "stage_status_counts": stage_counts,
        "per_clip_stage_status": per_clip,
        "all_nine_stages_measured_clips": len(fully_processed),
        "all_nine_stages_measured_clip_ids": sorted(fully_processed),
        "clips_with_unique_original_audio_matches": matched,
        "sampled_source_finished_frame_links": visual_pairs,
        "multimodal_cut_candidates": jump_candidates,
        "local_time_map_candidate_segments": local_segments,
        "candidate_coverage_seconds": round(total_map_coverage, 3),
        "measured_but_no_supported_local_map_clips": unknown_maps,
        "editor_cut_precision": None, "editor_cut_recall": None,
        "native_timestamp_error_seconds": None, "event_holdout_accuracy": None,
        "accuracy_unavailable_reason": (
            "No independent reviewer-verified exact editor cuts and event-level gold"
        ),
    }


def _select_batch(
    state: CorpusGrowthState, root: Path, max_clips: int,
    *, exclude_clip_ids: frozenset[str] = frozenset(),
) -> tuple[list, int]:
    """Prioritize still-unmeasured clip records; never use cache for qualification."""
    candidates = [
        item for item in _candidates(state)
        if human_clip_identity(item.clip_url) not in exclude_clip_ids
    ]
    last = root / STAGES[-1].report
    completed = {}
    if last.is_file():
        raw = json.loads(last.read_text(encoding="utf-8"))
        if (raw.get("version") == STAGES[-1].version
                and raw.get("qualification_allowed") is False
                and raw.get("reviewed_gold_available") is False
                and raw.get("event_split_verified") is False):
            completed = raw.get("records", {})
    ranked = sorted(
        enumerate(candidates), key=lambda pair: (
            completed.get(human_clip_identity(pair[1].clip_url), {}).get("status")
                == STAGES[-1].success,
            pair[0],
        ),
    )
    selected = [item for _, item in ranked[:max_clips]]
    remaining = max(0, len(candidates) - len(selected))
    return selected, remaining


def _batch_state(state: CorpusGrowthState, selected: list) -> CorpusGrowthState:
    """Project only selected reserved clips, retaining *all* nonholdout learning history.

    Crucially, durable holdout_stream_ids and queued-learning items stay intact;
    this is an ephemeral execution view, not a mutation of the corpus state.
    """
    selected_ids = {human_clip_identity(item.clip_url) for item in selected}
    if len(selected_ids) != len(selected):
        raise ValueError("Ambiguous duplicate reserved clip ID")
    subset = state.model_copy(deep=True)
    subset.items = {
        key: item for key, item in subset.items.items()
        if item.status != "holdout_reserved"
        or human_clip_identity(item.clip_url) in selected_ids
    }
    if len(_candidates(subset)) != len(selected):
        raise ValueError("Reserved batch projection is incomplete")
    return subset


def _run_reserved_holdout_e2e_unlocked(
    provider, *, store: Path, state_path: Path, root: Path,
    policy_rules: list, max_clips: int = 3,
    preflight_only: bool = False, progress=None, runners=None,
    exclude_clip_ids: frozenset[str] = frozenset(),
) -> dict:
    """Single bounded call on *real* local media; checkpoint all stage outcomes."""
    if not 1 <= max_clips <= 20:
        raise ValueError("1..20 reserved clip budget required")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("holdout-e2e-preflight.json" if preflight_only
                   else "holdout-e2e-audit.json")
    output = {
        "version": VERSION, "status": "BLOCKED_NO_RESERVED_CLIPS",
        "preflight_only": preflight_only,
        "real_media_run_completed": False,
        "production_ready": False, "ready": False, "qualification_allowed": False,
        "benchmark_held_out": False, "event_split_verified": False,
        "reviewed_gold_available": False, "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "stages": [], "report_path": str(path),
    }

    def save():
        atomic_json(path, output)

    if not state_path.is_file():
        output["status"] = "BLOCKED_MISSING_RESERVATION_STATE"
        save()
        return output
    state_bytes = state_path.read_bytes()
    output["reservation_state_sha256"] = hashlib.sha256(state_bytes).hexdigest()
    state = CorpusGrowthState.model_validate_json(state_bytes)
    candidates = _candidates(state)
    inventory = holdout_inventory(store, state_path)
    output["reserved_clips"] = len(candidates)
    output["reserved_original_streams"] = len(inventory["reserved_streams"])
    if not candidates:
        save()
        return output
    if inventory["conflicted_streams"]:
        output["status"] = "BLOCKED_TRAIN_HOLDOUT_SPLIT"
        output["conflicted_streams_count"] = len(inventory["conflicted_streams"])
        save()
        return output
    if len({human_clip_identity(item.clip_url) for item in candidates}) != len(candidates):
        output["status"] = "BLOCKED_AMBIGUOUS_RESERVATION"
        save()
        return output

    try:
        batch, remaining = _select_batch(
            state, root, max_clips, exclude_clip_ids=exclude_clip_ids,
        )
    except (ValueError, OSError):
        output["status"] = "BLOCKED_INVALID_PRIOR_CHECKPOINT"
        save()
        return output
    output["selected_clips"] = len(batch)
    output["remaining_clips_after_batch"] = remaining
    selected_ids = [human_clip_identity(item.clip_url) for item in batch]
    output["selected_clip_ids"] = selected_ids
    try:
        projected = _batch_state(state, batch)
    except ValueError:
        output["status"] = "BLOCKED_AMBIGUOUS_RESERVATION"
        save()
        return output
    output["status"] = "PREFLIGHT_ONLY_NO_MEDIA_EXECUTED" if preflight_only else "RUNNING"
    save()
    if preflight_only:
        return output

    batch_path = root / "holdout-e2e-batch-state.json"
    atomic_json(batch_path, projected.model_dump(mode="json"))
    original_sha = hashlib.sha256(state_bytes).hexdigest()
    ids = set(selected_ids)
    completed = set()
    for stage in STAGES:
        if progress:
            progress(f"reserved media trial: {stage.name}")
        fn = runners.get(stage.name) if runners is not None else stage.execute
        if not callable(fn):
            output["status"] = "BLOCKED_INVALID_RUNNER"
            output["blocked_stage"] = stage.name
            save()
            return output
        try:
            # If a reservation changes while FFmpeg is running, abort
            # instead of continuing with a stale holdout source assignment.
            if (not state_path.is_file()
                    or hashlib.sha256(state_path.read_bytes()).hexdigest() != original_sha):
                output["status"] = "BLOCKED_CHANGED_RESERVATION_STATE"
                output["blocked_stage"] = stage.name
                save()
                return output
            fn(provider, **_kwargs(
                stage.name, store=store, state=batch_path, root=root,
                rules=policy_rules, max_clips=len(batch), progress=progress,
            ))
            completed.add(stage.name)
            metrics = summarize_observed_evidence(
                root, ids, completed_stages=completed,
            )
            counts = metrics["stage_status_counts"].get(stage.name, {})
            step = {"stage": stage.name, "counts": counts, "report": stage.report}
            output["stages"].append(step)
            output["observations"] = metrics
            if counts.get(stage.success, 0) != len(ids):
                output["status"] = "BLOCKED_UPSTREAM_STAGE"
                output["blocked_stage"] = stage.name
                save()
                return output
        except Exception as exc:
            # Never persist raw metadata, stderr, API tokens or remote URLs.
            output["status"] = "STAGE_FAILED"
            output["blocked_stage"] = stage.name
            output["error_class"] = type(exc).__name__
            save()
            return output
        save()
    if (not state_path.is_file()
            or hashlib.sha256(state_path.read_bytes()).hexdigest() != original_sha):
        output["status"] = "BLOCKED_CHANGED_RESERVATION_STATE"
        output["blocked_stage"] = "postflight"
        save()
        return output
    output["real_media_run_completed"] = runners is None
    output["status"] = (
        "UNREVIEWED_MEASUREMENTS_COMPLETE" if runners is None
        else "SIMULATED_STAGE_TEST_COMPLETE"
    )
    save()
    return output


def run_reserved_holdout_e2e(
    provider, *, store: Path, state_path: Path, root: Path,
    policy_rules: list, max_clips: int = 3,
    preflight_only: bool = False, progress=None, runners=None,
) -> dict:
    """Own an exclusive run lock *before* reading/writing any shared audit.

    Refuse overlapping full/preflight calls rather than clobber an active
    report or projected corpus state. The lock is removed only by its owner.
    A crash leaves a lock requiring operator investigation (safe by default).
    """
    if not 1 <= max_clips <= 20:
        raise ValueError("1..20 reserved clip budget required")
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".holdout-e2e.lock"
    try:
        handle = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {
            "version": VERSION, "status": "BLOCKED_CONCURRENT_RUN",
            "real_media_run_completed": False, "ready": False,
            "production_ready": False, "qualification_allowed": False,
            "report_path": str(root / "holdout-e2e-audit.json"),
        }
    try:
        os.close(handle)
        return _run_reserved_holdout_e2e_unlocked(
            provider, store=store, state_path=state_path, root=root,
            policy_rules=policy_rules, max_clips=max_clips,
            preflight_only=preflight_only, progress=progress,
            runners=runners,
        )
    finally:
        lock.unlink(missing_ok=True)


def run_all_reserved_holdout_batches(
    provider, *, store: Path, state_path: Path, root: Path,
    policy_rules: list, max_clips: int = 3, max_batches: int = 8,
    progress=None, runners=None,
) -> dict:
    """One bounded invocation over distinct reserved clips; fail-stop on bad evidence.

    This is NOT a production qualification, event-level test, or automatic
    retries of failed reservations. It drives the same nine guarded stage
    functions for each selected batch, never evaluating the same clip twice
    in one invocation, even when all clips were previously cached.
    """
    if not 1 <= max_clips <= 20 or not 1 <= max_batches <= 12:
        raise ValueError("Batch size 1..20 and invocation budget 1..12 required")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "holdout-e2e-all-batches.json"
    lock = root / ".holdout-e2e.lock"
    try:
        handle = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {
            "version": MULTI_BATCH_VERSION, "status": "BLOCKED_CONCURRENT_RUN",
            "real_media_run_completed": False, "ready": False,
            "production_ready": False, "qualification_allowed": False,
            "report_path": str(path),
        }

    result = {
        "version": MULTI_BATCH_VERSION, "status": "RUNNING",
        "report_path": str(path), "real_media_run_completed": False,
        "ready": False, "production_ready": False, "qualification_allowed": False,
        "event_split_verified": False, "reviewed_gold_available": False,
        "benchmark_held_out": False, "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "max_clips_per_batch": max_clips, "max_batches": max_batches,
        "batches": [], "processed_clip_ids": [], "processed_clips": 0,
        "remaining_clips": 0,
        "observed_totals": {
            "clips_with_unique_original_audio_matches": 0,
            "sampled_source_finished_frame_links": 0,
            "multimodal_cut_candidates": 0,
            "local_time_map_candidate_segments": 0,
            "candidate_coverage_seconds": 0.,
        },
        "editor_cut_precision": None, "editor_cut_recall": None,
        "native_timestamp_error_seconds": None, "event_holdout_accuracy": None,
    }

    def save() -> None:
        atomic_json(path, result)

    try:
        os.close(handle)
        if not state_path.is_file():
            result["status"] = "BLOCKED_MISSING_RESERVATION_STATE"
            save()
            return result
        initial_state = state_path.read_bytes()
        reservation_hash = hashlib.sha256(initial_state).hexdigest()
        result["reservation_state_sha256"] = reservation_hash
        state = CorpusGrowthState.model_validate_json(initial_state)
        candidates = _candidates(state)
        all_ids = {human_clip_identity(c.clip_url) for c in candidates}
        result["reserved_clips"] = len(candidates)
        result["remaining_clips"] = len(candidates)
        if not candidates:
            result["status"] = "BLOCKED_NO_RESERVED_CLIPS"
            save()
            return result
        if len(all_ids) != len(candidates):
            result["status"] = "BLOCKED_AMBIGUOUS_RESERVATION"
            save()
            return result

        seen: set[str] = set()
        for number in range(1, max_batches + 1):
            if len(seen) == len(all_ids):
                break
            if (not state_path.is_file()
                    or hashlib.sha256(state_path.read_bytes()).hexdigest() != reservation_hash):
                result["status"] = "BLOCKED_CHANGED_RESERVATION_STATE"
                save()
                return result
            if progress:
                progress(f"reserved media batch {number}/{max_batches}")
            try:
                batch = _run_reserved_holdout_e2e_unlocked(
                    provider, store=store, state_path=state_path,
                    root=root, policy_rules=policy_rules, max_clips=max_clips,
                    progress=progress, runners=runners,
                    exclude_clip_ids=frozenset(seen),
                )
            except Exception as exc:
                # No remote/auth-bearing stderr or exception message persisted.
                result["status"] = "STAGE_FAILED"
                result["error_class"] = type(exc).__name__
                result["failed_batch"] = number
                save()
                return result

            selected = batch.get("selected_clip_ids", [])
            # Never report success for a duplicate, unreserved or empty batch.
            if (not isinstance(selected, list) or not selected
                    or len(selected) != len(set(selected))
                    or set(selected) & seen or not set(selected) <= all_ids):
                result["status"] = "BLOCKED_INVALID_BATCH_SELECTION"
                result["failed_batch"] = number
                save()
                return result
            summary = batch.get("observations", {})
            item = {
                "number": number, "status": batch["status"],
                "selected_clip_ids": selected,
                "completed_stage_count": len(batch.get("stages", [])),
                "blocked_stage": batch.get("blocked_stage"),
                "stage_status_counts": summary.get("stage_status_counts", {}),
                "observations": {
                    k: summary.get(k, 0) for k in result["observed_totals"]
                },
            }
            result["batches"].append(item)
            if batch["status"] not in (
                "UNREVIEWED_MEASUREMENTS_COMPLETE",
                "SIMULATED_STAGE_TEST_COMPLETE",
            ):
                result["status"] = "BLOCKED_BATCH"
                result["failed_batch"] = number
                result["blocked_stage"] = batch.get("blocked_stage")
                result["blocked_batch_status"] = batch["status"]
                save()
                return result
            if summary.get("all_nine_stages_measured_clips") != len(selected):
                result["status"] = "BLOCKED_BATCH_INCOMPLETE"
                result["failed_batch"] = number
                save()
                return result
            seen.update(selected)
            result["processed_clip_ids"] = sorted(seen)
            result["processed_clips"] = len(seen)
            result["remaining_clips"] = len(all_ids - seen)
            for key in result["observed_totals"]:
                result["observed_totals"][key] += item["observations"][key]
            # Persist after every completed batch, not only at invocation exit.
            save()

        if len(seen) != len(all_ids):
            result["status"] = "BOUNDED_BATCH_LIMIT_REACHED"
        elif (not state_path.is_file()
              or hashlib.sha256(state_path.read_bytes()).hexdigest() != reservation_hash):
            result["status"] = "BLOCKED_CHANGED_RESERVATION_STATE"
        else:
            result["status"] = (
                "UNREVIEWED_ALL_BATCHES_COMPLETE" if runners is None
                else "SIMULATED_ALL_BATCHES_COMPLETE"
            )
            result["real_media_run_completed"] = runners is None
        save()
        return result
    finally:
        lock.unlink(missing_ok=True)
