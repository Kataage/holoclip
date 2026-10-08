"""Guarded original-vs-finished sampled frame links, not authoritative cut gold."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.bounded_source_evidence import _checked_measurement, _valid_ranges
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
from holoclip.source_frame_links import VERSION as LINKS_VERSION, link_measured_frames
from holoclip.video_measurement import checksum_file

VERSION = "reserved-grounded-frame-links-v1"
CLOCK_VERSION = "reserved-source-section-clock-v1"
CLOCK_METHOD = "bounded-source-audio-clock-v1"


def _sha_json(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _window(path: str, clip_id: str, url: str):
    return verified_window(BenchmarkCase(
        id="frame-link-input", clip_id=clip_id, task="video", source_url=url,
        teacher_reference="unreviewed", public_data=True,
        dimensions=frozenset(), measurement_path=path,
    ))


def correlate_reserved_source_frames(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, clocks_root: Path, output: Path,
    policy_rules: list, max_clips: int = 3, max_frames: int = 64,
    progress=None, correlate=link_measured_frames,
) -> dict:
    """No network media downloads, model-generated gold or auto-promotion."""
    if not 1 <= max_clips <= 20 or not 1 <= max_frames <= 64:
        raise ValueError("Invalid bounded frame link budget")
    specs = [
        (prepared_root / "holdout-preparation.json", "reserved-holdout-measurement-v1"),
        (audio_root / "holdout-audio-alignment.json", "reserved-holdout-audio-alignment-v1"),
        (pairs_root / "holdout-source-pairs.json", "reserved-bounded-source-evidence-v1"),
        (clocks_root / "holdout-source-clock.json", CLOCK_VERSION),
    ]
    inputs = []
    for path, version in specs:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (raw.get("version") != version
                or any(raw.get(k) is not False for k in (
                    "qualification_allowed", "event_split_verified",
                    "reviewed_gold_available",
                ))):
            raise ValueError("Frame links require unpromoted, unreviewed media provenance")
        inputs.append(raw)
    prepared, audio, pairs, clocks = inputs
    if (pairs.get("frame_accurate_cuts_verified") is not False
            or clocks.get("frame_accurate_cuts_verified") is not False
            or clocks.get("original_video_absolute_pts_verified") is not False):
        raise ValueError("No verified original absolute frame clock is authorized")
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    inventory = holdout_inventory(store, state_path)
    training = load_human_clip_store(store)
    trained_streams = {e.stream_id for e in training if e.stream_id}
    trained_clips = {human_clip_identity(e.clip_url) for e in training}
    output.mkdir(parents=True, exist_ok=True)
    result_file = output / "holdout-frame-links.json"
    old = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
    records = old.get("records", {}) if old.get("version") == VERSION else {}
    result = {
        "version": VERSION, "status": "UNREVIEWED_SAMPLED_FRAME_LINKS",
        "qualification_allowed": False, "benchmark_held_out": False,
        "reviewed_gold_available": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False, "original_video_absolute_pts_verified": False,
        "dense_output_source_map_verified": False, "records": records,
    }

    def save():
        atomic_json(result_file, result)

    attempts = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_UNREVIEWED_FRAME_LINKS"},
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
                progress(f"frame links {clip_id}: {status}")

        if (item.source_stream_id in trained_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in trained_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if not item.source_url or item.source_stream_id not in state.holdout_stream_ids:
            block("BLOCKED_NOT_RESERVED")
            continue
        p = prepared.get("records", {}).get(clip_id, {})
        a = audio.get("records", {}).get(clip_id, {})
        e = pairs.get("records", {}).get(clip_id, {})
        c = clocks.get("records", {}).get(clip_id, {})
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
        ranges = _valid_ranges(a, float(p["duration_seconds"]), max_ranges=5)
        sections = e.get("pairs", [])
        section_clocks = c.get("section_clocks", [])
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or e.get("source_url") != item.source_url
                or e.get("source_stream_id") != item.source_stream_id
                or e.get("finished_media_sha256") != p["media_sha256"]
                or e.get("audio_source_sha256") != a["source_media_sha256"]
                or c.get("status") != "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS"
                or c.get("method") != CLOCK_METHOD
                or c.get("source_url") != item.source_url
                or c.get("source_stream_id") != item.source_stream_id
                or c.get("audio_source_sha256") != a["source_media_sha256"]
                or not isinstance(sections, list) or not sections
                or len(sections) > 5 or len(sections) != len(section_clocks)
                or len(ranges) < len(sections)):
            block("BLOCKED_UPSTREAM_PROVENANCE")
            continue
        try:
            source_shas, measurement_shas = [], []
            verified = []
            for i, (section, clock, span) in enumerate(zip(sections, section_clocks, ranges)):
                media = Path(section["source_video_path"])
                if (section.get("group") != i
                        or clock.get("group") != i
                        or not media.is_file()
                        or not 0 < media.stat().st_size <= 256 * 1024 * 1024
                        or checksum_file(media) != section["source_video_sha256"]
                        or clock.get("video_sha256") != section["source_video_sha256"]
                        or clock.get("source_measurement") != section["source_measurement"]
                        or clock.get("requested_source_range") != [
                            section["source_start"], section["source_end"],
                        ]
                        or clock.get("absolute_original_frame_pts_verified") is not False
                        or section["source_start"] > span["source_start"] + .05
                        or section["source_end"] < span["source_end"] - .05
                        or section["clip_start"] > span["clip_start"] + .05
                        or section["clip_end"] < span["clip_end"] - .05):
                    raise ValueError("Source section cannot inherit trusted frame coverage")
                source_shas.append(section["source_video_sha256"])
                for label in ("source", "finished"):
                    manifest = section[f"{label}_measurement"]
                    if _checked_measurement(manifest) != section[f"{label}_window_sha256"]:
                        raise ValueError("Measured visual source/finished frames altered")
                    measurement_shas.append(checksum_file(Path(manifest)))
                s_window = _window(section["source_measurement"], clip_id, item.source_url)
                f_window = _window(section["finished_measurement"], clip_id, item.clip_url)
                if (abs(s_window.source_start) > .05
                        or abs(f_window.source_start - section["clip_start"]) > .05
                        or abs(f_window.source_end - section["clip_end"]) > .05):
                    raise ValueError("Measured window starts are inconsistent")
                origin = clock.get("origin_seconds")
                if clock.get("status") == "PARTIAL_SECTION_AUDIO_CLOCK":
                    if (not isinstance(origin, (int, float)) or not math.isfinite(origin)
                            or abs(origin - section["source_start"]) > 5
                            or clock.get("frame_pts_verified") is not True
                            or clock.get("original_video_absolute_pts_verified") is not False):
                        raise ValueError("Unreliable section clock cannot be used")
                verified.append((section, clock, span, s_window, f_window))
            fingerprint = _sha_json({
                "version": CLOCK_VERSION, "method": CLOCK_METHOD,
                "audio_sha": a["source_media_sha256"],
                "finished_sha": p["media_sha256"],
                "source_video_sha": source_shas,
                "measurement_sha": measurement_shas,
                "sections": [(x["source_start"], x["source_end"]) for x in sections],
            })
            if c.get("fingerprint") != fingerprint:
                raise ValueError("Calibrated clock input provenance changed")
        except (OSError, ValueError, KeyError, TypeError, CostPolicyError):
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
            intervals = [(x["source_start"], x["source_end"]) for x in sections]
            for _, clock, _, _, _ in verified:
                observed = clock.get("approx_original_video_range")
                if observed is not None and clock.get("status") == "PARTIAL_SECTION_AUDIO_CLOCK":
                    if (not isinstance(observed, list) or len(observed) != 2
                            or not all(isinstance(t, (float, int)) and math.isfinite(t) for t in observed)
                            or not 0 <= observed[0] < observed[1]):
                        raise ValueError("Corrupt observed source clock range")
                    intervals.append((max(0., observed[0] - 3), observed[1] + 3))
            rp = evaluate_source_ranges(
                source, ranges=intervals, rules=policy_rules,
                operation=PolicyOperation.LEARN, deferred_findings=sp.findings,
            )
            if any(x.decision is not PolicyDecision.ALLOW for x in (cp, sp, rp)):
                block("QUARANTINED_POLICY")
                continue
            identity = _sha_json({
                "version": VERSION, "method": LINKS_VERSION,
                "clock": c, "source_pair": e, "audio_ranges": ranges,
                "source_manifests": measurement_shas, "max_frames": max_frames,
                "training_identity": clip_id,
            })
            cached = records.get(clip_id, {})
            if (cached.get("status") == "MEASURED_UNREVIEWED_FRAME_LINKS"
                    and cached.get("fingerprint") == identity):
                if progress:
                    progress(f"frame links {clip_id}: reused hash-verified correspondences")
                continue
            observed_links = []
            for section, clock, span, src, dst in verified:
                result_for_group = correlate(
                    src, dst, clock, audio_offset_seconds=(
                        span["source_start"] - span["clip_start"]
                    ), max_frames=max_frames,
                )
                if result_for_group.get("status") not in {
                    "PARTIAL_AUDIO_VISUAL_FRAME_LINKS", "UNKNOWN_CLOCK",
                    "UNKNOWN_MISSING_FRAMES", "UNKNOWN_FRAME_PAIR_BUDGET",
                    "UNKNOWN_TOO_SPARSE", "UNKNOWN_NO_COMPATIBLE_VISUAL_LINKS",
                    "UNKNOWN_TEMPORAL_CONTRADICTION",
                }:
                    raise ValueError("Unexpected unreviewed frame-link diagnostic")
                observed_links.append({
                    "group": section["group"],
                    "requested_source_range": [
                        section["source_start"], section["source_end"],
                    ],
                    "finished_range": [section["clip_start"], section["clip_end"]],
                    "original_absolute_frame_pts_verified": False,
                    **result_for_group,
                })
            records[clip_id] = {
                **base, "status": "MEASURED_UNREVIEWED_FRAME_LINKS",
                "fingerprint": identity, "method": LINKS_VERSION,
                "group_observations": observed_links,
                "audio_source_sha256": a["source_media_sha256"],
                "finished_media_sha256": p["media_sha256"],
                "source_clock_fingerprint": c["fingerprint"],
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
                "policy_ranges": rp.model_dump(mode="json"),
                "limitations": [
                    "Links are sampled reencoded window frames, not dense original frame PTS",
                    "Two adjacent matching frames cannot prove cuts or editor causality",
                    "A/V offset and original-source native PTS still require independent verification",
                ],
            }
            save()
            if progress:
                progress(f"frame links {clip_id}: MEASURED_UNREVIEWED_FRAME_LINKS")
        except Exception as exc:
            records[clip_id] = {
                **base, "status": "RETRYABLE_FAILURE", "error_class": type(exc).__name__,
            }
            save()
            if progress:
                progress(f"frame links {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for entry in records.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {
        "report_path": str(result_file), "attempted": attempts, "counts": counts,
        "qualification_allowed": False, "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False, "reviewed_gold_available": False,
    }
