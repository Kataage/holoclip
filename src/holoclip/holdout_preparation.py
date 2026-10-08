"""Resumable acquisition of *unreviewed* holdout video measurements.

Never imports these clips to learning, sets held_out=True, or creates gold grades.
Policy, public status and unchanged source identity are checked before download.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.providers.cost import CostPolicyError
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import BenchmarkCase, atomic_json, verified_window
from holoclip.providers.source.base import AcquisitionProfile
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file, measure_window, probe_video


VERSION = "reserved-holdout-measurement-v1"


def _measured(record: dict) -> bool:
    """Cache reuse requires pinned bytes, not just a success flag or file name."""
    try:
        media = Path(record["media_path"])
        if not media.is_file() or checksum_file(media) != record["media_sha256"]:
            return False
        if not record["windows"]:
            return False
        for path in record["windows"]:
            verified_window(BenchmarkCase(
                id="cache-check", clip_id=record["clip_id"], task="video",
                source_url=record["clip_url"], teacher_reference="unreviewed",
                public_data=True, dimensions=frozenset(), measurement_path=path,
            ))
        return True
    except (OSError, ValueError, KeyError, TypeError, CostPolicyError, json.JSONDecodeError):
        return False


def _candidates(state: CorpusGrowthState):
    """Prefer original-stream diversity before second clips from the same source."""
    grouped: dict[str, list] = {}
    for item in state.items.values():
        if item.status == "holdout_reserved" and item.source_stream_id:
            grouped.setdefault(item.source_stream_id, []).append(item)
    selected = []
    for group in grouped.values():
        group.sort(key=lambda x: x.clip_url)
    while any(grouped.values()):
        for stream_id in sorted(grouped):
            if grouped[stream_id]:
                selected.append(grouped[stream_id].pop(0))
    return selected


def prepare_reserved_holdouts(
    provider,
    *, store: Path, state_path: Path, output: Path,
    policy_rules: list, max_clips: int = 3, window_seconds: float = 20,
    progress: Callable[[str], None] | None = None,
    measure: Callable = measure_window,
    probe: Callable = probe_video,
) -> dict:
    """Acquire & measure bounded finished-clip windows; never certify semantic gold.

    All measurement jobs are locally deterministic. No external AI/API requests.
    Rechecks source provenance, permissions, and train/holdout split *every run*.
    """
    if not 1 <= max_clips <= 20 or not 5 <= window_seconds <= 120:
        raise ValueError("Holdout preparation needs 1..20 clips and 5..120s windows")
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-preparation.json"
    state = (
        CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.exists() else CorpusGrowthState()
    )
    inventory = holdout_inventory(store, state_path)
    # Every preexisting example is learning history, including untrusted/review examples.
    training_streams = {x.stream_id for x in load_human_clip_store(store) if x.stream_id}
    training_clips = {human_clip_identity(x.clip_url) for x in load_human_clip_store(store)}
    prior = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records: dict[str, dict] = prior.get("records", {}) if prior.get("version") == VERSION else {}
    result = {
        "version": VERSION, "status": "UNREVIEWED_MEASUREMENTS",
        "qualification_allowed": False, "reviewed_gold_available": False,
        "benchmark_held_out": False, "event_split_verified": False,
        "reserved_streams": inventory["reserved_streams"],
        "conflicted_streams": inventory["conflicted_streams"],
        "records": records,
    }

    def save():
        atomic_json(report_path, result)

    attempted = 0
    # Eligible reservations only; previously failed/quarantined items can be rechecked
    # without editing the discovery queue or manually preparing clip URLs.
    for item in _candidates(state):
        if attempted >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        key = clip_id
        base = dict(clip_id=clip_id, clip_url=item.clip_url,
                    source_url=item.source_url, source_stream_id=item.source_stream_id)
        def blocked(status: str):
            records[key] = {**base, "status": status, "qualification_allowed": False}
            save()
            if progress:
                progress(f"holdout {clip_id}: {status}")

        # These checks deliberately precede probe, acquire and measurement.
        if (item.source_stream_id in training_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in training_clips):
            blocked("BLOCKED_SPLIT_CONFLICT")
            continue
        if item.source_stream_id not in state.holdout_stream_ids or not item.source_url:
            blocked("BLOCKED_NOT_RESERVED")
            continue
        attempted += 1
        try:
            clip = provider.probe(item.clip_url)
            source = provider.probe(item.source_url)
            if not _public(clip) or not _public(source):
                blocked("QUARANTINED_NOT_PUBLIC_ENDED")
                continue
            if (not clip.external_id or not source.external_id
                    or source.external_id != item.source_stream_id
                    or human_clip_identity(source.url) != human_clip_identity(item.source_url)):
                blocked("QUARANTINED_SOURCE_CHANGED")
                continue
            # Re-resolve rather than trust stale saved description hints.
            chosen = choose_source_candidate(provider.source_candidates(clip))
            if chosen is None or human_clip_identity(chosen.url) != human_clip_identity(item.source_url):
                blocked("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            cp = evaluate_source_metadata(clip, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            sp = evaluate_source_metadata(source, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            # A scoped restriction cannot be cleared without precise aligned ranges.
            if cp.decision is not PolicyDecision.ALLOW or sp.decision is not PolicyDecision.ALLOW:
                blocked("QUARANTINED_POLICY")
                continue
            old = records.get(key, {})
            if (old.get("status") == "PREPARED_UNREVIEWED"
                    and old.get("source_stream_id") == source.external_id
                    and old.get("source_url") == item.source_url
                    and _measured(old)):
                if progress:
                    progress(f"holdout {clip_id}: reused verified media")
                continue
            acquired = provider.acquire(
                item.clip_url, output / "acquired", profile=AcquisitionProfile.EDITING_VIDEO
            )
            if human_clip_identity(acquired.metadata.url) != clip_id:
                blocked("QUARANTINED_ACQUIRED_IDENTITY")
                continue
            media_probe = probe(acquired.media_path)
            streams = media_probe.get("streams", [])
            if not any(s.get("codec_type") == "video" for s in streams):
                blocked("QUARANTINED_NO_VIDEO")
                continue
            duration = float(media_probe["format"]["duration"])
            if not math.isfinite(duration) or duration < 5:
                blocked("QUARANTINED_INVALID_DURATION")
                continue
            seconds = min(duration, window_seconds)
            starts = sorted({
                0.0, max(0.0, duration / 2 - seconds / 2),
                max(0.0, duration - seconds),
            })
            windows = []
            for start in starts:
                end = min(duration, start + seconds)
                window = measure(
                    acquired.media_path, output / "media", asset_id=clip_id,
                    start=start, end=end, transcript={},
                    dense_times=[(end - start) / 2],
                )
                windows.append(str(Path(window.video_path).parent / "measurement.json"))
            # Provenance/measurement audit record is *not* BenchmarkManifest/Assessment.
            records[key] = {
                **base, "status": "PREPARED_UNREVIEWED",
                "media_path": str(acquired.media_path),
                "media_sha256": checksum_file(acquired.media_path),
                "windows": windows, "duration_seconds": duration,
                "window_seconds": window_seconds,
                "clip_metadata": {"external_id": clip.external_id,
                                  "channel_id": clip.channel_id},
                "source_metadata": {"external_id": source.external_id,
                                    "channel_id": source.channel_id},
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
                "qualification_allowed": False, "event_split_verified": False,
                "reviewed_gold_available": False,
            }
            save()
            if progress:
                progress(f"holdout {clip_id}: PREPARED_UNREVIEWED")
        except Exception as exc:
            # Avoid embedding provider stderr, tokens, or remote text into reports.
            records[key] = {
                **base, "status": "RETRYABLE_FAILURE",
                "error_class": type(exc).__name__, "qualification_allowed": False,
            }
            save()
            if progress:
                progress(f"holdout {clip_id}: RETRYABLE_FAILURE ({type(exc).__name__})")
    save()
    counts = {}
    for row in records.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "status": result["status"], "report_path": str(report_path),
        "attempted": attempted, "counts": counts, "qualification_allowed": False,
        "reviewed_gold_available": False, "event_split_verified": False,
    }
