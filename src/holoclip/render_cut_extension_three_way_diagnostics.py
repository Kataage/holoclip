"""Three-way *measured* local edit diagnostics for already finished A/B/B2.

This is a technical review aid, not a quality ranking or a production gate.
All media and optional SRTs are independently checked, left untouched and
checked again after decoding. No downloads, OCR, models or remote requests.
"""
from __future__ import annotations

import hashlib
import json
import math

from holoclip.render_cut_extension_finish_ab import (
    _SUCCESS, _stage_paths, _verify_stages,
)
from holoclip.render_cut_extension_speech_audit import _load_matched
from holoclip.render_cut_extension_speech_b2_finish import (
    VERSION as B2_FINISH_VERSION, _b2_receipt,
    prepare_speech_b2_finishing,
)
from holoclip.render_cut_extension_speech_refine import suggest_speech_boundary_refinements
from holoclip.render_plan import _hash, _no_links
from holoclip.render_quality_diagnostics import _run
from holoclip.render_same_source_semantic_review import save_semantic_review

VERSION = "holoclip-speech-complete-three-way-diagnostics-v1"
_MAX_RECEIPT_BYTES = 65536
_FINISHED_STATUS = "LOCAL_B2_FINISH_STAGES_TECHNICALLY_COMPARABLE"


def prepare_three_way_diagnostics(**kwargs) -> dict:
    """Only recover a canonical, previously finished local A/B/B2 lineage."""
    finished = prepare_speech_b2_finishing(**kwargs)
    output = _no_links(finished["finished_receipt"].with_name(
        f"{finished['b2']['output'].stem}.three-way-diagnostics.json"
    ))
    return {"finished": finished, "receipt": output}


def _finished_b2_receipt(context: dict, b2_raw_sha: str) -> tuple[dict, str]:
    target = _no_links(context["finished_receipt"])
    if not target.is_file() or not 0 < target.stat().st_size <= _MAX_RECEIPT_BYTES:
        raise ValueError("LOCAL_THREE_WAY_B2_FINISH_RECEIPT_MISSING")
    raw = target.read_bytes()
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_THREE_WAY_B2_FINISH_RECEIPT_INVALID") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LOCAL_THREE_WAY_B2_FINISH_RECEIPT_INVALID")
    b2 = context["b2"]
    matched = b2["prepared"]
    trial = matched["trial"]
    needed = {
        "version": B2_FINISH_VERSION,
        "status": _FINISHED_STATUS,
        "job_id": trial["job_id"],
        "trial_id": trial["trial_id"],
        "b2_id": b2["b2_id"],
        "b2_render_receipt_sha256": b2_raw_sha,
        "matched_ab_receipt_sha256": b2["proposal"]["matched_receipt_sha256"],
        "source_asr_sha256": b2["proposal"]["source_asr_sha256"],
        "baseline_a_final_sha256": b2["proposal"]["baseline_final_sha256"],
        "trial_b_final_sha256": b2["proposal"]["trial_final_sha256"],
        "b2_original_mp4_sha256": _hash(b2["output"])[0],
        "stage_comparable_to_a_and_b": True,
        "b2_caption_audio_finishing_matched_to_a_b": True,
        "source_asr_linguistically_verified": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }
    if any(parsed.get(key) != value for key, value in needed.items()):
        raise ValueError("LOCAL_THREE_WAY_B2_FINISH_LINEAGE_CHANGED")
    if (
        type(parsed.get("b2_measured_final_seconds")) not in (int, float)
        or not math.isfinite(parsed["b2_measured_final_seconds"])
        or abs(parsed["b2_measured_final_seconds"] -
               b2["timeline"].estimated_duration) > 0.5
    ):
        raise ValueError("LOCAL_THREE_WAY_B2_FINISH_DURATION_UNTRUSTED")
    return parsed, hashlib.sha256(raw).hexdigest()


