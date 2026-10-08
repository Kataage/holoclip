"""Run all local/public reserved-video evidence stages and report *observed* coverage.

No synthesized gold, no paid model, no precision/recall without an independent
human reference. Failure at any stage aborts instead of silently skipping.
"""
from __future__ import annotations

import json
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


def summarize_observed_evidence(root: Path, expected_ids: set[str]) -> dict:
    """Report coverage and failure modes, not ground-truth accuracy."""
    reports = {}
    per_clip = {clip_id: {} for clip_id in sorted(expected_ids)}
    stage_counts = {}
    for stage in STAGES:
        path = root / stage.report
        if not path.is_file():
            stage_counts[stage.name] = {"missing_report": len(expected_ids)}
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != stage.version or any(
            data.get(flag) is not False for flag in (
                "qualification_allowed", "event_split_verified",
                "reviewed_gold_available",
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
        if all(stages.get(s.name) == s.success for s in STAGES)
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


def run_reserved_holdout_e2e(
    provider, *, store: Path, state_path: Path, root: Path,
    policy_rules: list, max_clips: int = 3,
    preflight_only: bool = False, progress=None, runners=None,
) -> dict:
    """Single bounded call on *real* local media; checkpoint all stage outcomes."""
    if not 1 <= max_clips <= 20:
        raise ValueError("1..20 reserved clip budget required")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "holdout-e2e-audit.json"
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
    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
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
    if len(candidates) > max_clips:
        output["status"] = "BLOCKED_RESERVATION_BATCH_BUDGET"
        output["required_max_clips"] = len(candidates)
        save()
        return output
    output["status"] = "PREFLIGHT_ONLY_NO_MEDIA_EXECUTED" if preflight_only else "RUNNING"
    save()
    if preflight_only:
        return output

    ids = {human_clip_identity(x.clip_url) for x in candidates}
    for stage in STAGES:
        if progress:
            progress(f"reserved media trial: {stage.name}")
        fn = runners.get(stage.name) if runners is not None else stage.execute
        if not callable(fn):
            raise ValueError("Missing stage runner: " + stage.name)
        try:
            fn(provider, **_kwargs(stage.name, store=store, state=state_path,
                                   root=root, rules=policy_rules,
                                   max_clips=max_clips, progress=progress))
            metrics = summarize_observed_evidence(root, ids)
            counts = metrics["stage_status_counts"].get(stage.name, {})
            step = {"stage": stage.name, "counts": counts, "report": stage.report}
            output["stages"].append(step)
            output["observations"] = metrics
            if counts.get(stage.success, 0) == 0 or sum(counts.values()) != len(ids):
                output["status"] = "BLOCKED_UPSTREAM_STAGE"
                output["blocked_stage"] = stage.name
                save()
                return output
        except Exception as exc:
            # Never persist auth-bearing stderr, remote metadata, or URLs.
            output["status"] = "STAGE_FAILED"
            output["blocked_stage"] = stage.name
            output["error_class"] = type(exc).__name__
            save()
            return output
        save()
    output["real_media_run_completed"] = True
    output["status"] = "UNREVIEWED_MEASUREMENTS_COMPLETE"
    save()
    return output
