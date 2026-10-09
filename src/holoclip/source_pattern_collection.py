"""Bounded unattended discovery and *unreviewed* real-video pattern measurement.

This orchestrates existing yt-dlp source acquisition and constructed self-controls.
Neither public search results nor transformed self-controls are editor/source gold.
"""
from __future__ import annotations

from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState, grow_corpus
from holoclip.source_candidate_visual import verify_pending_source_visual
from holoclip.source_visual_control_report import report_saved_source_controls
from holoclip.source_visual_pattern_report import report_saved_patterns

VERSION = "autonomous-source-pattern-collector-v1"
_NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "precision_recall_estimable": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
    "ready": False,
    "production_ready": False,
}


def _latest_attempt_outcomes(state_path: Path, keys: list[str]) -> dict:
    """Report only the current run's exact attempted clips, not stale probes."""
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    counts: dict[str, int] = {}
    for key in keys:
        item = state.items.get(key)
        if item is None or item.status != "source_visual_pending":
            raise ValueError("Missing or changed attempted visual clip")
        probe = (item.source_scout or {}).get("visual_probe")
        if not isinstance(probe, dict):
            raise ValueError("Missing persisted source-visual attempt")
        observations = probe.get("candidate_observations")
        if not isinstance(observations, list):
            raise ValueError("Malformed source acquisition observations")
        if not observations:
            name = "clip:" + str(probe.get("status", "UNKNOWN_NO_VIDEO_ATTEMPT"))
            counts[name] = counts.get(name, 0) + 1
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("Invalid source acquisition observation")
            outcome = observation.get("constructed_pattern_observation")
            if isinstance(outcome, dict):
                result = outcome.get("result")
                status = result.get("status") if isinstance(result, dict) else None
                if not isinstance(status, str):
                    raise ValueError("Malformed measured pattern diagnostic")
                name = "pattern:" + status
            elif "constructed_pattern_error_class" in observation:
                name = "pattern:UNKNOWN_CONTROL_EXECUTION_FAILURE"
            else:
                status = observation.get("status")
                if not isinstance(status, str):
                    raise ValueError("Invalid source acquisition status")
                name = "source:" + status
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _latest_attempt_stage_diagnostics(state_path: Path, keys: list[str]) -> dict:
    """Disjoint diagnostics for each stage, not a proxy for real-video gold.

    The historic single combined outcome prefers a successful constructed
    control over a failed source/finished visual match. Keep those independent
    so the unattended operator can locate acquisition and evidence gaps.
    Every count here is a *saved record*, not verified video identity.
    """
    if len(keys) != len(set(keys)) or not all(isinstance(k, str) for k in keys):
        raise ValueError("Duplicate or malformed attempted visual clip keys")
    candidate_statuses: dict[str, int] = {}
    pattern_statuses: dict[str, int] = {}
    probe_modes: dict[str, int] = {}
    clip_statuses: dict[str, int] = {}
    candidate_count = 0
    sampled_source_section_records = 0
    finished_sha_recorded_clips = 0
    pattern_execution_failures = 0
    cross_revisit_attempted_clips = 0
    if keys:
        state = CorpusGrowthState.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
        for key in keys:
            item = state.items.get(key)
            if item is None or item.status != "source_visual_pending":
                raise ValueError("Missing or changed attempted visual clip")
            probe = (item.source_scout or {}).get("visual_probe")
            if not isinstance(probe, dict):
                raise ValueError("Missing persisted source-visual attempt")
            status = probe.get("status")
            observations = probe.get("candidate_observations")
            if not isinstance(status, str) or not status:
                raise ValueError("Invalid persisted visual clip status")
            if not isinstance(observations, list):
                raise ValueError("Malformed source acquisition observations")
            clip_statuses[status] = clip_statuses.get(status, 0) + 1
            if probe.get("finished_video_sha256") is not None:
                digest = probe["finished_video_sha256"]
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)):
                    raise ValueError("Invalid recorded finished video SHA-256")
                finished_sha_recorded_clips += 1
            if "cross_revisit_step_before" in probe:
                cross_revisit_attempted_clips += 1
            for observed in observations:
                if not isinstance(observed, dict):
                    raise ValueError("Invalid source acquisition observation")
                outcome = observed.get("status")
                if not isinstance(outcome, str) or not outcome:
                    raise ValueError("Invalid source acquisition status")
                candidate_count += 1
                candidate_statuses[outcome] = candidate_statuses.get(outcome, 0) + 1
                mode = observed.get("probe_mode")
                if mode is not None:
                    if not isinstance(mode, str) or not mode:
                        raise ValueError("Malformed source probe mode")
                    probe_modes[mode] = probe_modes.get(mode, 0) + 1
                windows = observed.get("sampled_source_windows")
                if not isinstance(windows, list):
                    raise ValueError("Malformed sampled source section records")
                sampled_source_section_records += len(windows)
                pattern = observed.get("constructed_pattern_observation")
                if pattern is not None:
                    result = pattern.get("result") if isinstance(pattern, dict) else None
                    pattern_status = result.get("status") if isinstance(result, dict) else None
                    if not isinstance(pattern_status, str) or not pattern_status:
                        raise ValueError("Invalid constructed pattern status")
                    pattern_statuses[pattern_status] = (
                        pattern_statuses.get(pattern_status, 0) + 1
                    )
                if "constructed_pattern_error_class" in observed:
                    if pattern is not None or not isinstance(
                        observed["constructed_pattern_error_class"], str
                    ):
                        raise ValueError("Contradictory pattern execution observation")
                    pattern_execution_failures += 1
    return {
        "attempted_clip_records": len(keys),
        "candidate_observation_records": candidate_count,
        "finished_video_sha_recorded_clips": finished_sha_recorded_clips,
        "source_window_section_records": sampled_source_section_records,
        "constructed_pattern_execution_failures": pattern_execution_failures,
        "cross_revisit_attempted_clips": cross_revisit_attempted_clips,
        "clip_status_counts": dict(sorted(clip_statuses.items())),
        "candidate_visual_status_counts": dict(sorted(candidate_statuses.items())),
        "constructed_pattern_status_counts": dict(sorted(pattern_statuses.items())),
        "source_probe_mode_counts": dict(sorted(probe_modes.items())),
        "measured_original_match_gold": False,
        "record_counts_are_verified_public_media": False,
    }


