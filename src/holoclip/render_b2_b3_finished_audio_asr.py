"""Independent finished B2/B3 audio-ASR evidence, never spoken-word proof.

Only explicitly authorized public-data ASR calls. The default inspects
pre-existing immutable four-way QA and local source-ASR evidence without
decoding, making provider requests, or writing files.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from holoclip.render_cut_extension_clip_asr_alignment import temporal_asr_review
from holoclip.render_cut_extension_three_way_clip_asr import (
    _compare_observation, _normalize, _read_clip, _safe_asr,
    _safe_provenance, _segments,
)
from holoclip.render_four_way_local_diagnostics import (
    _context, _ensure_same_before_after,
)
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_same_source_transcript_context import _read_transcript
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_source_resolver import _canonical_assets
from holoclip.render_three_way_existing_media_pilot import _completed_jobs

VERSION = "holoclip-b2-b3-finished-audio-cross-asr-v1"
_ROLES = ("b2", "b3")
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_MAX_EXCERPTS = 8
_MAX_SOURCE_SEGMENTS = 20000
_MAX_FOCUS_SECONDS = 8.0
_WARNINGS = {
    "SHORT_TEXT_AMBIGUOUS_REVIEW",
    "PARTIAL_ASR_TEXT_OVERLAP_REVIEW",
    "LOW_ASR_TEXT_OVERLAP_AT_EXPECTED_TIME",
    "NO_EDITED_ASR_NEAR_EXPECTED_TIME",
    "TEXT_MATCH_FAR_FROM_SOURCE_CLOCK",
    "MULTIPLE_PLAUSIBLE_TRANSCRIPT_OCCURRENCES",
}
_NO_WARNING = "TIME_LOCAL_TEXT_OVERLAP_UNVERIFIED"


def _four_way(context: dict) -> tuple[dict, str]:
    """Require an actual completed, still-pinned four-way diagnostic receipt."""
    path = _no_links(context["found"]["target"].with_name(
        f"{context['found']['target'].stem}.four-way-diagnostics.json"
    ))
    if (not path.is_file() or path.is_symlink()
            or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES):
        raise ValueError("LOCAL_B2_B3_ASR_FOUR_WAY_QA_MISSING_OR_UNSAFE")
    raw = path.read_bytes()
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_B2_B3_ASR_QA_INVALID_JSON") from exc
    if (not isinstance(report, dict)
            or report.get("status")
            != "LOCAL_FOUR_WAY_TECHNICAL_ACOUSTIC_SIGNALS_ONLY"
            or report.get("job_id") != context["job_id"]
            or report.get("trial_id") != context["trial_id"]
            or report.get("b2_id") != context["b2_id"]
            or report.get("b3_id") != context["b3_id"]
            or report.get("source_asr_sha256") != context["source_asr_sha256"]
            or report.get("source_receipt_sha256") != context["receipt_sha256"]
            or report.get("actual_videos_scanned") != 4
            or report.get("human_audio_review_completed") is not False
            or report.get("automatic_adoption_allowed") is not False
            or report.get("publication_performed") is not False
            or report.get("ready") is not False
            or report.get("production_ready") is not False
            or report.get("best_variant") is not None):
        raise ValueError("LOCAL_B2_B3_ASR_FOUR_WAY_LINEAGE_UNTRUSTED")
    variants = report.get("variants")
    if not isinstance(variants, dict) or set(variants) != {"a", "b", "b2", "b3"}:
        raise ValueError("LOCAL_B2_B3_ASR_FOUR_WAY_ROLES_INVALID")
    samples = 0
    for role, item in variants.items():
        known = context["roles"][role]
        if (not isinstance(item, dict)
                or item.get("final_sha256") != known["final_sha256"]
                or item.get("base_sha256") != known["base_sha256"]
                or not isinstance(item.get("technical_diagnostic"), dict)
                or not str(item["technical_diagnostic"].get("status", "")).startswith(
                    "LOCAL_EDIT_DIAGNOSTIC_MEASURED_"
                )):
            raise ValueError("LOCAL_B2_B3_ASR_QA_VARIANT_CHANGED")
        windows = item.get("edge_audio_samples")
        expected = context["audio_windows"][role]
        if not isinstance(windows, list) or len(windows) != len(expected):
            raise ValueError("LOCAL_B2_B3_ASR_QA_AUDIO_WINDOWS_CHANGED")
        for observed, baseline in zip(windows, expected):
            if (not isinstance(observed, dict)
                    or any(observed.get(k) != baseline[k] for k in (
                        "kind", "output_start_seconds", "output_end_seconds",
                    ))
                    or type(observed.get("near_silent_energy_hint")) is not bool
                    or any(type(observed.get(k)) not in (float, int)
                           or not math.isfinite(observed[k]) for k in (
                               "mean_dbfs", "peak_dbfs",
                           ))):
                raise ValueError("LOCAL_B2_B3_ASR_QA_AUDIO_SAMPLE_UNTRUSTED")
            samples += 1
    if report.get("actual_audio_samples_measured") != samples:
        raise ValueError("LOCAL_B2_B3_ASR_QA_AUDIO_COUNTS_CHANGED")
    return report, hashlib.sha256(raw).hexdigest()


def _source(context: dict) -> tuple[list, str, str]:
    found = context["found"]
    original = found["trial"]["original"]
    cuts = {
        cut.source_asset_id
        for role in _ROLES for cut in context["roles"][role]["timeline"].segments
    }
    if len(cuts) != 1:
        raise ValueError("LOCAL_B2_B3_ASR_MULTISOURCE_NOT_ATTESTED")
    asset_id = next(iter(cuts))
    source = _canonical_assets(original["database"], {asset_id})[asset_id]
    parsed = _read_transcript(
        _no_links(found["b2"]["prepared"]["transcript_root"]),
        source.external_id,
    )
    if (parsed is None or parsed[1] != context["source_asr_sha256"]
            or parsed[2] not in {
                "FILENAME_ONLY_IDENTITY_UNVERIFIED",
                "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
            }
            or len(parsed[0]) > _MAX_SOURCE_SEGMENTS):
        raise ValueError("LOCAL_B2_B3_ASR_SOURCE_NOT_VERIFIED")
    return parsed


def _focus(timeline, baseline, *, index: int, side: str) -> dict:
    """Same original edit seam, projected into each finished output clock."""
    ordered = sorted(timeline.segments, key=lambda c: c.output_order)
    original = sorted(baseline.segments, key=lambda c: c.output_order)
    if (type(index) is not int or side not in ("EXTEND_START", "EXTEND_END")
            or not 0 <= index < len(ordered)
            or len(original) != len(ordered)):
        raise ValueError("LOCAL_B2_B3_ASR_EDIT_GEOMETRY_INVALID")
    cut, base = ordered[index], original[index]
    before = sum(c.source_end - c.source_start for c in ordered[:index])
    seam = before + (
        base.source_start - cut.source_start if side == "EXTEND_START"
        else base.source_end - cut.source_start
    )
    duration = timeline.estimated_duration
    if (not all(math.isfinite(x) for x in (seam, duration))
            or not 0 <= seam <= duration <= 180):
        raise ValueError("LOCAL_B2_B3_ASR_SEAM_INVALID")
    lo = round(max(0.0, seam - 2.0), 3)
    hi = round(min(duration, seam + 3.0), 3)
    if not .3 <= hi - lo <= _MAX_FOCUS_SECONDS:
        raise ValueError("LOCAL_B2_B3_ASR_FOCUS_INVALID")
    return {
        "output_start_seconds": lo,
        "output_end_seconds": hi,
        "same_source_seam_not_same_output_clock": True,
    }


def _references(timeline, utterances: list, digest: str, focus: dict) -> list[dict]:
    """Only fully included source-ASR utterances can be textual expectations."""
    lo, hi = focus["output_start_seconds"], focus["output_end_seconds"]
    middle = (lo + hi) / 2
    candidates = []
    cursor = 0.
    for cut in sorted(timeline.segments, key=lambda c: c.output_order):
        duration = cut.source_end - cut.source_start
        for i, utterance in enumerate(utterances):
            if (utterance.start < cut.source_start - .001
                    or utterance.end > cut.source_end + .001
                    or utterance.start >= utterance.end):
                continue
            begin = cursor + utterance.start - cut.source_start
            end = cursor + utterance.end - cut.source_start
            if begin < lo - .002 or end > hi + .002:
                continue
            normalized = _normalize(_safe_asr(utterance.text))
            if not normalized:
                continue
            token = hashlib.sha256(
                f"{digest}|{i}|{utterance.start:.6f}|{utterance.end:.6f}".encode()
            ).hexdigest()[:20]
            candidates.append({
                "id": token, "text": normalized,
                "output_start_seconds": round(begin, 3),
                "output_end_seconds": round(end, 3),
                "source_start_seconds": round(utterance.start, 3),
                "source_end_seconds": round(utterance.end, 3),
            })
        cursor += duration
    candidates.sort(key=lambda r: (
        abs((r["output_start_seconds"] + r["output_end_seconds"]) / 2 - middle),
        r["output_start_seconds"], r["id"],
    ))
    chosen = sorted(candidates[:_MAX_EXCERPTS], key=lambda r: (
        r["output_start_seconds"], r["id"],
    ))
    if len({x["id"] for x in chosen}) != len(chosen):
        raise ValueError("LOCAL_B2_B3_ASR_SOURCE_DUPLICATE_AMBIGUOUS")
    return chosen


def _basis(inputs: dict, candidate_index: int) -> dict:
    context = _context(inputs, candidate_index)
    _report, qa_sha = _four_way(context)
    utterances, source_sha, source_basis = _source(context)
    original = context["found"]["trial"]["original"]["baseline_timeline"]
    timelines = {
        role: context["roles"][role]["timeline"] for role in _ROLES
    }
    focus = {
        role: _focus(timelines[role], original,
                     index=context["source_cut_index"], side=context["trial_action"])
        for role in _ROLES
    }
    refs = {
        role: _references(timelines[role], utterances, source_sha, focus[role])
        for role in _ROLES
    }
    target = _no_links(context["found"]["target"].with_name(
        f"{context['found']['target'].stem}.b2-b3-audio-asr.json"
    ))
    return {
        "context": context, "four_way_sha256": qa_sha,
        "source_sha256": source_sha, "source_basis": source_basis,
        "focus": focus, "references": refs, "receipt": target,
    }


def _public(basis: dict) -> dict:
    context = basis["context"]
    return {
        "version": VERSION,
        "status": "LOCAL_B2_B3_FINISHED_AUDIO_ASR_READ_ONLY_PREVIEW",
        "job_id": context["job_id"], "trial_id": context["trial_id"],
        "b2_id": context["b2_id"], "b3_id": context["b3_id"],
        "candidate_index": context["candidate_index"],
        "source_asr_sha256": basis["source_sha256"],
        "four_way_diagnostics_sha256": basis["four_way_sha256"],
        "finished_mp4_sha256": {
            r: context["roles"][r]["final_sha256"] for r in _ROLES
        },
        "focus_by_variant": basis["focus"],
        "expected_fully_contained_source_asr_by_variant": {
            r: len(basis["references"][r]) for r in _ROLES
        },
        "asr_reference_ids_by_variant": {
            r: [x["id"] for x in basis["references"][r]] for r in _ROLES
        },
        "edited_audio_transcribed": False,
        "actual_finished_audio_sent_to_provider": False,
        "public_data_authorization_required": True,
        "transcript_text_exported": False,
        "source_asr_audio_verified": False,
        "edited_asr_audio_verified": False,
        "missing_or_repeated_words_proven": False,
        "human_audio_review_completed": False,
        "editorial_quality_improvement": None,
        "best_variant": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


def _comparison(results: dict, basis: dict) -> dict:
    a, b = (
        results["b2"]["temporal_asr_review"]["source_utterance_alignment"],
        results["b3"]["temporal_asr_review"]["source_utterance_alignment"],
    )
    by_a = {row["source_evidence_id"]: row["status"] for row in a}
    by_b = {row["source_evidence_id"]: row["status"] for row in b}
    shared = sorted(set(by_a) & set(by_b))
    statuses = _WARNINGS | {_NO_WARNING}
    if any(value not in statuses for value in (*by_a.values(), *by_b.values())):
        raise ValueError("LOCAL_B2_B3_ASR_UNKNOWN_STATUS")
    return {
        "shared_source_asr_interval_count": len(shared),
        "b3_newly_fully_included_source_asr_count": len(set(by_b) - set(by_a)),
        "b2_only_fully_included_source_asr_count": len(set(by_a) - set(by_b)),
        "b3_fewer_temporal_warnings_on_shared_intervals": sum(
            by_a[i] in _WARNINGS and by_b[i] == _NO_WARNING for i in shared
        ),
        "b3_more_temporal_warnings_on_shared_intervals": sum(
            by_a[i] == _NO_WARNING and by_b[i] in _WARNINGS for i in shared
        ),
        "b3_newly_included_is_not_proven_spoken": True,
        "b3_fewer_warnings_is_not_proof_of_improvement": True,
        "b3_more_warnings_is_not_proof_of_regression": True,
        "source_asr_clock_is_not_verified_words": True,
        "asr_segmentation_or_hallucination_can_change_counts": True,
        "no_automatic_winner_selected": True,
    }


async def observe_b2_b3_audio_asr(basis: dict, transcriber) -> dict:
    """Public-only, explicit remote ASR; no partial receipt on error/quota."""
    if os.getenv("HOLOCLIP_DATA_CLASS") != "public":
        raise ValueError("LOCAL_B2_B3_ASR_EXPLICIT_PUBLIC_DATA_REQUIRED")
    target = validate_semantic_output(_no_links(basis["receipt"]))
    before = _basis(basis["inputs"], basis["context"]["candidate_index"])
    if _public(before) != _public(basis):
        raise ValueError("LOCAL_B2_B3_ASR_STALE_BEFORE_PROVIDER")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_B2_B3_ASR_FFMPEG_NOT_FOUND")
    result = _public(basis)
    rows = {}
    with tempfile.TemporaryDirectory(prefix="holoclip-b2-b3-asr-") as tmp:
        for role in _ROLES:
            path = _no_links(basis["context"]["roles"][role]["final"])
            digest = basis["context"]["roles"][role]["final_sha256"]
            if _hash(path)[0] != digest:
                raise ValueError("LOCAL_B2_B3_ASR_INPUT_MP4_CHANGED")
            focus = basis["focus"][role]
            start, end = focus["output_start_seconds"], focus["output_end_seconds"]
            audio = Path(tmp) / f"{role}.mp3"
            _read_clip(ffmpeg, path, audio, start=start, end=end)
            if _hash(path)[0] != digest:
                raise ValueError("LOCAL_B2_B3_ASR_CHANGED_DURING_EXTRACTION")
            segments = await transcriber.transcribe_file(audio)
            heard, count = _segments(segments, end - start)
            references = basis["references"][role]
            alignment = temporal_asr_review(
                expected=references, heard=segments,
                output_focus_start_seconds=start, output_focus_end_seconds=end,
                normalize=_normalize,
            )
            rows[role] = {
                "output_focus_start_seconds": start,
                "output_focus_end_seconds": end,
                **_compare_observation(
                    source=references, output_asr=heard, output_segment_count=count,
                ),
                "temporal_asr_review": alignment,
                "model_provenance": _safe_provenance(
                    getattr(transcriber, "last_provenance", None)
                ),
                "human_auditory_check_completed": False,
            }
            audio.unlink()
    after = _basis(basis["inputs"], basis["context"]["candidate_index"])
    _ensure_same_before_after(basis["context"], after["context"])
    if (_public(after) != _public(basis)
            or after["four_way_sha256"] != basis["four_way_sha256"]
            or after["source_sha256"] != basis["source_sha256"]
            or after["references"] != basis["references"]):
        raise ValueError("LOCAL_B2_B3_ASR_CHANGED_DURING_PROVIDER")
    result.update({
        "status": "LOCAL_B2_B3_ASR_DISCREPANCIES_NOT_VERIFIED_SPEECH",
        "edited_audio_transcribed": True,
        "actual_finished_audio_sent_to_provider": True,
        "variants": rows,
        "comparison": _comparison(rows, basis),
    })
    save_semantic_review(result, target)
    return result


def prepare_b2_b3_audio_asr(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
) -> dict:
    if (type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)
            or type(candidate_index) is not int or candidate_index not in (0, 1)):
        raise ValueError("LOCAL_B2_B3_ASR_ARGUMENTS_INVALID")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(p)) for p in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    )
    jobs, _ = _completed_jobs(queue, job_id=job_id, max_jobs=1)
    if not jobs:
        return {
            "status": "LOCAL_B2_B3_ASR_NO_COMPLETED_JOB", "ready": False,
            "production_ready": False,
        }
    inputs = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": jobs[0],
        "proposal_index": proposal_index, "refinement_index": refinement_index,
    }
    basis = _basis(inputs, candidate_index)
    basis["inputs"] = inputs
    return basis
