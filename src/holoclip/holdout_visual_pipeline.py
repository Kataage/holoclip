"""Unattended, non-promotable visual observations of measured source/finished pairs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.bounded_source_evidence import _checked_measurement
from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata, evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _candidates, _measured, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import BenchmarkCase, atomic_json, verified_window
from holoclip.providers.cost import CostPolicyError
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file
from holoclip.visual_edit_evidence import VERSION as VISUAL_VERSION, inspect_visual_pair

VERSION = "reserved-visual-observations-v1"


def _fingerprint(pairs: list[dict], *, max_frames: int, time_tolerance: float) -> str:
    values = {
        "version": VERSION, "method": VISUAL_VERSION, "max_frames": max_frames,
        "time_tolerance": time_tolerance,
        "pairs": [
            {
                **{key: pair.get(key) for key in (
                    "source_start", "source_end", "clip_start", "clip_end",
                    "source_video_sha256", "source_window_sha256",
                    "finished_window_sha256", "source_measurement", "finished_measurement",
                )},
                # The measured input window's frame sampling/checksums are part of
                # the editing evidence identity, not only the bounded video bytes.
                "source_manifest_sha256": checksum_file(Path(pair["source_measurement"])),
                "finished_manifest_sha256": checksum_file(Path(pair["finished_measurement"])),
            } for pair in pairs
        ],
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def inspect_reserved_visual_edits(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, output: Path, policy_rules: list,
    max_clips: int = 3, max_frames: int = 64, time_tolerance: float = 4.0,
    progress=None, inspect=inspect_visual_pair,
) -> dict:
    """Source-time and edit semantics stay unknown unless separately validated."""
    if not 1 <= max_clips <= 20 or not 1 <= max_frames <= 64:
        raise ValueError("Invalid bounded visual inspection limits")
    if not 0 < time_tolerance <= 10:
        raise ValueError("Time tolerance must be in 0..10s")
    prepared = json.loads(
        (prepared_root / "holdout-preparation.json").read_text(encoding="utf-8")
    )
    audio = json.loads(
        (audio_root / "holdout-audio-alignment.json").read_text(encoding="utf-8")
    )
    pairs_data = json.loads(
        (pairs_root / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )
    for report, version in (
        (prepared, "reserved-holdout-measurement-v1"),
        (audio, "reserved-holdout-audio-alignment-v1"),
        (pairs_data, "reserved-bounded-source-evidence-v1"),
    ):
        if (report.get("version") != version
                or any(report.get(key) is not False for key in (
                    "qualification_allowed", "reviewed_gold_available",
                    "event_split_verified",
                ))):
            raise ValueError("Only unreviewed diagnostic evidence is accepted")
    if pairs_data.get("frame_accurate_cuts_verified") is not False:
        raise ValueError("Frame-accurate cut proof was not supplied")
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    inventory = holdout_inventory(store, state_path)
    examples = load_human_clip_store(store)
    learning_streams = {x.stream_id for x in examples if x.stream_id}
    learning_clips = {human_clip_identity(x.clip_url) for x in examples}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-visual-observations.json"
    old = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = old.get("records", {}) if old.get("version") == VERSION else {}
    result = {
        "version": VERSION, "status": "UNREVIEWED_VISUAL_EVIDENCE",
        "qualification_allowed": False, "reviewed_gold_available": False,
        "benchmark_held_out": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False, "records": records,
    }

    def save():
        atomic_json(report_path, result)

    attempted = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"OBSERVED_UNREVIEWED_VISUAL_GEOMETRY"},
    ):
        if attempted >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        base = {
            "clip_id": clip_id, "clip_url": item.clip_url,
            "source_url": item.source_url, "source_stream_id": item.source_stream_id,
            "qualification_allowed": False, "reviewed_gold_available": False,
            "event_split_verified": False, "frame_accurate_cuts_verified": False,
        }

        def block(status: str):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"visual edits {clip_id}: {status}")

        if (item.source_stream_id in learning_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in learning_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if not item.source_url or item.source_stream_id not in state.holdout_stream_ids:
            block("BLOCKED_NOT_RESERVED")
            continue
        p = prepared.get("records", {}).get(clip_id, {})
        a = audio.get("records", {}).get(clip_id, {})
        e = pairs_data.get("records", {}).get(clip_id, {})
        if (not p or p.get("status") != "PREPARED_UNREVIEWED"
                or p.get("source_url") != item.source_url
                or p.get("source_stream_id") != item.source_stream_id
                or not _measured(p)):
            block("BLOCKED_FINISHED_VIDEO_UNVERIFIED")
            continue
        source_audio_path = a.get("source_audio_path")
        if (a.get("status") != "PARTIAL_AUDIO_MATCH"
                or a.get("source_url") != item.source_url
                or a.get("source_stream_id") != item.source_stream_id
                or a.get("clip_media_sha256") != p["media_sha256"]
                or not source_audio_path or not Path(source_audio_path).is_file()
                or checksum_file(Path(source_audio_path)) != a.get("source_media_sha256")):
            block("BLOCKED_SOURCE_AUDIO_UNVERIFIED")
            continue
        pairs = e.get("pairs")
        plans = e.get("plans")
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or e.get("source_url") != item.source_url
                or e.get("source_stream_id") != item.source_stream_id
                or e.get("finished_media_sha256") != p["media_sha256"]
                or e.get("audio_source_sha256") != a["source_media_sha256"]
                or e.get("audio_method") != "audio-band-anchor-v1"
                or not isinstance(pairs, list) or not 1 <= len(pairs) <= 5
                or not isinstance(plans, list) or plans != [
                    {k: pair[k] for k in (
                        "group", "source_start", "source_end",
                        "clip_start", "clip_end", "matched_anchors", "min_correlation",
                    )} for pair in pairs
                ]):
            block("BLOCKED_SOURCE_PAIRS_UNVERIFIED")
            continue

        # Check input artifact checksums *before* any network metadata probe.
        try:
            source_windows = []
            finished_windows = []
            for pair in pairs:
                video = Path(pair["source_video_path"])
                if (not video.is_file() or video.stat().st_size > 256 * 1024 * 1024
                        or checksum_file(video) != pair["source_video_sha256"]):
                    raise ValueError("Bad source section checksum")
                if _checked_measurement(pair["source_measurement"]) != pair["source_window_sha256"]:
                    raise ValueError("Invalid source measurement")
                if _checked_measurement(pair["finished_measurement"]) != pair["finished_window_sha256"]:
                    raise ValueError("Invalid finished measurement")
                source_windows.append(verified_window(BenchmarkCase(
                    id="visual-source", clip_id=clip_id, task="video", source_url=item.source_url,
                    teacher_reference="unreviewed", public_data=True,
                    dimensions=frozenset(), measurement_path=pair["source_measurement"],
                )))
                finished_windows.append(verified_window(BenchmarkCase(
                    id="visual-finished", clip_id=clip_id, task="video", source_url=item.clip_url,
                    teacher_reference="unreviewed", public_data=True,
                    dimensions=frozenset(), measurement_path=pair["finished_measurement"],
                )))
        except (OSError, ValueError, KeyError, TypeError, CostPolicyError):
            block("BLOCKED_MEASUREMENT_INTEGRITY")
            continue

        attempted += 1
        try:
            clip = provider.probe(item.clip_url)
            source = provider.probe(item.source_url)
            if not _public(clip) or not _public(source):
                block("QUARANTINED_NOT_PUBLIC_ENDED")
                continue
            if (human_clip_identity(clip.url) != clip_id
                    or source.external_id != item.source_stream_id
                    or human_clip_identity(source.url)
                       != human_clip_identity(item.source_url)):
                block("QUARANTINED_SOURCE_CHANGED")
                continue
            candidate = choose_source_candidate(provider.source_candidates(clip))
            if candidate is None or human_clip_identity(candidate.url) != human_clip_identity(item.source_url):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            clip_policy = evaluate_source_metadata(
                clip, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            source_policy = evaluate_source_metadata(
                source, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            range_policy = evaluate_source_ranges(
                source,
                ranges=[(pair["source_start"], pair["source_end"]) for pair in pairs],
                rules=policy_rules, operation=PolicyOperation.LEARN,
                deferred_findings=source_policy.findings,
            )
            if (clip_policy.decision is not PolicyDecision.ALLOW
                    or source_policy.decision is not PolicyDecision.ALLOW
                    or range_policy.decision is not PolicyDecision.ALLOW):
                block("QUARANTINED_POLICY")
                continue
            identity = _fingerprint(
                pairs, max_frames=max_frames, time_tolerance=time_tolerance
            )
            old_row = records.get(clip_id, {})
            if (old_row.get("status") == "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY"
                    and old_row.get("fingerprint") == identity
                    and old_row.get("source_stream_id") == item.source_stream_id
                    and len(old_row.get("pair_observations", [])) == len(pairs)):
                if progress:
                    progress(f"visual edits {clip_id}: reused validated observations")
                continue
            observations = []
            for pair, src_window, finished_window in zip(
                pairs, source_windows, finished_windows,
            ):
                measured = inspect(
                    src_window, finished_window, max_frames=max_frames,
                    max_time_delta=time_tolerance,
                )
                if measured.get("status") not in {
                    "SPARSE_VISUAL_CORRESPONDENCE", "UNKNOWN_VISUAL_MATCH",
                    "UNKNOWN_FRAME_BUDGET", "UNKNOWN_NO_FRAMES",
                }:
                    raise ValueError("Unexpected visual interpretation status")
                observations.append({
                    "group": pair["group"],
                    "source_measurement": pair["source_measurement"],
                    "finished_measurement": pair["finished_measurement"],
                    "requested_source_range": [pair["source_start"], pair["source_end"]],
                    "finished_range": [pair["clip_start"], pair["clip_end"]],
                    "absolute_seek_verified": False,
                    **measured,
                })
            records[clip_id] = {
                **base, "status": "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY",
                "fingerprint": identity, "method_version": VISUAL_VERSION,
                "pair_observations": observations,
                "policy_clip": clip_policy.model_dump(mode="json"),
                "policy_source": source_policy.model_dump(mode="json"),
                "policy_ranges": range_policy.model_dump(mode="json"),
                "unknowns": [
                    "Sampled visual geometry is not an exact dense OutputSourceTimeMap",
                    "No frame-accurate cuts or visual absence can be asserted",
                    "Independent original event/group and semantic gold verification pending",
                ],
            }
            save()
            if progress:
                progress(f"visual edits {clip_id}: OBSERVED_UNREVIEWED_VISUAL_GEOMETRY")
        except Exception as exc:
            records[clip_id] = {
                **base, "status": "RETRYABLE_FAILURE", "error_class": type(exc).__name__,
            }
            save()
            if progress:
                progress(f"visual edits {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for row in records.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "report_path": str(report_path), "attempted": attempted,
        "counts": counts, "qualification_allowed": False,
        "frame_accurate_cuts_verified": False, "reviewed_gold_available": False,
    }
