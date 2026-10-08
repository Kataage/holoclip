"""Guarded original source-section timing evidence, never approved cut-time gold."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata, evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _candidates, _measured, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import atomic_json
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.source_section_clock import VERSION as CLOCK_VERSION, calibrate_section_clock
from holoclip.video_measurement import checksum_file
from holoclip.bounded_source_evidence import _checked_measurement
from holoclip.providers.cost import CostPolicyError

VERSION = "reserved-source-section-clock-v1"


def calibrate_reserved_section_clocks(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, output: Path, policy_rules: list,
    max_clips: int = 3, progress=None, calibrate=calibrate_section_clock,
) -> dict:
    """Conservatively link bounded section audio to the original source audio clock."""
    if not 1 <= max_clips <= 20:
        raise ValueError("Only bounded calibration batches allowed")
    specs = [
        (prepared_root / "holdout-preparation.json", "reserved-holdout-measurement-v1"),
        (audio_root / "holdout-audio-alignment.json", "reserved-holdout-audio-alignment-v1"),
        (pairs_root / "holdout-source-pairs.json", "reserved-bounded-source-evidence-v1"),
    ]
    reports = []
    for path, version in specs:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (raw.get("version") != version or any(raw.get(k) is not False for k in (
            "qualification_allowed", "event_split_verified", "reviewed_gold_available",
        ))):
            raise ValueError("Section clock requires unreviewed, unpromoted upstream evidence")
        reports.append(raw)
    prepared, audio, pairs = reports
    if pairs.get("frame_accurate_cuts_verified") is not False:
        raise ValueError("Source video pair is not certified for frame-level cut claims")
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    inventory = holdout_inventory(store, state_path)
    learning = load_human_clip_store(store)
    learning_streams = {e.stream_id for e in learning if e.stream_id}
    learning_clips = {human_clip_identity(e.clip_url) for e in learning}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-source-clock.json"
    old = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = old.get("records", {}) if old.get("version") == VERSION else {}
    report = {
        "version": VERSION, "status": "UNREVIEWED_SECTION_TIME_PROVENANCE",
        "qualification_allowed": False, "benchmark_held_out": False,
        "reviewed_gold_available": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False,
        "original_video_absolute_pts_verified": False,
        "records": records,
    }

    def save():
        atomic_json(report_path, report)

    attempts = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS"},
    ):
        if attempts >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        base = {
            "clip_id": clip_id, "clip_url": item.clip_url,
            "source_url": item.source_url, "source_stream_id": item.source_stream_id,
            "qualification_allowed": False, "reviewed_gold_available": False,
            "frame_accurate_cuts_verified": False, "event_split_verified": False,
        }

        def block(status: str):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"source clock {clip_id}: {status}")

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
        e = pairs.get("records", {}).get(clip_id, {})
        if (p.get("status") != "PREPARED_UNREVIEWED"
                or p.get("source_url") != item.source_url
                or p.get("source_stream_id") != item.source_stream_id
                or not _measured(p)):
            block("BLOCKED_FINISHED_VIDEO_UNVERIFIED")
            continue
        audio_path = a.get("source_audio_path")
        if (a.get("status") != "PARTIAL_AUDIO_MATCH"
                or a.get("source_url") != item.source_url
                or a.get("source_stream_id") != item.source_stream_id
                or a.get("clip_media_sha256") != p["media_sha256"]
                or a.get("fingerprint_version") != "audio-band-anchor-v1"
                or not audio_path or not Path(audio_path).is_file()
                or checksum_file(Path(audio_path)) != a.get("source_media_sha256")):
            block("BLOCKED_SOURCE_AUDIO_UNVERIFIED")
            continue
        sections = e.get("pairs", [])
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or e.get("source_url") != item.source_url
                or e.get("source_stream_id") != item.source_stream_id
                or e.get("audio_source_sha256") != a["source_media_sha256"]
                or e.get("finished_media_sha256") != p["media_sha256"]
                or e.get("audio_method") != "audio-band-anchor-v1"
                or not isinstance(sections, list) or not 1 <= len(sections) <= 5):
            block("BLOCKED_SOURCE_PAIRS_UNVERIFIED")
            continue
        try:
            source_hashes = []
            manifest_hashes = []
            for index, section in enumerate(sections):
                path = Path(section["source_video_path"])
                if (section.get("group") != index
                        or not path.is_file()
                        or not 0 < path.stat().st_size <= 256 * 1024 * 1024
                        or checksum_file(path) != section["source_video_sha256"]):
                    raise ValueError("Original section media integrity failure")
                source_hashes.append(section["source_video_sha256"])
                for label in ("source", "finished"):
                    measurement = section[f"{label}_measurement"]
                    if _checked_measurement(measurement) != section[f"{label}_window_sha256"]:
                        raise ValueError("Original/finished measurement changed")
                    manifest_hashes.append(checksum_file(Path(measurement)))
            identity = hashlib.sha256(json.dumps({
                "version": VERSION, "method": CLOCK_VERSION,
                "audio_sha": a["source_media_sha256"],
                "finished_sha": p["media_sha256"],
                "source_video_sha": source_hashes,
                "measurement_sha": manifest_hashes,
                "sections": [(x["source_start"], x["source_end"]) for x in sections],
            }, sort_keys=True).encode()).hexdigest()
        except (OSError, TypeError, KeyError, ValueError, CostPolicyError):
            block("BLOCKED_MEASUREMENT_INTEGRITY")
            continue
        attempts += 1
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
            if (candidate is None
                    or human_clip_identity(candidate.url) != human_clip_identity(item.source_url)):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            cp = evaluate_source_metadata(clip, rules=policy_rules, operation=PolicyOperation.LEARN)
            sp = evaluate_source_metadata(source, rules=policy_rules, operation=PolicyOperation.LEARN)
            rp = evaluate_source_ranges(
                source, ranges=[(x["source_start"], x["source_end"]) for x in sections],
                rules=policy_rules, operation=PolicyOperation.LEARN,
                deferred_findings=sp.findings,
            )
            if any(x.decision is not PolicyDecision.ALLOW for x in (cp, sp, rp)):
                block("QUARANTINED_POLICY")
                continue
            cached = records.get(clip_id, {})
            if (cached.get("status") == "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS"
                    and cached.get("fingerprint") == identity
                    and cached.get("source_stream_id") == source.external_id):
                if progress:
                    progress(f"source clock {clip_id}: reused checked section clocks")
                continue
            calibrated = []
            for section in sections:
                result = calibrate(
                    Path(section["source_video_path"]),
                    Path(audio_path), output / "fingerprints",
                )
                status = result.get("status")
                if status not in {
                    "PARTIAL_SECTION_AUDIO_CLOCK", "UNKNOWN_SHORT_OR_MISSING_AUDIO",
                    "UNKNOWN_NONUNIQUE_SOURCE_AUDIO", "UNKNOWN_INCONSISTENT_SOURCE_CLOCK",
                    "UNKNOWN_SECTION_NO_BOUNDED_AV", "UNKNOWN_AV_CLOCK_DISAGREEMENT",
                }:
                    raise ValueError("Unexpected section timestamp confidence")
                origin = result.get("origin_seconds")
                if origin is not None and abs(origin - section["source_start"]) > 5:
                    result = {**result, "status": "UNKNOWN_REQUESTED_SEEK_DISAGREEMENT",
                              "origin_seconds": None,
                              "original_video_absolute_pts_verified": False}
                observed = result.get("approx_original_video_range")
                if observed is not None and result["status"] == "PARTIAL_SECTION_AUDIO_CLOCK":
                    observed_ranges = [(
                        max(0., observed[0] - 3),
                        observed[1] + 3,
                    )]
                    policy = evaluate_source_ranges(
                        source, ranges=observed_ranges, rules=policy_rules,
                        operation=PolicyOperation.LEARN, deferred_findings=sp.findings,
                    )
                    if policy.decision is not PolicyDecision.ALLOW:
                        raise ValueError("Calibrated source range fails original clip policy")
                calibrated.append({
                    "group": section["group"],
                    "requested_source_range": [
                        section["source_start"], section["source_end"],
                    ],
                    "video_sha256": section["source_video_sha256"],
                    "source_measurement": section["source_measurement"],
                    "absolute_original_frame_pts_verified": False,
                    **result,
                })
            records[clip_id] = {
                **base, "status": "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS",
                "fingerprint": identity, "method": CLOCK_VERSION,
                "section_clocks": calibrated, "audio_source_sha256": a["source_media_sha256"],
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
                "policy_ranges": rp.model_dump(mode="json"),
                "limitations": [
                    "Original section native frame PTS are not original livestream absolute PTS",
                    "Audio segment correspondence cannot certify original video AV sync",
                    "No dense source-to-finished frame map or exact editing cut is produced",
                ],
            }
            save()
            if progress:
                progress(f"source clock {clip_id}: MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS")
        except Exception as exc:
            records[clip_id] = {**base, "status": "RETRYABLE_FAILURE",
                                "error_class": type(exc).__name__}
            save()
            if progress:
                progress(f"source clock {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for record in records.values():
        status = record["status"]
        counts[status] = counts.get(status, 0) + 1
    return {"report_path": str(report_path), "attempted": attempts,
            "counts": counts, "qualification_allowed": False,
            "original_video_absolute_pts_verified": False}
