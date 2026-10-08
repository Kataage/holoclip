"""Unattended acoustic cut-interval diagnostics on protected source/finished data.

This stage never produces frame-accurate cuts, approved labels, or production gold.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.acoustic_cut_brackets import (
    VERSION as BRACKET_VERSION, refine_acoustic_cut_brackets,
)
from holoclip.audio_anchor_alignment import (
    VERSION as AUDIO_VERSION, fingerprint_media,
)
from holoclip.bounded_source_evidence import _valid_ranges, _checked_measurement
from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata, evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _candidates, _measured, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import atomic_json
from holoclip.providers.cost import CostPolicyError
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file

VERSION = "reserved-acoustic-cut-brackets-v1"


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def refine_reserved_cut_candidates(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, visual_root: Path, output: Path,
    policy_rules: list, max_clips: int = 3, max_points: int = 480,
    progress=None, fingerprint=fingerprint_media, refine=refine_acoustic_cut_brackets,
) -> dict:
    """Revalidate actual input bytes, current policies and source split each run."""
    if not 1 <= max_clips <= 20 or not 10 <= max_points <= 1000:
        raise ValueError("Invalid bounded acoustic refinement configuration")
    upstream = []
    for path, version in [
        (prepared_root / "holdout-preparation.json", "reserved-holdout-measurement-v1"),
        (audio_root / "holdout-audio-alignment.json", "reserved-holdout-audio-alignment-v1"),
        (pairs_root / "holdout-source-pairs.json", "reserved-bounded-source-evidence-v1"),
        (visual_root / "holdout-visual-observations.json", "reserved-visual-observations-v1"),
    ]:
        report = json.loads(path.read_text(encoding="utf-8"))
        if (report.get("version") != version
                or any(report.get(k) is not False for k in [
                    "qualification_allowed", "reviewed_gold_available",
                    "event_split_verified",
                ])):
            raise ValueError("Cut refinement accepts unreviewed evidence only")
        upstream.append(report)
    prepared, audio, pairs_data, visual_data = upstream
    if (pairs_data.get("frame_accurate_cuts_verified") is not False
            or visual_data.get("frame_accurate_cuts_verified") is not False):
        raise ValueError("No verified dense original cut map exists")
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    inv = holdout_inventory(store, state_path)
    training = load_human_clip_store(store)
    train_streams = {x.stream_id for x in training if x.stream_id}
    train_clips = {human_clip_identity(x.clip_url) for x in training}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-cut-brackets.json"
    old = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = old.get("records", {}) if old.get("version") == VERSION else {}
    report = {
        "version": VERSION, "status": "UNREVIEWED_CUT_CANDIDATES",
        "qualification_allowed": False, "benchmark_held_out": False,
        "frame_accurate_cuts_verified": False, "event_split_verified": False,
        "reviewed_gold_available": False, "records": records,
    }

    def save():
        atomic_json(report_path, report)

    attempted = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_UNREVIEWED_CUT_BRACKETS"},
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

        def block(status):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"cut refinement {clip_id}: {status}")

        if (item.source_stream_id in train_streams
                or item.source_stream_id in inv["conflicted_streams"]
                or clip_id in train_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if item.source_stream_id not in state.holdout_stream_ids or not item.source_url:
            block("BLOCKED_NOT_RESERVED")
            continue
        p = prepared.get("records", {}).get(clip_id, {})
        a = audio.get("records", {}).get(clip_id, {})
        e = pairs_data.get("records", {}).get(clip_id, {})
        v = visual_data.get("records", {}).get(clip_id, {})
        if (p.get("status") != "PREPARED_UNREVIEWED"
                or p.get("source_url") != item.source_url
                or p.get("source_stream_id") != item.source_stream_id
                or not _measured(p)):
            block("BLOCKED_FINISHED_VIDEO_UNVERIFIED")
            continue
        source_path = a.get("source_audio_path")
        if (a.get("status") != "PARTIAL_AUDIO_MATCH"
                or a.get("source_stream_id") != item.source_stream_id
                or a.get("source_url") != item.source_url
                or a.get("clip_media_sha256") != p["media_sha256"]
                or a.get("fingerprint_version") != AUDIO_VERSION
                or not source_path or not Path(source_path).is_file()
                or checksum_file(Path(source_path)) != a.get("source_media_sha256")):
            block("BLOCKED_AUDIO_UNVERIFIED")
            continue
        ranges = _valid_ranges(a, float(p["duration_seconds"]), max_ranges=5)
        if not ranges:
            block("BLOCKED_AUDIO_ANCHORS_UNVERIFIED")
            continue
        source_pairs = e.get("pairs", [])
        visual_pairs = v.get("pair_observations", [])
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or e.get("source_stream_id") != item.source_stream_id
                or e.get("source_url") != item.source_url
                or e.get("audio_source_sha256") != a["source_media_sha256"]
                or e.get("finished_media_sha256") != p["media_sha256"]
                or v.get("status") != "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY"
                or v.get("source_stream_id") != item.source_stream_id
                or v.get("source_url") != item.source_url
                or not isinstance(source_pairs, list) or not source_pairs
                or len(visual_pairs) != len(source_pairs)
                or v.get("method_version") != "visual-orb-affine-v1"):
            block("BLOCKED_VISUAL_EVIDENCE_UNVERIFIED")
            continue
        # Only acoustic ranges actually covered by the bounded original-video
        # pairs may enter this stage; an extra unmatched source stream segment
        # cannot gain a visual-evidence provenance by association.
        paired_ranges = ranges[:len(source_pairs)]
        if len(paired_ranges) != len(source_pairs) or any(
            pair.get("group") != index
            or pair.get("source_start", 0) > span["source_start"] + .05
            or pair.get("source_end", 0) < span["source_end"] - .05
            or pair.get("clip_start", 0) > span["clip_start"] + .05
            or pair.get("clip_end", 0) < span["clip_end"] - .05
            for index, (pair, span) in enumerate(zip(source_pairs, paired_ranges))
        ):
            block("BLOCKED_AUDIO_VISUAL_RANGE_MISMATCH")
            continue
        try:
            # All source video, frame and sheet evidence must be intact BEFORE
            # metadata probing. Previous image comparisons are only diagnostics,
            # never a proof of source-time alignment.
            visual_counts = []
            evidence_hashes = []
            for pair, visual in zip(source_pairs, visual_pairs):
                video_path = Path(pair["source_video_path"])
                if (not video_path.is_file()
                        or video_path.stat().st_size > 256 * 1024 * 1024
                        or checksum_file(video_path) != pair["source_video_sha256"]):
                    raise ValueError("Corrupt original video section")
                for label in ("source", "finished"):
                    if (_checked_measurement(pair[f"{label}_measurement"])
                            != pair[f"{label}_window_sha256"]):
                        raise ValueError("Corrupt measured frame data")
                    evidence_hashes.append(
                        checksum_file(Path(pair[f"{label}_measurement"]))
                    )
                if (visual.get("group") != pair["group"]
                        or visual.get("source_measurement") != pair["source_measurement"]
                        or visual.get("finished_measurement") != pair["finished_measurement"]
                        or visual.get("absolute_seek_verified") is not False):
                    raise ValueError("Mismatched visual and original source data")
                visual_counts.append({
                    "group": pair["group"],
                    "visual_correspondences": len(visual.get("observations", [])),
                    "visual_status": visual["status"],
                    "cut_boundary_visual_confirmation": False,
                })
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
            chosen = choose_source_candidate(provider.source_candidates(clip))
            if chosen is None or human_clip_identity(chosen.url) != human_clip_identity(item.source_url):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            cp = evaluate_source_metadata(clip, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            sp = evaluate_source_metadata(source, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            rp = evaluate_source_ranges(
                source,
                ranges=[(s["source_start"], s["source_end"]) for s in source_pairs],
                rules=policy_rules, operation=PolicyOperation.LEARN,
                deferred_findings=sp.findings,
            )
            if any(x.decision is not PolicyDecision.ALLOW for x in (cp, sp, rp)):
                block("QUARANTINED_POLICY")
                continue
            identity = _digest({
                "clip_sha256": p["media_sha256"],
                "audio_sha256": a["source_media_sha256"],
                "coarse_anchors": a.get("anchors", []), "ranges": paired_ranges,
                "source_pair_sha256": [x["source_video_sha256"] for x in source_pairs],
                "frame_manifest_sha256": evidence_hashes,
                "visual_observations": visual_pairs,
                "max_points": max_points, "method": BRACKET_VERSION,
                "audio_version": AUDIO_VERSION,
            })
            cached = records.get(clip_id, {})
            if (cached.get("status") == "MEASURED_UNREVIEWED_CUT_BRACKETS"
                    and cached.get("fingerprint") == identity
                    and cached.get("source_stream_id") == source.external_id):
                if progress:
                    progress(f"cut refinement {clip_id}: reused verified mapping")
                continue
            clip_features = fingerprint(Path(p["media_path"]), output / "fingerprints")
            original_features = fingerprint(Path(source_path), output / "fingerprints")
            refined = refine(
                clip_features, original_features, paired_ranges, max_points=max_points,
            )
            if refined.get("status") not in {
                "AUDIO_SUPPORTED_INTERVALS", "UNKNOWN_NO_UNIQUE_POINTS",
                "UNKNOWN_SAMPLING_BUDGET", "UNKNOWN_NO_SUPPORTED_RANGES",
            }:
                raise ValueError("Unexpected acoustic cut refinement status")
            records[clip_id] = {
                **base, "status": "MEASURED_UNREVIEWED_CUT_BRACKETS",
                "fingerprint": identity, "method": BRACKET_VERSION,
                "audio_source_sha256": a["source_media_sha256"],
                "finished_media_sha256": p["media_sha256"],
                "refinement": refined, "visual_pair_summary": visual_counts,
                "visual_cut_boundary_confirmed": False,
                "source_absolute_seek_verified": False,
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
                "policy_ranges": rp.model_dump(mode="json"),
            }
            save()
            if progress:
                progress(f"cut refinement {clip_id}: MEASURED_UNREVIEWED_CUT_BRACKETS")
        except Exception as exc:
            records[clip_id] = {**base, "status": "RETRYABLE_FAILURE",
                                "error_class": type(exc).__name__}
            save()
            if progress:
                progress(f"cut refinement {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for row in records.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "report_path": str(report_path), "attempted": attempted, "counts": counts,
        "qualification_allowed": False, "frame_accurate_cuts_verified": False,
        "event_split_verified": False, "reviewed_gold_available": False,
    }