def _source_evidence(prepared: dict) -> dict:
    """Rehash all three finished videos, optional artifacts, and receipts."""
    context = prepared["finished"]
    b2 = context["b2"]
    matched = b2["prepared"]
    trial = matched["trial"]
    original = trial["original"]
    stages = matched["baseline_stages"]
    base_a = _no_links(original["baseline_media"])
    base_b = _no_links(trial["output"])
    base_b2 = _no_links(b2["output"])

    ab, a_final, b_final, ab_sha = _load_matched(matched)
    if ab_sha != b2["proposal"]["matched_receipt_sha256"]:
        raise ValueError("LOCAL_THREE_WAY_AB_RECEIPT_CHANGED")
    technical, technical_sha = _b2_receipt(context)
    if technical["matched_ab_receipt_sha256"] != ab_sha:
        raise ValueError("LOCAL_THREE_WAY_B2_RENDER_LINEAGE_CHANGED")
    b2_finish, finished_sha = _finished_b2_receipt(context, technical_sha)
    evidence = suggest_speech_boundary_refinements(matched)
    if evidence != b2["proposal"]:
        raise ValueError("LOCAL_THREE_WAY_ASR_PROPOSAL_CHANGED")

    stage_hashes = _verify_stages(
        _stage_paths(base_a), stages, original["baseline_sha256"]
    )
    if ab.get("baseline_stage_hashes") != stage_hashes:
        raise ValueError("LOCAL_THREE_WAY_BASELINE_STAGES_CHANGED")

    # B's intermediate hashes were not persisted in #241; never call
    # them earlier signed evidence. Snapshot the bytes here and recheck
    # them after actual decoding.
    stage_a = _stage_paths(base_a)
    stage_b = _stage_paths(base_b)
    stage_b2 = _stage_paths(base_b2)
    sibling_hashes = {}
    for role, paths in (("b", stage_b), ("b2", stage_b2)):
        for name in ("audio", "caption", "burnin"):
            file = paths[name]
            successful = stages[name]["successful"]
            expected = (
                b2_finish.get({
                    "audio": "b2_audio_status",
                    "caption": "b2_caption_status",
                    "burnin": "b2_burnin_status",
                }[name]) if role == "b2" else (
                    ab.get({
                        "audio": "trial_audio_status",
                        "caption": "trial_caption_status",
                        "burnin": "trial_burnin_status",
                    }[name])
                )
            )
            if expected != (_SUCCESS[name] if successful else "NOT_REQUESTED"):
                raise ValueError("LOCAL_THREE_WAY_PROCESSING_MISMATCH")
            if successful:
                if not file.is_file():
                    raise ValueError("LOCAL_THREE_WAY_SIBLING_MISSING")
                sibling_hashes[f"{role}_{name}"] = _hash(file)[0]
            elif file.exists():
                raise ValueError("LOCAL_THREE_WAY_UNEXPECTED_SIBLING")
    final_kind = (
        "burnin" if stages["burnin"]["successful"] else
        "audio" if stages["audio"]["successful"] else "base"
    )
    b2_final = stage_b2[final_kind]
    final_sha = b2_finish.get("b2_final_sha256")
    if (b2_finish.get("b2_final_filename") != b2_final.name
            or not isinstance(final_sha, str)
            or _hash(b2_final)[0] != final_sha):
        raise ValueError("LOCAL_THREE_WAY_B2_FINAL_CHANGED")
    if (
        _hash(base_a)[0] != original["baseline_sha256"]
        or _hash(base_b)[0] != ab["trial_source_mp4_sha256"]
        or _hash(base_b2)[0] != technical["b2_mp4_sha256"]
    ):
        raise ValueError("LOCAL_THREE_WAY_BASE_MEDIA_CHANGED")

    roles = {
        "a": {
            "base": base_a, "final": a_final,
            "timeline": original["baseline_timeline"],
            "base_sha256": original["baseline_sha256"],
            "final_sha256": ab["baseline_final_sha256"],
            "caption_sha256": stage_hashes.get("caption"),
        },
        "b": {
            "base": base_b, "final": b_final,
            "timeline": trial["trial"],
            "base_sha256": ab["trial_source_mp4_sha256"],
            "final_sha256": ab["trial_final_sha256"],
            "caption_sha256": sibling_hashes.get("b_caption"),
        },
        "b2": {
            "base": base_b2, "final": b2_final,
            "timeline": b2["timeline"],
            "base_sha256": technical["b2_mp4_sha256"],
            "final_sha256": final_sha,
            "caption_sha256": sibling_hashes.get("b2_caption"),
        },
    }
    return {
        "roles": roles,
        "receipt_hashes": {
            "matched_ab": ab_sha,
            "render_b2": technical_sha,
            "finished_b2": finished_sha,
        },
        "sibling_hashes": sibling_hashes,
        "asr_sha256": b2["proposal"]["source_asr_sha256"],
        "boundary": {
            "original_a_crossings": None,
            "trial_b_crossings": b2["choice"]["old_trial_boundary_asr_crossings"],
            "proposal_b2_crossings":
                b2["choice"]["proposed_boundary_asr_crossings"],
            "asr_only_not_verified_audio": True,
        },
        "job_id": trial["job_id"],
        "trial_id": trial["trial_id"],
        "b2_id": b2["b2_id"],
    }