async def _no_ingest(**_kwargs):
    raise AssertionError("Metadata-only source discovery must never ingest media")


async def collect_public_source_patterns(
    provider, *, store: Path, state_path: Path, output: Path, policy_rules: list,
    cycles: int = 3, max_clips: int = 2, max_candidates: int = 2,
    scan_limit: int = 20, target_distinct_streams: int = 3,
    discover: bool = True, progress=None,
) -> dict:
    """Discover at no cloud inference cost and collect bounded video evidence.

    Existing queue and histories persist after each clip. The returned delta is
    descriptive; a success ratio is NEVER fabricated from unavailable video.
    """
    if (type(cycles) is not int or not 1 <= cycles <= 20
            or type(max_clips) is not int or not 1 <= max_clips <= 2
            or type(max_candidates) is not int or not 1 <= max_candidates <= 2
            or type(scan_limit) is not int or not 1 <= scan_limit <= 100
            or type(target_distinct_streams) is not int
            or not 1 <= target_distinct_streams <= 50
            or type(discover) is not bool):
        raise ValueError("Invalid source pattern collection budget")
    # Fail closed on history tampering before making public network requests.
    before = report_saved_source_controls(state_path)
    before_patterns = before["constructed_pattern_audit"]
    discovery = None
    if discover:
        discovery = await grow_corpus(
            provider, store=store, state_path=state_path,
            ingest=_no_ingest, policy_rules=policy_rules, discover_only=True,
            scan_limit=scan_limit, progress=progress,
        )
    runs = []
    previous = report_saved_patterns(state_path)
    distinct_before = before_patterns["distinct_candidate_source_streams_measured"]
    # The target is fresh candidate sources measured THIS invocation, not a
    # lifetime quota. Otherwise scheduled runs stop forever after the first
    # successful three-stream day and cannot learn about newer public videos.
    # Access challenges affect the runner's public IP, not only one clip.
    # On a three-probe hostwide pause, do not immediately retry other
    # yt-dlp video ranges during the same unattended invocation.
    visual_cycles = (
        0 if discovery and discovery.get("metadata_access_paused") is True
        else cycles
    )
    for cycle in range(visual_cycles):
        if (previous["distinct_candidate_source_streams_measured"] - distinct_before
                >= target_distinct_streams):
            break
        observed = verify_pending_source_visual(
            provider, store=store, state_path=state_path, output=output,
            policy_rules=policy_rules, max_clips=max_clips,
            max_candidates=max_candidates, progress=progress,
        )
        updated = report_saved_patterns(state_path)
        keys = observed.get("attempted_clip_keys", [])
        counts = (_latest_attempt_outcomes(state_path, keys) if keys else {})
        row = {
            "cycle": cycle + 1,
            "attempted_clips": observed["attempted"],
            "attempted_clip_keys": keys,
            "verification_outcomes": observed["counts"],
            "source_pattern_attempt_outcomes": counts,
            "stage_diagnostics": _latest_attempt_stage_diagnostics(
                state_path, keys,
            ),
            "new_unique_video_samples": (
                updated["real_measured_source_video_samples"]
                - previous["real_measured_source_video_samples"]
            ),
            "new_distinct_candidate_streams": (
                updated["distinct_candidate_source_streams_measured"]
                - previous["distinct_candidate_source_streams_measured"]
            ),
        }
        if row["new_unique_video_samples"] < 0 or row["new_distinct_candidate_streams"] < 0:
            raise ValueError("Pattern evidence count unexpectedly decreased")
        runs.append(row)
        previous = updated
        if progress:
            progress(f"source-pattern cycle {cycle + 1}: "
                     f"+{row['new_unique_video_samples']} unique measured samples")
        if not observed["attempted"]:
            break
    final = report_saved_source_controls(state_path)
    patterns = final["constructed_pattern_audit"]
    new_samples = (
        patterns["real_measured_source_video_samples"]
        - before_patterns["real_measured_source_video_samples"]
    )
    new_sources = (
        patterns["distinct_candidate_source_streams_measured"] - distinct_before
    )
    if new_samples < 0 or new_sources < 0:
        raise ValueError("Persisted video pattern history shrank during collection")
    return {
        "version": VERSION,
        "status": (
            "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS"
            if new_samples > 0
            else "UNKNOWN_NO_NEW_PUBLIC_VIDEO_PATTERN_MEASUREMENTS"
            if patterns["real_measured_source_video_samples"] > 0
            else "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS"
        ),
        "discovery": discovery,
        "cycles_completed": len(runs),
        "cycle_diagnostics": runs,
        "observed_source_samples_before": (
            before_patterns["real_measured_source_video_samples"]
        ),
        "new_unique_video_samples_this_run": new_samples,
        "distinct_candidate_streams_measured": (
            patterns["distinct_candidate_source_streams_measured"]
        ),
        "distinct_candidate_streams_before": distinct_before,
        "new_distinct_candidate_streams_this_run": new_sources,
        "target_scope": "this_run_new_distinct_candidate_streams",
        "target_distinct_streams": target_distinct_streams,
        "target_reached": new_sources >= target_distinct_streams,
        "controls": final,
        **_NO_GOLD,
    }
