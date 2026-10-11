"""Prioritize *what to inspect*, never which local A/B/B2 cut is best.

Uses an already persisted, SHA-linked three-way technical report plus
canonical source-clock timelines. No model, decoding, network or edit occurs.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.render_cut_extension_three_way_diagnostics import (
    VERSION as DIAGNOSTIC_VERSION,
    _source_evidence,
    prepare_three_way_diagnostics,
)
from holoclip.render_plan import _no_links
from holoclip.render_quality_review import candidates_for_review
from holoclip.render_same_source_semantic_review import save_semantic_review

VERSION = "holoclip-three-way-editorial-review-triage-v1"
_MAX_DIAGNOSTIC_BYTES = 1024 * 1024
_MAX_OBSERVATIONS_PER_VARIANT = 100
_MAX_REVIEW_TARGETS = 72
_DIAGNOSTIC_STATUS = "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY"
_PRIORITY_KIND = {
    "blackout": 1,
    "audio_peak": 1,
    "audio_low_mean": 1,
    "freeze": 2,
    "fast_subtitle": 2,
    "wide_subtitle": 2,
}


def prepare_three_way_review(**kwargs) -> dict:
    """Find existing three-way evidence through current queue and A/B/B2."""
    prior = prepare_three_way_diagnostics(**kwargs)
    output = _no_links(prior["receipt"].with_name(
        f"{prior['finished']['b2']['output'].stem}.three-way-review.json"
    ))
    return {"diagnostics": prior, "receipt": output}


def _load_diagnostics(prepared: dict, observed: dict) -> tuple[dict, str, dict]:
    """Reject stale source, media, policy/ASR and inconsistent measured report."""
    path = _no_links(prepared["diagnostics"]["receipt"])
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_DIAGNOSTIC_BYTES:
        raise ValueError("LOCAL_REVIEW_TRIAGE_DIAGNOSTIC_MISSING")
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_REVIEW_TRIAGE_DIAGNOSTIC_INVALID") from exc
    if not isinstance(data, dict):
        raise ValueError("LOCAL_REVIEW_TRIAGE_DIAGNOSTIC_INVALID")
    required = {
        "version": DIAGNOSTIC_VERSION,
        "status": _DIAGNOSTIC_STATUS,
        "job_id": observed["job_id"],
        "trial_id": observed["trial_id"],
        "b2_id": observed["b2_id"],
        "receipt_sha256": observed["receipt_hashes"],
        "source_asr_sha256": observed["asr_sha256"],
        "source_clock_boundary": observed["boundary"],
        "actual_media_scanned": True,
        "source_asr_linguistically_verified": False,
        "human_editorial_qa_completed": False,
        "editorial_quality_improvement": None,
        "auto_adoption_allowed": False,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }
    if any(data.get(key) != value for key, value in required.items()):
        raise ValueError("LOCAL_REVIEW_TRIAGE_DIAGNOSTIC_LINEAGE_CHANGED")
    comp = data.get("comparison")
    if not isinstance(comp, dict) or comp.get("no_automatic_winner_selected") is not True:
        raise ValueError("LOCAL_REVIEW_TRIAGE_DIAGNOSTIC_RANKING_UNTRUSTED")
    variants = data.get("variants")
    if not isinstance(variants, dict) or set(variants) != {"a", "b", "b2"}:
        raise ValueError("LOCAL_REVIEW_TRIAGE_VARIANTS_INVALID")
    candidates = {}
    counts = {}
    for name, role in observed["roles"].items():
        row = variants.get(name)
        if (not isinstance(row, dict)
                or row.get("base_sha256") != role["base_sha256"]
                or row.get("final_sha256") != role["final_sha256"]):
            raise ValueError("LOCAL_REVIEW_TRIAGE_VARIANT_LINEAGE_CHANGED")
        metric = row.get("technical_diagnostic")
        if (not isinstance(metric, dict)
                or not isinstance(metric.get("status"), str)
                or not metric["status"].startswith("LOCAL_EDIT_DIAGNOSTIC_MEASURED_")
                or metric.get("base_sha256") != role["base_sha256"]
                or metric.get("final_sha256") != role["final_sha256"]
                or type(metric.get("flag_count")) is not int
                or not 0 <= metric["flag_count"] <= 600
                or type(metric.get("subtitle_checked")) is not bool
                or metric["subtitle_checked"] != (role["caption_sha256"] is not None)):
            raise ValueError("LOCAL_REVIEW_TRIAGE_METRICS_INVALID")
        observations = metric.get("review_observations")
        if (not isinstance(observations, list)
                or len(observations) > _MAX_OBSERVATIONS_PER_VARIANT):
            raise ValueError("LOCAL_REVIEW_TRIAGE_OBSERVATIONS_INVALID")
        # Existing validator maps each warning to canonical output cut IDs,
        # and checks negative/nonfinite/out-of-range timestamps and cue IDs.
        candidates[name] = candidates_for_review(role["timeline"], observations)
        counts[name] = metric["flag_count"]
    if (comp.get("b2_minus_b_review_flag_count") != counts["b2"] - counts["b"]
            or comp.get("b2_minus_a_review_flag_count") != counts["b2"] - counts["a"]):
        raise ValueError("LOCAL_REVIEW_TRIAGE_COMPARISON_UNTRUSTED")
    return data, hashlib.sha256(raw).hexdigest(), candidates


def _focus_windows(prepared: dict, observed: dict) -> dict:
    """Map edited *source* cut to matching A/B/B2 *output* positions."""
    b2 = prepared["diagnostics"]["finished"]["b2"]
    trial = b2["prepared"]["trial"]
    original = trial["original"]["baseline_timeline"]
    index = trial["source_cut_index"]
    action = trial["trial_action"]
    if type(index) is not int or action not in {"EXTEND_START", "EXTEND_END"}:
        raise ValueError("LOCAL_REVIEW_TRIAGE_CUT_INVALID")
    result = {}
    for name, timeline in (
        ("a", original), ("b", trial["trial"]), ("b2", b2["timeline"]),
    ):
        ordered = sorted(timeline.segments, key=lambda s: s.output_order)
        baseline = sorted(original.segments, key=lambda s: s.output_order)
        if not 0 <= index < len(ordered) or len(ordered) != len(baseline):
            raise ValueError("LOCAL_REVIEW_TRIAGE_CUT_INVALID")
        segment = ordered[index]
        previous = baseline[index]
        start = sum(x.source_end - x.source_start for x in ordered[:index])
        if action == "EXTEND_START":
            seam = start + (previous.source_start - segment.source_start)
        else:
            seam = start + (previous.source_end - segment.source_start)
        duration = timeline.estimated_duration
        if (not math.isfinite(seam) or not 0 <= seam <= duration
                or not 0 < duration <= 180):
            raise ValueError("LOCAL_REVIEW_TRIAGE_SEAM_UNTRUSTED")
        result[name] = {
            "source_cut_index": index,
            "output_start_seconds": round(max(0.0, seam - 2.0), 3),
            "output_end_seconds": round(min(duration, seam + 3.0), 3),
            "focus_source_clock_not_verified_dialogue": True,
            "segment_id": segment.id,
        }
    return result


def _build_targets(focus: dict, candidates: dict) -> list[dict]:
    targets = []
    # Examine the *same semantic transition* in each variant, instead of
    # comparing unrelated absolute timestamps on changed-length videos.
    for variant in ("a", "b", "b2"):
        window = focus[variant]
        targets.append({
            "priority": 0,
            "variant": variant,
            "kind": "VERIFY_EDIT_TRANSITION_AND_SPEECH_COMPLETENESS",
            "output_start_seconds": window["output_start_seconds"],
            "output_end_seconds": window["output_end_seconds"],
            "source_cut_index": window["source_cut_index"],
            "segment_ids": [window["segment_id"]],
            "question_code": "LISTEN_FOR_COMPLETE_UTTERANCE_AND_NATURAL_CUT",
            "grounding": "SOURCE_ASR_CLOCK_ONLY_REQUIRES_LISTENING",
            "editorial_result": None,
        })
    for variant in ("a", "b", "b2"):
        for item in candidates[variant]:
            kind = item["kind"]
            targets.append({
                "priority": _PRIORITY_KIND[kind],
                "variant": variant,
                "kind": kind,
                "output_start_seconds": item["start_seconds"],
                "output_end_seconds": item["end_seconds"],
                "segment_ids": item["segment_ids"],
                "source_cut_index": None,
                "question_code": item["recommendation"],
                "grounding": "DECODED_LOCAL_TECHNICAL_WARNING_NOT_EDIT_ERROR",
                "editorial_result": None,
                **({"cue_number": item["cue_number"]}
                   if "cue_number" in item else {}),
            })
    if len(targets) > _MAX_REVIEW_TARGETS:
        raise ValueError("LOCAL_REVIEW_TRIAGE_TARGET_LIMIT")
    targets.sort(key=lambda item: (
        item["priority"], {"a": 0, "b": 1, "b2": 2}[item["variant"]],
        item["output_start_seconds"], item["kind"],
    ))
    for position, item in enumerate(targets, start=1):
        item["review_order"] = position
    return targets


def run_three_way_review(prepared: dict, *, save: bool = False) -> dict:
    """Plan review questions only; source and media remain unchanged."""
    observed = _source_evidence(prepared["diagnostics"])
    diagnostics, digest, candidates = _load_diagnostics(prepared, observed)
    focus = _focus_windows(prepared, observed)
    targets = _build_targets(focus, candidates)
    plan = {
        "version": VERSION,
        "status": "LOCAL_THREE_WAY_REVIEW_TARGETS_UNRATED",
        "job_id": observed["job_id"],
        "trial_id": observed["trial_id"],
        "b2_id": observed["b2_id"],
        "three_way_diagnostic_sha256": digest,
        "receipt_sha256": observed["receipt_hashes"],
        "source_asr_sha256": observed["asr_sha256"],
        "source_clock_boundary": observed["boundary"],
        "review_focus_per_variant": focus,
        "review_target_count": len(targets),
        "review_targets": targets,
        "cross_variant_editorial_questions": [
            "DOES_THE_VIEWER_UNDERSTAND_THE_SETUP_WITHOUT_SOURCE_CONTEXT",
            "DOES_THE_PAYOFF_OR_REACTION_COMPLETE_WITHOUT_ABRUPT_CUT",
            "DOES_THE_PAUSE_AND_PACING_STAY_NATURAL",
            "ARE_SPOKEN_WORDS_AND_SUBTITLES_FAITHFUL_WHEN_LISTENED",
        ],
        "review_order_is_not_variant_ranking": True,
        "in_sample_reference_not_independent_gold": True,
        "source_asr_linguistically_verified": False,
        "human_editorial_qa_completed": False,
        "editorial_quality_improvement": None,
        "best_variant": None,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
        "youtube_contacted": False,
    }
    if save:
        output = _no_links(prepared["receipt"])
        if output.exists() or not output.parent.is_dir():
            raise ValueError("LOCAL_REVIEW_TRIAGE_OUTPUT_EXISTS_OR_UNSAFE")
        # Refuse to record a stale plan, even if evidence changed during
        # source-clock mapping or action ordering. No output is overwritten.
        fresh = _source_evidence(prepared["diagnostics"])
        again, after_sha, _ = _load_diagnostics(prepared, fresh)
        if (after_sha != digest or again != diagnostics
                or fresh["receipt_hashes"] != observed["receipt_hashes"]
                or fresh["sibling_hashes"] != observed["sibling_hashes"]
                or fresh["asr_sha256"] != observed["asr_sha256"]):
            raise ValueError("LOCAL_REVIEW_TRIAGE_SOURCE_CHANGED")
        save_semantic_review(plan, output)
    return plan
