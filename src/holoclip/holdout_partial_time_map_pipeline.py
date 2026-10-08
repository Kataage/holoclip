"""Unattended, fail-closed partial time-map diagnostics on reserved clips."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.acoustic_cut_brackets import VERSION as REFINEMENT_VERSION
from holoclip.bounded_source_evidence import _checked_measurement, _valid_ranges
from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata, evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _candidates, _measured, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.partial_time_map import VERSION as MAP_VERSION, construct_partial_time_map
from holoclip.provider_benchmark import atomic_json
from holoclip.providers.cost import CostPolicyError
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file
from holoclip.visual_source_crosscheck import VERSION as SOURCE_VISUAL_VERSION
from holoclip.visual_only_frame_search import VERSION as VISUAL_SEARCH_VERSION

VERSION = "reserved-partial-output-source-map-v1"


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def build_reserved_partial_maps(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, clocks_root: Path,
    frame_links_root: Path, cuts_root: Path, cut_sides_root: Path,
    output: Path, policy_rules: list, max_clips: int = 3,
    progress=None, build=construct_partial_time_map,
) -> dict:
    """Never interpolate into unknown media or mutate a learned editorial profile."""
    if not 1 <= max_clips <= 20:
        raise ValueError("Invalid bounded batch size")
    specs = [
        (prepared_root / "holdout-preparation.json", "reserved-holdout-measurement-v1"),
        (audio_root / "holdout-audio-alignment.json", "reserved-holdout-audio-alignment-v1"),
        (pairs_root / "holdout-source-pairs.json", "reserved-bounded-source-evidence-v1"),
        (clocks_root / "holdout-source-clock.json", "reserved-source-section-clock-v1"),
        (frame_links_root / "holdout-frame-links.json", "reserved-grounded-frame-links-v1"),
        (cuts_root / "holdout-cut-brackets.json", "reserved-acoustic-cut-brackets-v1"),
        (cut_sides_root / "holdout-cut-side-evidence.json",
         "reserved-multimodal-cut-side-evidence-v1"),
    ]
    sources = []
    for path, version in specs:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (raw.get("version") != version
                or any(raw.get(flag) is not False for flag in (
                    "qualification_allowed", "reviewed_gold_available",
                    "event_split_verified", "benchmark_held_out",
                ))):
            raise ValueError("Only unpromoted, unreviewed evidence may become a candidate map")
        sources.append(raw)
    prep, audio, paired, clocks, links, cuts, sides = sources
    for upstream in (paired, clocks, links, cuts, sides):
        if upstream.get("frame_accurate_cuts_verified") is not False:
            raise ValueError("Frame-accurate cut evidence is not established")
    for upstream in (clocks, links, sides):
        if upstream.get("original_video_absolute_pts_verified") is not False:
            raise ValueError("Global source native video time remains unverified")
    for upstream in (links, sides):
        if upstream.get("dense_output_source_map_verified") is not False:
            raise ValueError("No dense output/source map may be promoted")

    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
    inventory = holdout_inventory(store, state_path)
    training = load_human_clip_store(store)
    trained_streams = {e.stream_id for e in training if e.stream_id}
    trained_clips = {human_clip_identity(e.clip_url) for e in training}

    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-partial-time-map.json"
    cached = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = cached.get("records", {}) if cached.get("version") == VERSION else {}
    report = {
        "version": VERSION, "status": "UNREVIEWED_PARTIAL_TIME_MAP_CANDIDATES",
        "qualification_allowed": False, "benchmark_held_out": False,
        "reviewed_gold_available": False, "event_split_verified": False,
        "frame_accurate_cuts_verified": False,
        "original_video_absolute_pts_verified": False,
        "dense_output_source_map_verified": False,
        "records": records,
    }

    def save():
        atomic_json(report_path, report)

    attempted = 0
    for candidate in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_UNREVIEWED_PARTIAL_MAP"},
    ):
        if attempted >= max_clips:
            break
        clip_id = human_clip_identity(candidate.clip_url)
        base = {
            "clip_id": clip_id, "clip_url": candidate.clip_url,
            "source_url": candidate.source_url, "source_stream_id": candidate.source_stream_id,
            "qualification_allowed": False, "reviewed_gold_available": False,
            "frame_accurate_cuts_verified": False, "event_split_verified": False,
        }

        def block(status: str):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"partial map {clip_id}: {status}")

        if (candidate.source_stream_id in trained_streams
                or candidate.source_stream_id in inventory["conflicted_streams"]
                or clip_id in trained_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if (not candidate.source_url
                or candidate.source_stream_id not in state.holdout_stream_ids):
            block("BLOCKED_NOT_RESERVED")
            continue
        p, a, e, clock, linked, cut, side = (
            source.get("records", {}).get(clip_id, {}) for source in sources
        )
        if (p.get("status") != "PREPARED_UNREVIEWED"
                or p.get("source_url") != candidate.source_url
                or p.get("source_stream_id") != candidate.source_stream_id
                or not _measured(p)):
            block("BLOCKED_FINISHED_VIDEO_UNVERIFIED")
            continue
        audio_path = a.get("source_audio_path")
        if (a.get("status") != "PARTIAL_AUDIO_MATCH"
                or a.get("source_url") != candidate.source_url
                or a.get("source_stream_id") != candidate.source_stream_id
                or a.get("clip_media_sha256") != p["media_sha256"]
                or not audio_path or not Path(audio_path).is_file()
                or checksum_file(Path(audio_path)) != a.get("source_media_sha256")):
            block("BLOCKED_AUDIO_UNVERIFIED")
            continue
        sections = e.get("pairs", [])
        section_clocks = clock.get("section_clocks", [])
        group_links = linked.get("group_observations", [])
        section_shas = side.get("source_section_sha256", [])
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or clock.get("status") != "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS"
                or linked.get("status") != "MEASURED_UNREVIEWED_FRAME_LINKS"
                or cut.get("status") != "MEASURED_UNREVIEWED_CUT_BRACKETS"
                or cut.get("method") != REFINEMENT_VERSION
                or not isinstance(cut.get("refinement"), dict)
                or cut["refinement"].get("version") != REFINEMENT_VERSION
                or side.get("status") != "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE"
                or side.get("source_visual_method_version") != SOURCE_VISUAL_VERSION
                or side.get("visual_only_frame_search_version") != VISUAL_SEARCH_VERSION
                or not isinstance(sections, list) or not 1 <= len(sections) <= 5
                or not (len(sections) == len(section_clocks)
                        == len(group_links) == len(section_shas))
                or any(row.get("source_url") != candidate.source_url
                       or row.get("source_stream_id") != candidate.source_stream_id
                       for row in (e, clock, linked, cut, side))
                or any(row.get("audio_source_sha256") != a["source_media_sha256"]
                       for row in (e, clock, linked, cut, side))
                or any(row.get("finished_media_sha256") != p["media_sha256"]
                       for row in (e, linked, cut, side))
                or linked.get("source_clock_fingerprint") != clock.get("fingerprint")
                or side.get("source_clock_fingerprint") != clock.get("fingerprint")):
            block("BLOCKED_UPSTREAM_PROVENANCE")
            continue
        try:
            spans = _valid_ranges(a, float(p["duration_seconds"]), max_ranges=5)
            if not spans or len(spans) < len(sections):
                raise ValueError("No uniquely aligned audio source range")
            source_hashes, manifest_hashes = [], []
            for i, (section, proof, obs, digest) in enumerate(
                zip(sections, section_clocks, group_links, section_shas)
            ):
                path = Path(section["source_video_path"])
                if (section.get("group") != i or proof.get("group") != i
                        or obs.get("group") != i
                        or not path.is_file()
                        or not 0 < path.stat().st_size <= 256 * 1024 * 1024
                        or checksum_file(path) != digest
                        or digest != section["source_video_sha256"]
                        or proof.get("video_sha256") != digest
                        or proof.get("source_measurement") != section["source_measurement"]
                        or proof.get("requested_source_range") != [
                            section["source_start"], section["source_end"],
                        ]):
                    raise ValueError("Original section and clock provenance mismatch")
                source_hashes.append(digest)
                for label in ("source", "finished"):
                    manifest = section[f"{label}_measurement"]
                    if _checked_measurement(manifest) != section[f"{label}_window_sha256"]:
                        raise ValueError("Measured source/finished frames changed")
                    manifest_hashes.append(checksum_file(Path(manifest)))
            section_clock_identity = _digest({
                "version": "reserved-source-section-clock-v1",
                "method": "bounded-source-audio-clock-v1",
                "audio_sha": a["source_media_sha256"],
                "finished_sha": p["media_sha256"],
                "source_video_sha": source_hashes, "measurement_sha": manifest_hashes,
                "sections": [(s["source_start"], s["source_end"]) for s in sections],
            })
            if section_clock_identity != clock.get("fingerprint"):
                raise ValueError("Section clock calibrated against different source bytes")
            side_identity = _digest({
                "version": "reserved-multimodal-cut-side-evidence-v1",
                "method": "bounded-cross-video-cut-sides-v1",
                "prepared_sha": p["media_sha256"],
                "source_video_sha": source_hashes,
                "manifest_sha": manifest_hashes,
                "clock_report": clock, "frame_links": linked,
                "cut_report": cut,
                "source_visual_method": SOURCE_VISUAL_VERSION,
                "visual_only_sequence_method": VISUAL_SEARCH_VERSION,
                "full_audio_sha": a["source_media_sha256"],
            })
            if side_identity != side.get("fingerprint"):
                raise ValueError("Cut sides do not refer to the pinned upstream reports")
            if not Path(p["media_path"]).is_file() or (
                checksum_file(Path(p["media_path"])) != p["media_sha256"]
            ):
                raise ValueError("Finished clip hash no longer matches cached measurements")
            for item in side.get("transitions", []):
                observation = item.get("candidate", {})
                if (item.get("editorial_gold") is not False
                        or observation.get("verified_edit_cut") is not False
                        or observation.get("exact_cut_frame_verified") is True
                        or observation.get("output_source_map_verified") is True):
                    raise ValueError("Unreviewed human edit cut was illegally promoted")
        except (OSError, ValueError, TypeError, KeyError, CostPolicyError):
            block("BLOCKED_MEASUREMENT_INTEGRITY")
            continue

        attempted += 1
        try:
            clip = provider.probe(candidate.clip_url)
            original = provider.probe(candidate.source_url)
            if not _public(clip) or not _public(original):
                block("QUARANTINED_NOT_PUBLIC_ENDED")
                continue
            if (human_clip_identity(clip.url) != clip_id
                    or original.external_id != candidate.source_stream_id
                    or human_clip_identity(original.url)
                       != human_clip_identity(candidate.source_url)):
                block("QUARANTINED_SOURCE_CHANGED")
                continue
            source = choose_source_candidate(provider.source_candidates(clip))
            if (source is None or human_clip_identity(source.url)
                    != human_clip_identity(candidate.source_url)):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            clip_policy = evaluate_source_metadata(
                clip, rules=policy_rules, operation=PolicyOperation.LEARN,
            )
            original_policy = evaluate_source_metadata(
                original, rules=policy_rules, operation=PolicyOperation.LEARN,
            )
            allowed_ranges = [(s["source_start"], s["source_end"]) for s in sections]
            for section in section_clocks:
                observed = section.get("approx_original_video_range")
                if (section.get("status") == "PARTIAL_SECTION_AUDIO_CLOCK"
                        and observed is not None):
                    if (not isinstance(observed, list) or len(observed) != 2
                            or not all(isinstance(t, (int, float)) and math.isfinite(t)
                                       for t in observed)
                            or not 0 <= observed[0] < observed[1]):
                        raise ValueError("Unsafe original source observed range")
                    allowed_ranges.append((max(0., observed[0] - 3), observed[1] + 3))
            range_policy = evaluate_source_ranges(
                original, ranges=allowed_ranges, rules=policy_rules,
                operation=PolicyOperation.LEARN, deferred_findings=original_policy.findings,
            )
            if any(rule.decision is not PolicyDecision.ALLOW
                   for rule in (clip_policy, original_policy, range_policy)):
                block("QUARANTINED_POLICY")
                continue
            identity = _digest({
                "method": VERSION, "mapping_version": MAP_VERSION,
                "audio_anchors": a.get("anchors", []),
                "clip_duration": p["duration_seconds"],
                "clock_report": clock, "frame_links": linked,
                "cut_report": cut, "cut_sides": side,
                "source_video_sha": source_hashes,
                "measurement_sha": manifest_hashes,
            })
            old = records.get(clip_id, {})
            if (old.get("status") == "MEASURED_UNREVIEWED_PARTIAL_MAP"
                    and old.get("fingerprint") == identity):
                if progress:
                    progress(f"partial map {clip_id}: reused verified evidence")
                continue
            observed = build(
                group_links, a.get("anchors", []),
                cut.get("refinement", {}).get("transitions", []),
                finished_duration=float(p["duration_seconds"]),
            )
            if (observed.get("status") not in {
                "PARTIAL_LOCAL_INTERVAL_CANDIDATES",
                "UNKNOWN_NO_SUPPORTED_LOCAL_INTERVALS", "UNKNOWN_PAIR_BUDGET",
            } or any(observed.get(flag) is not False for flag in (
                "qualification_allowed", "frame_accurate_cuts_verified",
                "source_absolute_video_pts_verified",
                "dense_output_source_map_verified", "reviewed_gold_available",
                "interpolation_verified",
            ))):
                raise ValueError("Unreviewed map attempted to promote evidence")
            records[clip_id] = {
                **base, "status": "MEASURED_UNREVIEWED_PARTIAL_MAP",
                "fingerprint": identity, "method": MAP_VERSION,
                "time_map": observed,
                "source_clock_fingerprint": clock["fingerprint"],
                "cut_side_fingerprint": side["fingerprint"],
                "source_section_sha256": source_hashes,
                "policy_clip": clip_policy.model_dump(mode="json"),
                "policy_source": original_policy.model_dump(mode="json"),
                "policy_ranges": range_policy.model_dump(mode="json"),
            }
            save()
            if progress:
                progress(f"partial map {clip_id}: MEASURED_UNREVIEWED_PARTIAL_MAP")
        except Exception as exc:
            records[clip_id] = {
                **base, "status": "RETRYABLE_FAILURE",
                "error_class": type(exc).__name__,
            }
            save()
            if progress:
                progress(f"partial map {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for item in records.values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "report_path": str(report_path), "attempted": attempted,
        "counts": counts, "qualification_allowed": False,
        "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "reviewed_gold_available": False,
    }
