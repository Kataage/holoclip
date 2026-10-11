"""Bounded local inventory of real in-sample semantic evidence readiness.

Reports how much *existing* trusted human-alignment data can be paired with
provenance-checked source ASR without fetching media, running NIM, or asking
for operator-created labels. This is not independent semantic accuracy.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from holoclip.examples import HumanClipExample, human_clip_identity
from holoclip.render_plan import _no_links
from holoclip.render_same_source_geometry import (
    DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE, DEFAULT_TRANSCRIPT_ROOT,
    _human_by_source, _reserve_state,
)
from holoclip.render_real_source_omission_probe import _sample_asr, _spans
from holoclip.render_same_source_semantic_review import _clean
from holoclip.render_same_source_transcript_context import (
    INGESTED_MANIFEST_BASIS, _read_transcript, trusted_source_manifest_path,
)

VERSION = "holoclip-real-semantic-corpus-inventory-v1"
_MAX_STREAMS = 128
_MAX_SUGGESTIONS = 12
_ROLES = ("setup", "development", "payoff")
_GROUPS = ("opening_aligned", "intermediate_aligned", "closing_aligned")
_EVIDENCE_BASIS = {
    "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
    INGESTED_MANIFEST_BASIS,
}


def _clip_evidence(example: HumanClipExample, source_asr: list) -> dict | None:
    """Only positional coverage; never assign setup/payoff to timestamps."""
    try:
        spans = _spans(example)
    except (ValueError, TypeError):
        return None
    groups = (spans[:1], spans[1:-1], spans[-1:])
    counts = {
        group: len(_sample_asr(source_asr, intervals)) if intervals else 0
        for group, intervals in zip(_GROUPS, groups, strict=True)
    }
    annotations_present = all(
        _clean(getattr(example, role), 280) is not None
        for role in _ROLES
    )
    # Two different human-clip positions (including opening + closing)
    # allow a nontrivial *positional* omission experiment. This remains
    # in-sample evidence, not correctness of the narrative role descriptions.
    positional_evidence = (
        counts["opening_aligned"] > 0
        and counts["closing_aligned"] > 0
    )
    return {
        "human_clip_identity_sha256": hashlib.sha256(
            human_clip_identity(example.clip_url).encode("utf-8")
        ).hexdigest(),
        "aligned_segment_count": len(spans),
        "local_asr_sampled_by_human_clip_position": counts,
        "human_clip_level_role_descriptions_present": annotations_present,
        "positionally_observable": positional_evidence,
        "role_hypothesis_case_possible": positional_evidence and annotations_present,
        "alignment_confidence": example.alignment_confidence,
    }


def inspect_real_corpus_readiness(
    *, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    max_streams: int = 128,
    max_suggestions: int = 12,
) -> dict:
    """Deterministic one-pass inventory; source diversity and bounded output."""
    if type(max_streams) is not int or not 1 <= max_streams <= _MAX_STREAMS:
        raise ValueError("LOCAL_REAL_INVENTORY_STREAM_LIMIT_INVALID")
    if (type(max_suggestions) is not int
            or not 1 <= max_suggestions <= _MAX_SUGGESTIONS):
        raise ValueError("LOCAL_REAL_INVENTORY_SUGGESTION_LIMIT_INVALID")
    example_store = _no_links(example_store)
    growth_state = _no_links(growth_state)
    transcript_root = _no_links(transcript_root)
    reserved, holdout_digest, known = _reserve_state(growth_state)
    grouped, corpus = _human_by_source(example_store, reserved)
    report = {
        "version": VERSION,
        "status": "LOCAL_REAL_INVENTORY_HOLDOUT_BLOCKED",
        "corpus": corpus,
        "holdout_state_sha256": holdout_digest,
        "holdout_reservation_metadata_verified": known,
        "scope": "BOUNDED_EXISTING_LOCAL_CORPUS",
        "stream_scan_limit": max_streams,
        "selected_reference_limit": max_suggestions,
        "trusted_stream_count": len(grouped),
        "scanned_stream_count": 0,
        "remaining_stream_count_not_scanned": 0,
        "scope_exhaustive": False,
        "source_asr_metadata_verified_stream_count": 0,
        "source_asr_ingest_manifest_matched_stream_count": 0,
        "source_asr_missing_stream_count": 0,
        "source_asr_untrusted_or_ambiguous_stream_count": 0,
        "source_asr_filename_only_stream_count": 0,
        "eligible_two_segment_reference_count": 0,
        "insufficient_alignment_reference_count": 0,
        "positional_evidence_reference_count": 0,
        "role_description_complete_reference_count": 0,
        "role_hypothesis_case_possible_reference_count": 0,
        "positionally_observable_distinct_stream_count": 0,
        "selected_reference_count": 0,
        "suggested_in_sample_references": [],
        "raw_video_identifiers_included": False,
        "verbatim_asr_text_included": False,
        "human_story_descriptions_included": False,
        "human_training_references_not_independent_holdout_gold": True,
        "position_groups_not_semantic_role_timestamps": True,
        "real_video_role_accuracy": None,
        "independent_semantic_gold_available": False,
        "external_ai_requests": 0,
        "new_asr_performed": False,
        "media_downloads_performed": False,
        "auto_edit_allowed": False,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }
    if not known or corpus["holdout_training_conflicts"]:
        return report

    # Scan by the opaque stream hash; no raw ID or source URL is reported.
    streams = sorted(grouped, key=lambda s: (hashlib.sha256(s.encode()).hexdigest(), s))
    targets = streams[:max_streams]
    report["scanned_stream_count"] = len(targets)
    report["remaining_stream_count_not_scanned"] = len(streams) - len(targets)
    report["scope_exhaustive"] = len(targets) == len(streams)
    report["status"] = "LOCAL_REAL_INVENTORY_NO_ROLE_CASES_UNQUALIFIED"
    candidates: list[dict] = []
    for stream in targets:
        examples = grouped[stream]
        manifest = trusted_source_manifest_path(examples)
        try:
            parsed = _read_transcript(
                transcript_root, stream,
                trusted_source_manifest_path=manifest,
            )
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            report["source_asr_untrusted_or_ambiguous_stream_count"] += 1
            continue
        if parsed is None:
            report["source_asr_missing_stream_count"] += 1
            continue
        asr, source_sha, basis = parsed
        if basis not in _EVIDENCE_BASIS:
            report["source_asr_filename_only_stream_count"] += 1
            continue
        if basis == INGESTED_MANIFEST_BASIS:
            report["source_asr_ingest_manifest_matched_stream_count"] += 1
        else:
            report["source_asr_metadata_verified_stream_count"] += 1
        source_has_position = False
        for human in examples:
            clip = _clip_evidence(human, asr)
            if clip is None:
                report["insufficient_alignment_reference_count"] += 1
                continue
            report["eligible_two_segment_reference_count"] += 1
            if clip["human_clip_level_role_descriptions_present"]:
                report["role_description_complete_reference_count"] += 1
            if clip["positionally_observable"]:
                report["positional_evidence_reference_count"] += 1
                source_has_position = True
            if not clip["role_hypothesis_case_possible"]:
                continue
            report["role_hypothesis_case_possible_reference_count"] += 1
            candidates.append({
                "source_stream_sha256": hashlib.sha256(stream.encode()).hexdigest(),
                "local_transcript_sha256": source_sha,
                "source_identity_basis": basis,
                **clip,
                "human_reference_is_training_in_sample": True,
                "independent_gold_not_available": True,
            })
        report["positionally_observable_distinct_stream_count"] += int(
            source_has_position
        )

    # Prioritize richest measured ASR evidence but keep at most one example
    # per source, preventing popular streams dominating the shortlist.
    candidates.sort(key=lambda item: (
        -sum(item["local_asr_sampled_by_human_clip_position"].values()),
        -item["aligned_segment_count"],
        -(item["alignment_confidence"] or 0),
        item["source_stream_sha256"],
        item["human_clip_identity_sha256"],
    ))
    selected_streams: set[str] = set()
    for candidate in candidates:
        stream_hash = candidate["source_stream_sha256"]
        if stream_hash in selected_streams:
            continue
        selected_streams.add(stream_hash)
        report["suggested_in_sample_references"].append(candidate)
        if len(selected_streams) == max_suggestions:
            break
    report["selected_reference_count"] = len(selected_streams)
    if candidates:
        report["status"] = "LOCAL_REAL_INVENTORY_POSITIONAL_CASES_AVAILABLE_UNQUALIFIED"
    return report