def _public(observed: dict) -> dict:
    samples = {}
    for key, role in observed["roles"].items():
        samples[key] = {
            "base_sha256": role["base_sha256"],
            "final_sha256": role["final_sha256"],
            "technical_diagnostic": None,
        }
    return {
        "version": VERSION,
        "status": "LOCAL_THREE_WAY_DIAGNOSTICS_PREVIEW_ONLY",
        "job_id": observed["job_id"],
        "trial_id": observed["trial_id"],
        "b2_id": observed["b2_id"],
        "source_asr_sha256": observed["asr_sha256"],
        "receipt_sha256": observed["receipt_hashes"],
        "source_clock_boundary": observed["boundary"],
        "variants": samples,
        "comparison": None,
        "actual_media_scanned": False,
        "source_asr_linguistically_verified": False,
        "editorial_quality_improvement": None,
        "human_rating_available": False,
        "human_editorial_qa_completed": False,
        "auto_adoption_allowed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False,
        "youtube_contacted": False,
    }


def run_three_way_diagnostics(prepared: dict, *, execute: bool = False) -> dict:
    """No decoder in preview; execute stores immutable, fail-closed signals."""
    observed = _source_evidence(prepared)
    report = _public(observed)
    if not execute:
        return report
    target = _no_links(prepared["receipt"])
    if target.exists() or not target.parent.is_dir():
        raise ValueError("LOCAL_THREE_WAY_REPORT_EXISTS_OR_UNSAFE")

    measurements = {}
    for name, role in observed["roles"].items():
        measured = _run(
            base=role["base"], base_sha256=role["base_sha256"],
            final=role["final"], final_sha256=role["final_sha256"],
            timeline=role["timeline"],
            caption_sha256=role["caption_sha256"],
        )
        if not measured["status"].startswith("LOCAL_EDIT_DIAGNOSTIC_MEASURED_"):
            raise ValueError("LOCAL_THREE_WAY_SCAN_INCOMPLETE")
        measurements[name] = measured

    after = _source_evidence(prepared)
    if (after["receipt_hashes"] != observed["receipt_hashes"]
            or after["sibling_hashes"] != observed["sibling_hashes"]
            or after["boundary"] != observed["boundary"]
            or after["asr_sha256"] != observed["asr_sha256"]):
        raise ValueError("LOCAL_THREE_WAY_SOURCE_CHANGED_DURING_SCAN")
    for name in ("a", "b", "b2"):
        before = observed["roles"][name]
        current = after["roles"][name]
        if (
            current["base_sha256"] != before["base_sha256"]
            or current["final_sha256"] != before["final_sha256"]
            or current["caption_sha256"] != before["caption_sha256"]
            or _hash(before["base"])[0] != before["base_sha256"]
            or _hash(before["final"])[0] != before["final_sha256"]
        ):
            raise ValueError("LOCAL_THREE_WAY_MEDIA_CHANGED_DURING_SCAN")

    for name in ("a", "b", "b2"):
        report["variants"][name]["technical_diagnostic"] = measurements[name]
    report["comparison"] = {
        "b2_minus_b_review_flag_count": (
            measurements["b2"]["flag_count"] - measurements["b"]["flag_count"]
        ),
        "b2_minus_a_review_flag_count": (
            measurements["b2"]["flag_count"] - measurements["a"]["flag_count"]
        ),
        "flag_count_is_not_creative_quality_score": True,
        "audio_dbfs_is_not_speech_intelligibility": True,
        "subtitle_cps_is_not_transcript_accuracy": True,
        "source_asr_clock_is_not_verified_dialogue": True,
        "no_automatic_winner_selected": True,
    }
    report["status"] = "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY"
    report["actual_media_scanned"] = True
    save_semantic_review(report, target)
    return report
