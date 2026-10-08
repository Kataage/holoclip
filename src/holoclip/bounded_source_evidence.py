"""Resumable, policy-gated source/finished *visual evidence pairs* from audio anchors.

Requested source seek offsets are hypotheses, not frame-accurate truth. Neither
this artifact nor any recovered comparison is validated human gold.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata, evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _candidates, _measured, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import BenchmarkCase, atomic_json, verified_window
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file, measure_window, probe_video

VERSION = "reserved-bounded-source-evidence-v1"
AUDIO_VERSION = "audio-band-anchor-v1"
MAX_SECTION_BYTES = 256 * 1024 * 1024


def _valid_ranges(audio: dict, duration: float, *, max_ranges: int) -> list[dict]:
    """Reject altered/non-grounded alignment; no model may invent these ranges."""
    if (audio.get("status") != "PARTIAL_AUDIO_MATCH"
            or audio.get("fingerprint_version") != AUDIO_VERSION
            or not audio.get("anchors") or len(audio["anchors"]) < 3
            or not isinstance(audio.get("ranges"), list)):
        return []
    anchors = audio["anchors"]
    if any(not isinstance(a, dict) or not all(
        isinstance(a.get(k), (float, int)) and math.isfinite(a[k])
        for k in ("clip_start", "clip_end", "source_start", "source_end", "correlation",
                  "uniqueness_margin")
    ) or a["correlation"] < 0.79 or a["uniqueness_margin"] < 0.065
           for a in anchors):
        return []
    chosen = []
    last_end = -1.0
    for item in audio["ranges"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), (float, int)) and math.isfinite(item[k])
            for k in ("clip_start", "clip_end", "source_start", "source_end",
                      "min_correlation")
        ):
            return []
        if (not isinstance(item.get("anchors"), int) or item["anchors"] < 1
                or item["clip_start"] < last_end - 0.5
                or not 0 <= item["clip_start"] < item["clip_end"] <= duration + .05
                or not 0 <= item["source_start"] < item["source_end"]
                or abs((item["source_end"] - item["source_start"])
                       - (item["clip_end"] - item["clip_start"])) > 1.0
                or item["min_correlation"] < 0.79):
            return []
        # Range endpoints must be supported by actual stored acoustic anchors.
        group = [a for a in anchors if
                 item["clip_start"] - .5 <= a["clip_start"]
                 and a["clip_end"] <= item["clip_end"] + .5
                 and abs((a["source_start"] - a["clip_start"])
                         - (item["source_start"] - item["clip_start"])) <= .75]
        if len(group) < item["anchors"]:
            return []
        last_end = item["clip_end"]
        if len(chosen) < max_ranges:
            chosen.append(item)
    return chosen


def _checked_measurement(path: str) -> str:
    window = verified_window(BenchmarkCase(
        id="source-pair-verify", clip_id="unreviewed", task="video",
        source_url="https://invalid.local/unreviewed", teacher_reference="unreviewed",
        public_data=True, dimensions=frozenset(), measurement_path=path,
    ))
    return window.media_sha256


def _reusable(old: dict, audio: dict, *, clips_sha: str, source_sha: str,
              max_ranges: int, padding: float) -> bool:
    if (old.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
            or old.get("audio_source_sha256") != source_sha
            or old.get("finished_media_sha256") != clips_sha
            or old.get("audio_method") != AUDIO_VERSION
            or old.get("max_ranges") != max_ranges
            or old.get("padding_seconds") != padding
            or not old.get("pairs")):
        return False
    try:
        for pair in old["pairs"]:
            path = Path(pair["source_video_path"])
            if (not path.is_file() or path.stat().st_size > MAX_SECTION_BYTES
                    or checksum_file(path) != pair["source_video_sha256"]):
                return False
            if _checked_measurement(pair["source_measurement"]) != pair["source_window_sha256"]:
                return False
            if _checked_measurement(pair["finished_measurement"]) != pair["finished_window_sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def measure_reserved_source_pairs(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, output: Path, policy_rules: list, max_clips=3,
    max_ranges=3, padding_seconds=3.0, progress=None,
    probe=probe_video, measure=measure_window,
) -> dict:
    """No full original-video fallback. Fail closed on unknown source or edits."""
    if not 1 <= max_clips <= 20 or not 1 <= max_ranges <= 5:
        raise ValueError("Bounded job limits required")
    if not 2 <= padding_seconds <= 10:
        raise ValueError("Padding must remain 2..10 seconds")
    prepared = json.loads((prepared_root / "holdout-preparation.json").read_text(encoding="utf-8"))
    audio = json.loads((audio_root / "holdout-audio-alignment.json").read_text(encoding="utf-8"))
    if (prepared.get("version") != "reserved-holdout-measurement-v1"
            or audio.get("version") != "reserved-holdout-audio-alignment-v1"
            or any(report.get(key) is not False for report in (prepared, audio)
                   for key in ("qualification_allowed", "event_split_verified",
                               "reviewed_gold_available"))):
        raise ValueError("Expected original unreviewed prepared/audio evidence")
    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
    inventory = holdout_inventory(store, state_path)
    examples = load_human_clip_store(store)
    learning_streams = {x.stream_id for x in examples if x.stream_id}
    learning_clips = {human_clip_identity(x.clip_url) for x in examples}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-source-pairs.json"
    old_report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = old_report.get("records", {}) if old_report.get("version") == VERSION else {}
    result = {"version": VERSION, "status": "UNREVIEWED_PAIRED_MEASUREMENTS",
              "qualification_allowed": False, "reviewed_gold_available": False,
              "benchmark_held_out": False, "event_split_verified": False,
              "frame_accurate_cuts_verified": False, "records": records}

    def save():
        atomic_json(report_path, result)

    attempted = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_UNREVIEWED_SOURCE_PAIRS"},
    ):
        if attempted >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        base = {"clip_id": clip_id, "clip_url": item.clip_url,
                "source_url": item.source_url, "source_stream_id": item.source_stream_id,
                "qualification_allowed": False, "reviewed_gold_available": False,
                "event_split_verified": False, "frame_accurate_cuts_verified": False}

        def block(status):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"source pairs {clip_id}: {status}")

        # Before any probing/network action, recheck reservations and prepared
        # evidence and verify the exact media bytes on disk.
        if (item.source_stream_id in learning_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in learning_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if item.source_stream_id not in state.holdout_stream_ids or not item.source_url:
            block("BLOCKED_NOT_RESERVED")
            continue
        prepared_item = prepared.get("records", {}).get(clip_id)
        audio_item = audio.get("records", {}).get(clip_id)
        if (not prepared_item or prepared_item.get("status") != "PREPARED_UNREVIEWED"
                or prepared_item.get("source_url") != item.source_url
                or prepared_item.get("source_stream_id") != item.source_stream_id
                or not _measured(prepared_item)):
            block("BLOCKED_FINISHED_VIDEO_UNVERIFIED")
            continue
        if (not audio_item or audio_item.get("clip_url") != item.clip_url
                or audio_item.get("source_url") != item.source_url
                or audio_item.get("source_stream_id") != item.source_stream_id
                or audio_item.get("clip_media_sha256") != prepared_item["media_sha256"]
                or not audio_item.get("source_audio_path")
                or not Path(audio_item["source_audio_path"]).is_file()
                or checksum_file(Path(audio_item["source_audio_path"]))
                   != audio_item.get("source_media_sha256")):
            block("BLOCKED_SOURCE_AUDIO_UNVERIFIED")
            continue
        ranges = _valid_ranges(
            audio_item, float(prepared_item["duration_seconds"]), max_ranges=max_ranges
        )
        if not ranges:
            block("BLOCKED_UNVERIFIED_AUDIO_ANCHORS")
            continue
        attempted += 1
        try:
            clip = provider.probe(item.clip_url)
            source = provider.probe(item.source_url)
            if not _public(clip) or not _public(source):
                block("QUARANTINED_NOT_PUBLIC_ENDED")
                continue
            if (human_clip_identity(clip.url) != clip_id or not source.external_id
                    or source.external_id != item.source_stream_id
                    or human_clip_identity(source.url) != human_clip_identity(item.source_url)):
                block("QUARANTINED_SOURCE_CHANGED")
                continue
            resolved = choose_source_candidate(provider.source_candidates(clip))
            if resolved is None or human_clip_identity(resolved.url) != human_clip_identity(item.source_url):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            clip_policy = evaluate_source_metadata(
                clip, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            source_policy = evaluate_source_metadata(
                source, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            if (clip_policy.decision is not PolicyDecision.ALLOW
                    or source_policy.decision is not PolicyDecision.ALLOW):
                block("QUARANTINED_POLICY")
                continue

            # Padded source windows are *requested* absolute positions. Require
            # full policy approval for these windows, including range restrictions.
            plans = []
            for idx, segment in enumerate(ranges):
                start = max(0.0, segment["source_start"] - padding_seconds)
                end = segment["source_end"] + padding_seconds
                if source.duration is not None:
                    if segment["source_end"] > source.duration + .1:
                        plans = []
                        break
                    end = min(end, source.duration)
                start, end = round(start, 3), round(end, 3)
                if not 5 <= end - start <= 120:
                    plans = []
                    break
                clip_start = max(0.0, segment["clip_start"] - padding_seconds)
                clip_end = min(
                    float(prepared_item["duration_seconds"]),
                    segment["clip_end"] + padding_seconds,
                )
                if clip_end - clip_start < 5:
                    plans = []
                    break
                plans.append({"group": idx, "source_start": start, "source_end": end,
                              "clip_start": round(clip_start, 3),
                              "clip_end": round(clip_end, 3),
                              "matched_anchors": segment["anchors"],
                              "min_correlation": segment["min_correlation"]})
            if len(plans) != len(ranges):
                block("QUARANTINED_INVALID_SOURCE_WINDOWS")
                continue
            scoped_policy = evaluate_source_ranges(
                source,
                ranges=[(p["source_start"], p["source_end"]) for p in plans],
                rules=policy_rules, operation=PolicyOperation.LEARN,
                deferred_findings=source_policy.findings,
            )
            if scoped_policy.decision is not PolicyDecision.ALLOW:
                block("QUARANTINED_RANGE_POLICY")
                continue
            previous = records.get(clip_id, {})
            if (_reusable(
                previous, audio_item, clips_sha=prepared_item["media_sha256"],
                source_sha=audio_item["source_media_sha256"],
                max_ranges=max_ranges, padding=padding_seconds,
            ) and previous.get("source_stream_id") == source.external_id
                    and previous.get("source_url") == item.source_url
                    and previous.get("plans") == plans):
                if progress:
                    progress(f"source pairs {clip_id}: reused checked windows")
                continue

            pairs = []
            for plan in plans:
                # Provider contract must support section-only fetching. If it
                # cannot, fail here rather than downloading the entire archive.
                section = provider.acquire_video_range(
                    item.source_url, output / "original_sections",
                    start=plan["source_start"], end=plan["source_end"],
                )
                if (section.metadata.external_id != source.external_id
                        or human_clip_identity(section.metadata.url)
                           != human_clip_identity(item.source_url)
                        or abs(section.requested_start - plan["source_start"]) > .001
                        or abs(section.requested_end - plan["source_end"]) > .001):
                    raise ValueError("Range provider returned a mismatched original source")
                if section.media_path.stat().st_size > MAX_SECTION_BYTES:
                    raise ValueError("Bounded original source section exceeds size limit")
                measured = probe(section.media_path)
                if not any(s.get("codec_type") == "video" for s in measured["streams"]):
                    raise ValueError("Bounded original section contains no video")
                actual_seconds = float(measured["format"]["duration"])
                expected_seconds = plan["source_end"] - plan["source_start"]
                if (not math.isfinite(actual_seconds)
                        or not max(5., expected_seconds - 3.) <= actual_seconds <= expected_seconds + 3.):
                    raise ValueError("Section duration differs from bounded request")
                source_window = measure(
                    section.media_path, output / "measured_source",
                    asset_id=f"{clip_id}-source-{plan['group']}",
                    start=0., end=min(actual_seconds, 120.),
                    transcript={}, dense_times=[actual_seconds / 2],
                )
                finished_window = measure(
                    Path(prepared_item["media_path"]), output / "measured_finished",
                    asset_id=f"{clip_id}-finished-{plan['group']}",
                    start=plan["clip_start"], end=plan["clip_end"],
                    transcript={}, dense_times=[(plan["clip_end"] - plan["clip_start"]) / 2],
                )
                pairs.append({
                    **plan, "source_video_path": str(section.media_path),
                    "source_video_sha256": checksum_file(section.media_path),
                    "source_measured_duration_seconds": actual_seconds,
                    "source_measurement": str(Path(source_window.video_path).parent / "measurement.json"),
                    "source_window_sha256": source_window.media_sha256,
                    "finished_measurement": str(Path(finished_window.video_path).parent / "measurement.json"),
                    "finished_window_sha256": finished_window.media_sha256,
                    "absolute_seek_verified": False, "frame_accurate_cuts_verified": False,
                })
                # Atomic per-range progress allows already downloaded sections to
                # be reused by their exact requested-range directory on retry.
            records[clip_id] = {
                **base, "status": "MEASURED_UNREVIEWED_SOURCE_PAIRS",
                "pairs": pairs, "plans": plans, "audio_method": AUDIO_VERSION,
                "audio_source_sha256": audio_item["source_media_sha256"],
                "finished_media_sha256": prepared_item["media_sha256"],
                "max_ranges": max_ranges, "padding_seconds": padding_seconds,
                "policy_clip": clip_policy.model_dump(mode="json"),
                "policy_source": source_policy.model_dump(mode="json"),
                "policy_ranges": scoped_policy.model_dump(mode="json"),
                "limitations": [
                    "Requested source absolute seeks are not independently frame-accurate",
                    "No visual edit, text overlay or multi-POV semantic gold is inferred",
                ],
            }
            save()
            if progress:
                progress(f"source pairs {clip_id}: MEASURED_UNREVIEWED_SOURCE_PAIRS")
        except Exception as exc:
            # Logs and persisted states never contain raw secrets or yt-dlp stderr.
            records[clip_id] = {**base, "status": "RETRYABLE_FAILURE",
                                "error_class": type(exc).__name__}
            save()
            if progress:
                progress(f"source pairs {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for record in records.values():
        status = record["status"]
        counts[status] = counts.get(status, 0) + 1
    return {"report_path": str(report_path), "attempted": attempted,
            "counts": counts, "qualification_allowed": False,
            "reviewed_gold_available": False, "event_split_verified": False}
