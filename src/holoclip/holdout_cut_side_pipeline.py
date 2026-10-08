"""Unattended multimodal cut-side evidence, fully non-promotable.

Each run rechecks original media/frame/audio checksums and fresh clip policies
before decoding any short source or finished image windows.
"""
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
from holoclip.multimodal_cut_sides import (
    MAX_TRANSITIONS, VERSION as METHOD, compare_transition_sides,
)
from holoclip.provider_benchmark import atomic_json
from holoclip.providers.cost import CostPolicyError
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file
from holoclip.visual_source_crosscheck import (
    VERSION as SOURCE_VISUAL_VERSION, inspect_visual_only_source_changes,
)

VERSION = "reserved-multimodal-cut-side-evidence-v1"


def inspect_reserved_cut_sides(
    provider, *, store: Path, state_path: Path, prepared_root: Path,
    audio_root: Path, pairs_root: Path, clocks_root: Path,
    frame_links_root: Path, cuts_root: Path, output: Path, policy_rules: list,
    max_clips: int = 3, progress=None, compare=compare_transition_sides,
    crosscheck=inspect_visual_only_source_changes,
) -> dict:
    """Observe source jump versus edited output; never certify human editing gold."""
    if not 1 <= max_clips <= 20:
        raise ValueError("Invalid inspection clip budget")
    source_paths = [
        (prepared_root / "holdout-preparation.json", "reserved-holdout-measurement-v1"),
        (audio_root / "holdout-audio-alignment.json", "reserved-holdout-audio-alignment-v1"),
        (pairs_root / "holdout-source-pairs.json", "reserved-bounded-source-evidence-v1"),
        (clocks_root / "holdout-source-clock.json", "reserved-source-section-clock-v1"),
        (frame_links_root / "holdout-frame-links.json", "reserved-grounded-frame-links-v1"),
        (cuts_root / "holdout-cut-brackets.json", "reserved-acoustic-cut-brackets-v1"),
    ]
    payloads = []
    for path, version in source_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("version") != version
                or any(data.get(key) is not False for key in (
                    "qualification_allowed", "reviewed_gold_available",
                    "event_split_verified",
                ))):
            raise ValueError("Only unreviewed source and finished evidence is accepted")
        payloads.append(data)
    prepared, audio, pairs, clocks, frames, cuts = payloads
    if (pairs.get("frame_accurate_cuts_verified") is not False
            or clocks.get("frame_accurate_cuts_verified") is not False
            or frames.get("frame_accurate_cuts_verified") is not False
            or frames.get("dense_output_source_map_verified") is not False
            or cuts.get("frame_accurate_cuts_verified") is not False):
        raise ValueError("No independently reviewed frame-accurate cut proof supplied")

    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    inventory = holdout_inventory(store, state_path)
    examples = load_human_clip_store(store)
    trained_streams = {e.stream_id for e in examples if e.stream_id}
    trained_clips = {human_clip_identity(e.clip_url) for e in examples}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-cut-side-evidence.json"
    saved = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = saved.get("records", {}) if saved.get("version") == VERSION else {}
    report = {
        "version": VERSION, "status": "UNREVIEWED_MULTIMODAL_EDIT_CANDIDATES",
        "qualification_allowed": False, "benchmark_held_out": False,
        "event_split_verified": False, "reviewed_gold_available": False,
        "frame_accurate_cuts_verified": False, "original_video_absolute_pts_verified": False,
        "dense_output_source_map_verified": False, "records": records,
    }

    def save():
        atomic_json(report_path, report)

    attempted = 0
    for item in prioritize_unprocessed(
        _candidates(state), records, {"MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE"},
    ):
        if attempted >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        base = {
            "clip_id": clip_id, "clip_url": item.clip_url,
            "source_url": item.source_url, "source_stream_id": item.source_stream_id,
            "qualification_allowed": False, "reviewed_gold_available": False,
            "frame_accurate_cuts_verified": False, "event_split_verified": False,
        }

        def block(status):
            records[clip_id] = {**base, "status": status}
            save()
            if progress:
                progress(f"cut sides {clip_id}: {status}")

        if (item.source_stream_id in trained_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in trained_clips):
            block("BLOCKED_SPLIT_CONFLICT")
            continue
        if not item.source_url or item.source_stream_id not in state.holdout_stream_ids:
            block("BLOCKED_NOT_RESERVED")
            continue
        p, a, e, clock, linked, cut = (
            data.get("records", {}).get(clip_id, {}) for data in payloads
        )
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
                or not audio_path or not Path(audio_path).is_file()
                or checksum_file(Path(audio_path)) != a.get("source_media_sha256")):
            block("BLOCKED_AUDIO_UNVERIFIED")
            continue
        segments = e.get("pairs", [])
        clocks_for_sections = clock.get("section_clocks", [])
        group_links = linked.get("group_observations", [])
        if (e.get("status") != "MEASURED_UNREVIEWED_SOURCE_PAIRS"
                or e.get("source_url") != item.source_url
                or e.get("source_stream_id") != item.source_stream_id
                or e.get("audio_source_sha256") != a["source_media_sha256"]
                or e.get("finished_media_sha256") != p["media_sha256"]
                or clock.get("status") != "MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS"
                or clock.get("source_url") != item.source_url
                or clock.get("source_stream_id") != item.source_stream_id
                or clock.get("audio_source_sha256") != a["source_media_sha256"]
                or linked.get("status") != "MEASURED_UNREVIEWED_FRAME_LINKS"
                or linked.get("source_url") != item.source_url
                or linked.get("source_stream_id") != item.source_stream_id
                or linked.get("source_clock_fingerprint") != clock.get("fingerprint")
                or linked.get("audio_source_sha256") != a["source_media_sha256"]
                or linked.get("finished_media_sha256") != p["media_sha256"]
                or cut.get("status") != "MEASURED_UNREVIEWED_CUT_BRACKETS"
                or cut.get("method") != REFINEMENT_VERSION
                or not isinstance(cut.get("refinement"), dict)
                or cut["refinement"].get("version") != REFINEMENT_VERSION
                or cut.get("source_url") != item.source_url
                or cut.get("source_stream_id") != item.source_stream_id
                or cut.get("audio_source_sha256") != a["source_media_sha256"]
                or cut.get("finished_media_sha256") != p["media_sha256"]
                or not isinstance(segments, list)
                or not 1 <= len(segments) <= 5
                or len(segments) != len(clocks_for_sections)
                or len(segments) != len(group_links)):
            block("BLOCKED_UPSTREAM_PROVENANCE")
            continue

        try:
            supported = _valid_ranges(a, float(p["duration_seconds"]), max_ranges=5)
            if not supported or len(supported) < len(segments):
                raise ValueError("Missing independently anchored acoustic spans")
            media_hashes = []
            manifests = []
            for idx, (section, proof, observed, acoustic) in enumerate(
                zip(segments, clocks_for_sections, group_links, supported)
            ):
                media = Path(section["source_video_path"])
                if (section.get("group") != idx
                        or proof.get("group") != idx
                        or observed.get("group") != idx
                        or not media.is_file()
                        or not 0 < media.stat().st_size <= 256 * 1024 * 1024
                        or checksum_file(media) != section["source_video_sha256"]
                        or proof.get("video_sha256") != section["source_video_sha256"]
                        or proof.get("requested_source_range") != [
                            section["source_start"], section["source_end"],
                        ]
                        or observed.get("requested_source_range") != [
                            section["source_start"], section["source_end"],
                        ]
                        or section["source_start"] > acoustic["source_start"] + .05
                        or section["source_end"] < acoustic["source_end"] - .05):
                    raise ValueError("Source, clock and audio section origin contradiction")
                media_hashes.append(section["source_video_sha256"])
                for label in ("source", "finished"):
                    manifest = section[f"{label}_measurement"]
                    if _checked_measurement(manifest) != section[f"{label}_window_sha256"]:
                        raise ValueError("Changed measured original or output frame")
                    manifests.append(checksum_file(Path(manifest)))
            clock_fingerprint = hashlib.sha256(json.dumps({
                "version": "reserved-source-section-clock-v1",
                "method": "bounded-source-audio-clock-v1",
                "audio_sha": a["source_media_sha256"],
                "finished_sha": p["media_sha256"],
                "source_video_sha": media_hashes,
                "measurement_sha": manifests,
                "sections": [(s["source_start"], s["source_end"]) for s in segments],
            }, sort_keys=True).encode()).hexdigest()
            if clock_fingerprint != clock.get("fingerprint"):
                raise ValueError("Source audio clock was calibrated on different input bytes")
            if not Path(p["media_path"]).is_file():
                raise ValueError("Finished media unavailable")
            if checksum_file(Path(p["media_path"])) != p["media_sha256"]:
                raise ValueError("Finished media no longer matches declared content hash")
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
            if (candidate is None or human_clip_identity(candidate.url)
                    != human_clip_identity(item.source_url)):
                block("QUARANTINED_SOURCE_AMBIGUOUS")
                continue
            cp = evaluate_source_metadata(clip, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            sp = evaluate_source_metadata(source, rules=policy_rules,
                                          operation=PolicyOperation.LEARN)
            ranges = [(s["source_start"], s["source_end"]) for s in segments]
            for evidence in clocks_for_sections:
                seen = evidence.get("approx_original_video_range")
                if seen is not None and evidence.get("status") == "PARTIAL_SECTION_AUDIO_CLOCK":
                    if (not isinstance(seen, list) or len(seen) != 2
                            or not all(isinstance(x, (int, float)) and math.isfinite(x)
                                       for x in seen) or not 0 <= seen[0] < seen[1]):
                        raise ValueError("Invalid measured source rights interval")
                    ranges.append((max(0., seen[0] - 3.), seen[1] + 3.))
            scoped = evaluate_source_ranges(
                source, ranges=ranges, rules=policy_rules,
                operation=PolicyOperation.LEARN, deferred_findings=sp.findings,
            )
            if any(p.decision is not PolicyDecision.ALLOW for p in (cp, sp, scoped)):
                block("QUARANTINED_POLICY")
                continue
            identity = hashlib.sha256(json.dumps({
                "version": VERSION, "method": METHOD, "prepared_sha": p["media_sha256"],
                "source_video_sha": media_hashes, "manifest_sha": manifests,
                "clock_report": clock, "frame_links": linked, "cut_report": cut,
                "source_visual_method": SOURCE_VISUAL_VERSION,
                "full_audio_sha": a["source_media_sha256"],
            }, sort_keys=True).encode()).hexdigest()
            cached = records.get(clip_id, {})
            if (cached.get("status") == "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE"
                    and cached.get("fingerprint") == identity):
                if progress:
                    progress(f"cut sides {clip_id}: reused verified observations")
                continue

            possible = [
                t for t in cut.get("refinement", {}).get("transitions", [])
                if t.get("status") == "CUT_INTERVAL_CANDIDATE"
            ]
            prior_visual = cut.get("output_visual_cut_evidence", [])
            if len(possible) > MAX_TRANSITIONS:
                details = [{
                    "status": "UNKNOWN_TRANSITION_BUDGET",
                    "unresolved": len(possible),
                }]
            else:
                details = []
                for transition in possible:
                    corroborated = next((
                        evidence for evidence in prior_visual
                        if evidence.get("acoustic_interval") == transition.get("interval")
                        and evidence.get("acoustic_offset_change_seconds")
                            == transition.get("offset_change_seconds")
                    ), {})
                    measured = compare(
                        Path(p["media_path"]), segments, clocks_for_sections,
                        cut["refinement"], transition, corroborated,
                        clip_duration=float(p["duration_seconds"]),
                    )
                    if measured.get("status") not in {
                        "MULTIMODAL_EDIT_JUMP_CANDIDATE",
                        "UNKNOWN_INDEPENDENT_VISUAL_SUPPORT",
                        "UNKNOWN_UNSUPPORTED_ACOUSTIC_INTERVAL",
                        "UNKNOWN_OUTPUT_VISUAL_JUMP", "UNKNOWN_SOURCE_GROUP",
                        "UNKNOWN_SECTION_CLOCK", "UNKNOWN_MISSING_SIDE_AUDIO_ANCHOR",
                        "UNKNOWN_SOURCE_SEEK_OUTSIDE_SECTION",
                        "UNKNOWN_SOURCE_CONTINUITY", "UNKNOWN_REORDERED_SOURCE",
                        "UNKNOWN_BOUNDED_FRAME_DECODE",
                        "UNKNOWN_ONE_OR_BOTH_SIDES_UNVERIFIED",
                    } or measured.get("verified_edit_cut") is not False:
                        raise ValueError("Unexpected promoted or malformed cut-side evidence")
                    details.append({
                        "acoustic_interval": transition["interval"],
                        "candidate": measured, "editorial_gold": False,
                    })
            # The independent output survey may contain a screen change
            # with no acoustic cut. Inspect only source-video sections already
            # validated for hashes, rights, stream ID and partial audio clock.
            visual_source = crosscheck(
                cut.get("independent_output_visual_survey"),
                segments, clocks_for_sections, cut["refinement"],
            )
            if (not isinstance(visual_source, dict)
                    or visual_source.get("version") != SOURCE_VISUAL_VERSION
                    or visual_source.get("status") not in {
                        "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY",
                        "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET",
                        "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS",
                    }
                    or any(visual_source.get(k) is not False for k in (
                        "verified_edit_cut", "source_scene_change_ruled_out",
                        "original_video_absolute_pts_verified",
                        "reviewed_gold_available", "qualification_allowed",
                    ))):
                raise ValueError("Unreviewed output/source visual crosscheck was promoted")
            records[clip_id] = {
                **base, "status": "MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE",
                "fingerprint": identity, "method": METHOD,
                "source_clock_fingerprint": clock["fingerprint"],
                "transitions": details,
                "independent_visual_source_checks": visual_source,
                "source_visual_method_version": SOURCE_VISUAL_VERSION,
                "audio_source_sha256": a["source_media_sha256"],
                "finished_media_sha256": p["media_sha256"],
                "source_section_sha256": media_hashes,
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
                "policy_ranges": scoped.model_dump(mode="json"),
                "unknowns": [
                    "Direct source/finished images remain source-audio-relative",
                    "An editor-cut causal boundary, native original frame time, and true gold remain unverified",
                ],
            }
            save()
            if progress:
                progress(f"cut sides {clip_id}: MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE")
        except Exception as exc:
            records[clip_id] = {
                **base, "status": "RETRYABLE_FAILURE",
                "error_class": type(exc).__name__,
            }
            save()
            if progress:
                progress(f"cut sides {clip_id}: RETRYABLE_FAILURE")
    save()
    totals = {}
    for record in records.values():
        totals[record["status"]] = totals.get(record["status"], 0) + 1
    return {
        "report_path": str(report_path), "attempted": attempted,
        "counts": totals, "qualification_allowed": False,
        "frame_accurate_cuts_verified": False, "reviewed_gold_available": False,
        "dense_output_source_map_verified": False,
    }
