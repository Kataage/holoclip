"""Offline omission probes from existing aligned human clips and cached local ASR.

This is observational *in-sample* source-clock evidence from actual human
alignment metadata, not ground truth semantic roles or editorial quality.
No source download, model, audio decoding, or video changes occur.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from holoclip.examples import HumanClipExample, human_clip_identity
from holoclip.render_same_source_geometry import (
    DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE, DEFAULT_TRANSCRIPT_ROOT,
    _human_by_source, _reserve_state,
)
from holoclip.render_same_source_narrative_reference import _shared, _union
from holoclip.render_same_source_selection_presence import source_span_coverage
from holoclip.render_same_source_transcript_context import _read_transcript, _utterances
from holoclip.render_plan import _no_links

VERSION = "holoclip-real-source-alignment-omission-probes-v1"
_MAX_PAGE = 20
_MAX_ASR_EXCERPTS_PER_GROUP = 3
_MAX_ALIGNED_SPANS = 200
_POSITIONS = ("opening_aligned", "intermediate_aligned", "closing_aligned")


def _spans(example: HumanClipExample) -> list[tuple[float, float]]:
    ordered = sorted(
        example.alignment_segments, key=lambda s: (s.clip_start, s.clip_end),
    )
    if not 2 <= len(ordered) <= _MAX_ALIGNED_SPANS:
        raise ValueError("LOCAL_REAL_PROBE_ALIGNMENT_COUNT_INVALID")
    previous_clip_end = -1.0
    out = []
    for span in ordered:
        start, end = span.source_start, span.source_end
        if (not all(math.isfinite(x) for x in (
                span.clip_start, span.clip_end, start, end))
                or not 0 <= start < end <= 86400
                or span.clip_start < previous_clip_end - 0.001):
            raise ValueError("LOCAL_REAL_PROBE_ALIGNMENT_UNTRUSTED")
        previous_clip_end = span.clip_end
        out.append((start, end))
    return out


def _sample_asr(
    source_asr: list, group: list[tuple[float, float]],
) -> list[dict]:
    """Bound speech evidence, with no verbatim excerpts in exported JSON."""
    observations = []
    seen: set[tuple[float, float, str]] = set()
    for start, end in group:
        for item in _utterances(source_asr, start, end):
            if len(observations) >= _MAX_ASR_EXCERPTS_PER_GROUP:
                break
            key = (
                item["start_seconds"], item["end_seconds"],
                item["unverified_asr_text"],
            )
            if key in seen:
                continue
            seen.add(key)
            observations.append({
                "asr_start_seconds": item["start_seconds"],
                "asr_end_seconds": item["end_seconds"],
                "asr_text_sha256": hashlib.sha256(
                    item["unverified_asr_text"].encode("utf-8")
                ).hexdigest(),
            })
        if len(observations) >= _MAX_ASR_EXCERPTS_PER_GROUP:
            break
    return observations


def _variant(
    name: str, omitted: list[tuple[float, float]],
    retained: list[tuple[float, float]],
    observed_asr: list[dict],
) -> dict:
    omitted_union = _union(omitted)
    baseline_union = _union(omitted + retained)
    remainder_union = _union(retained)
    omitted_seconds = sum(e - s for s, e in omitted_union)
    preserved = _shared(omitted_union, remainder_union)
    source_lost = max(0.0, omitted_seconds - preserved)
    excerpt_records = []
    for observation in observed_asr:
        start, end = observation["asr_start_seconds"], observation["asr_end_seconds"]
        before = source_span_coverage(start, end, baseline_union)
        after = source_span_coverage(start, end, remainder_union)
        # ASR is source-timed, unverified. These values do not prove that a
        # word was audible or that an edit omitted the semantic narrative role.
        lost = max(0.0, before["source_clock_selected_seconds"]
                   - after["source_clock_selected_seconds"])
        excerpt_records.append({
            **observation,
            "baseline_selected_seconds": before["source_clock_selected_seconds"],
            "counterfactual_selected_seconds": after["source_clock_selected_seconds"],
            "lost_source_clock_seconds": round(lost, 3),
            "counterfactual_source_clock_status": after["source_clock_selection_status"],
        })
    return {
        "omitted_position": name,
        "counterfactual_is_virtual_not_rendered": True,
        "removed_aligned_segment_count": len(omitted),
        "retained_aligned_segment_count": len(retained),
        "removed_group_unique_source_seconds": round(omitted_seconds, 3),
        "source_seconds_lost_after_union": round(source_lost, 3),
        "overlap_recovered_by_other_aligned_spans_seconds": round(preserved, 3),
        "sampled_source_asr_utterances": excerpt_records,
        "source_clock_utterances_with_observed_loss": sum(
            x["lost_source_clock_seconds"] > 0 for x in excerpt_records
        ),
        "human_aligned_position_not_semantic_role": True,
    }


def _reference_probe(
    example: HumanClipExample, source_asr: list,
    transcript_sha: str,
) -> dict:
    aligned = _spans(example)
    groups = (
        ("opening_aligned", aligned[:1]),
        ("intermediate_aligned", aligned[1:-1]),
        ("closing_aligned", aligned[-1:]),
    )
    probes = []
    for name, omitted in groups:
        if not omitted:
            continue
        retained = []
        for other_name, others in groups:
            if other_name != name:
                retained.extend(others)
        samples = _sample_asr(source_asr, omitted)
        probes.append(_variant(name, omitted, retained, samples))
    return {
        "canonical_stream_sha256": hashlib.sha256(
            example.stream_id.encode("utf-8")
        ).hexdigest(),
        "human_clip_identity_sha256": hashlib.sha256(
            human_clip_identity(example.clip_url).encode("utf-8")
        ).hexdigest(),
        "local_source_transcript_sha256": transcript_sha,
        "human_alignment_confidence": example.alignment_confidence,
        "human_aligned_segment_count": len(aligned),
        "human_aligned_unique_source_seconds": round(
            sum(e - s for s, e in _union(aligned)), 3
        ),
        "probes": probes,
        "clip_level_story_descriptions_are_not_source_time_labels": True,
        "asr_audio_independently_verified": False,
        "human_training_reference_not_holdout_gold": True,
        "narrative_roles_verified": False,
    }


def probe_real_source_omissions(
    *, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    max_examples: int = 12,
) -> dict:
    """Deterministic bounded page of real aligned examples, never an LLM test."""
    if type(max_examples) is not int or not 1 <= max_examples <= _MAX_PAGE:
        raise ValueError("LOCAL_REAL_PROBE_LIMIT_INVALID")
    example_store = _no_links(example_store)
    growth_state = _no_links(growth_state)
    transcript_root = _no_links(transcript_root)
    reserved, state_sha, reservations_known = _reserve_state(growth_state)
    grouped, corpus = _human_by_source(example_store, reserved)
    report = {
        "version": VERSION,
        "status": "LOCAL_REAL_PROBE_UNQUALIFIED_NO_ELIGIBLE_EXAMPLES",
        "max_examples": max_examples,
        "corpus": corpus,
        "holdout_state_sha256": state_sha,
        "holdout_reservation_metadata_verified": reservations_known,
        "reference_count": 0,
        "candidate_trusted_reference_count": 0,
        "unavailable_local_asr_count": 0,
        "untrusted_or_ambiguous_local_asr_count": 0,
        "insufficient_alignment_count": 0,
        "listed_reference_limit_reached": False,
        "results": [],
        "source_alignments_from_training_not_independent_gold": True,
        "synthetic_probes_on_real_alignment_metadata_only": True,
        "real_video_semantic_accuracy": None,
        "narrative_quality_precision": None,
        "narrative_quality_recall": None,
        "local_asr_is_unverified": True,
        "external_ai_requests": 0,
        "video_renders_performed": 0,
        "auto_edit_allowed": False,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }
    if not reservations_known or corpus["holdout_training_conflicts"]:
        report["status"] = "LOCAL_REAL_PROBE_HOLDOUT_GATES_BLOCKED"
        return report

    entries = sorted(
        (
            example for examples in grouped.values() for example in examples
        ),
        key=lambda e: (
            hashlib.sha256(human_clip_identity(e.clip_url).encode("utf-8")).hexdigest(),
            e.stream_id,
        ),
    )
    report["candidate_trusted_reference_count"] = len(entries)
    cache: dict[str, tuple[list, str, str] | None] = {}
    invalid: set[str] = set()
    for example in entries:
        if len(report["results"]) >= max_examples:
            report["listed_reference_limit_reached"] = True
            break
        try:
            _spans(example)
        except (ValueError, TypeError):
            report["insufficient_alignment_count"] += 1
            continue
        stream = example.stream_id
        if stream not in cache and stream not in invalid:
            try:
                cache[stream] = _read_transcript(transcript_root, stream)
            except (OSError, ValueError, UnicodeError, TypeError, KeyError):
                invalid.add(stream)
        if stream in invalid:
            report["untrusted_or_ambiguous_local_asr_count"] += 1
            continue
        parsed = cache[stream]
        if parsed is None:
            report["unavailable_local_asr_count"] += 1
            continue
        segments, digest, source_id_basis = parsed
        if source_id_basis != "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED":
            report["untrusted_or_ambiguous_local_asr_count"] += 1
            continue
        try:
            report["results"].append(_reference_probe(example, segments, digest))
        except (ValueError, TypeError):
            report["insufficient_alignment_count"] += 1

    if report["results"]:
        report["status"] = "LOCAL_REAL_PROBE_SOURCE_CLOCK_OBSERVED_UNQUALIFIED"
    report["reference_count"] = len(report["results"])
    return report
