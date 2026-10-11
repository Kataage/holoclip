"""Local acoustic corroboration of three-way source-timed dialogue hypotheses.

Never verifies words, speakers, meaning or creative quality. Uses bounded FFmpeg
volume measurements on SHA-pinned *finished* A/B/B2, not a speech recognizer.
The prior separately decoded video scan is referenced, not reinterpreted.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil

from holoclip.render_audio_finish import _measure
from holoclip.render_cut_extension_three_way_dialogue import (
    _pack, prepare_three_way_dialogue,
)
from holoclip.render_cut_extension_three_way_diagnostics import _source_evidence
from holoclip.render_cut_extension_three_way_triage import _focus_windows, _load_diagnostics
from holoclip.render_plan import _hash, _no_links
from holoclip.render_same_source_semantic_review import save_semantic_review

VERSION = "holoclip-three-way-source-asr-local-audio-probe-v1"
_ROLES = ("a", "b", "b2")
_MAX_ASR_PROBES = 3
_MAX_DURATION = 180.0
_SAMPLE_SECONDS = 0.6


def prepare_three_way_acoustic(**kwargs) -> dict:
    """Reconstruct three-way proofs from local canonical data, not a user JSON."""
    dialogue = prepare_three_way_dialogue(**kwargs)
    output = _no_links(dialogue["receipt"].with_name(
        f"{dialogue['review']['diagnostics']['finished']['b2']['output'].stem}"
        ".three-way-acoustic.json"
    ))
    return {"dialogue": dialogue, "receipt": output}


def _context(prepared: dict) -> tuple[dict, dict, dict, str]:
    dialogue = prepared["dialogue"]
    review = dialogue["review"]
    observed = _source_evidence(review["diagnostics"])
    diagnostic, digest, _ = _load_diagnostics(review, observed)
    pack = _pack(dialogue)
    if (digest != pack["three_way_diagnostic_sha256"]
            or pack["source_asr_sha256"] != observed["asr_sha256"]):
        raise ValueError("LOCAL_ACOUSTIC_LINEAGE_CHANGED")
    return observed, diagnostic, pack, digest


def _samples(role: str, focus: dict, excerpts: list[dict], duration: float) -> list[dict]:
    """Choose bounded interior *output-clock* ranges; no source seek guesses."""
    if not math.isfinite(duration) or not 0 < duration <= _MAX_DURATION:
        raise ValueError("LOCAL_ACOUSTIC_DURATION_INVALID")
    f = focus[role]
    start, end = f["output_start_seconds"], f["output_end_seconds"]
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end)
            or not 0 <= start < end <= duration + 0.05):
        raise ValueError("LOCAL_ACOUSTIC_FOCUS_INVALID")
    selected = [
        ("FOCUS_ENTRY_NOT_SPEECH_PROOF", None, start, min(end, start + _SAMPLE_SECONDS)),
        ("FOCUS_EXIT_NOT_SPEECH_PROOF", None, max(start, end - _SAMPLE_SECONDS), end),
    ]
    for item in excerpts[:_MAX_ASR_PROBES]:
        lo, hi = item["output_start_seconds"], item["output_end_seconds"]
        if (type(lo) not in (int, float) or type(hi) not in (int, float)
                or not math.isfinite(lo) or not math.isfinite(hi)
                or not 0 <= lo < hi <= duration + 0.05
                or item.get("fully_contained_in_clip") is not True):
            raise ValueError("LOCAL_ACOUSTIC_ASR_CLOCK_INVALID")
        middle = (lo + hi) / 2
        span = min(_SAMPLE_SECONDS, hi - lo)
        selected.append((
            "SOURCE_ASR_CLOCK_NOT_AUDIBLE_TEXT_PROOF",
            item["evidence_id"], max(lo, middle - span / 2), min(hi, middle + span / 2),
        ))
    result = []
    for kind, evidence_id, lo, hi in selected:
        lo = max(0.0, min(float(lo), duration))
        hi = max(0.0, min(float(hi), duration))
        if not 0.06 <= hi - lo <= _SAMPLE_SECONDS + 0.002:
            raise ValueError("LOCAL_ACOUSTIC_PROBE_WINDOW_INVALID")
        result.append({
            "kind": kind,
            "evidence_id": evidence_id,
            "output_start_seconds": round(lo, 3),
            "output_end_seconds": round(hi, 3),
        })
    return result


def _public(prepared: dict, observed: dict, diagnostic: dict,
            pack: dict, digest: str) -> dict:
    role_reports = {}
    focus = _focus_windows(prepared["dialogue"]["review"], observed)
    for role in _ROLES:
        original_timeline = observed["roles"][role]["timeline"]
        sample_plan = _samples(
            role,
            focus,
            pack["variants"][role]["excerpts"],
            original_timeline.estimated_duration,
        )
        metric = diagnostic["variants"][role]["technical_diagnostic"]
        role_reports[role] = {
            "final_video_sha256": observed["roles"][role]["final_sha256"],
            "audio_samples": [{**item, "mean_dbfs": None, "peak_dbfs": None,
                               "near_silent_energy_hint": None} for item in sample_plan],
            "asr_partial_utterance_count_unverified":
                pack["variants"][role]["partial_utterances_near_focus_count"],
            "previously_scanned_video_black_interval_count":
                metric["black_interval_count"],
            "previously_scanned_video_freeze_interval_count":
                metric["freeze_interval_count"],
            "new_video_frame_analysis_performed": False,
            "spoken_words_verified": False,
        }
    return {
        "version": VERSION,
        "status": "LOCAL_ACOUSTIC_PROBE_PREVIEW_NO_DECODING",
        "job_id": pack["job_id"],
        "trial_id": pack["trial_id"],
        "b2_id": pack["b2_id"],
        "source_asr_sha256": pack["source_asr_sha256"],
        "source_dialogue_evidence_sha256": pack["review_plan_sha256"],
        "three_way_diagnostic_sha256": digest,
        "source_receipt_sha256": observed["receipt_hashes"],
        "variants": role_reports,
        "measured_audio_sample_count": 0,
        "previous_video_diagnostics_only_not_visual_semantics": True,
        "silence_does_not_imply_incomplete_speech": True,
        "non_silence_does_not_prove_spoken_words": True,
        "asr_audio_content_independently_verified": False,
        "subtitle_to_speech_verified": False,
        "human_narrative_quality_verified": False,
        "editorial_quality_improvement": None,
        "best_variant": None,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
        "youtube_contacted": False,
    }


def run_three_way_acoustic(prepared: dict, *, execute: bool = False) -> dict:
    """Measure finite local samples and save create-only after rechecking proofs."""
    observed, diagnostic, pack, digest = _context(prepared)
    report = _public(prepared, observed, diagnostic, pack, digest)
    if not execute:
        return report
    target = _no_links(prepared["receipt"])
    if target.exists() or not target.parent.is_dir():
        raise ValueError("LOCAL_ACOUSTIC_OUTPUT_EXISTS_OR_UNSAFE")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("LOCAL_ACOUSTIC_FFMPEG_UNAVAILABLE")
    for role in _ROLES:
        file = _no_links(observed["roles"][role]["final"])
        expected = observed["roles"][role]["final_sha256"]
        if _hash(file)[0] != expected:
            raise ValueError("LOCAL_ACOUSTIC_FINISHED_MEDIA_CHANGED")
        for sample in report["variants"][role]["audio_samples"]:
            start, end = sample["output_start_seconds"], sample["output_end_seconds"]
            if not 0.06 <= end - start <= _SAMPLE_SECONDS + 0.002:
                raise ValueError("LOCAL_ACOUSTIC_PROBE_WINDOW_INVALID")
            mean, peak = _measure(ffmpeg, file, start, end)
            if (not all(math.isfinite(x) for x in (mean, peak))
                    or not -100.0 <= mean <= 6.0 or not -100.0 <= peak <= 6.0
                    or peak < mean - .5):
                raise ValueError("LOCAL_ACOUSTIC_MEASUREMENT_UNTRUSTED")
            sample["mean_dbfs"] = round(mean, 2)
            sample["peak_dbfs"] = round(peak, 2)
            sample["near_silent_energy_hint"] = peak <= -48 or mean <= -50
            report["measured_audio_sample_count"] += 1

    # No result may be committed if any source, A/B/B2 media, current ASR,
    # subtitles, or evidence JSON changed while FFmpeg was decoding audio.
    later_observed, later_diag, later_pack, later_hash = _context(prepared)
    if (later_hash != digest or later_diag != diagnostic or later_pack != pack
            or later_observed["receipt_hashes"] != observed["receipt_hashes"]
            or later_observed["sibling_hashes"] != observed["sibling_hashes"]
            or later_observed["asr_sha256"] != observed["asr_sha256"]):
        raise ValueError("LOCAL_ACOUSTIC_EVIDENCE_CHANGED_DURING_SCAN")
    for role in _ROLES:
        old, latest = observed["roles"][role], later_observed["roles"][role]
        if (old["base_sha256"] != latest["base_sha256"]
                or old["final_sha256"] != latest["final_sha256"]
                or old["caption_sha256"] != latest["caption_sha256"]
                or _hash(old["base"])[0] != old["base_sha256"]
                or _hash(old["final"])[0] != old["final_sha256"]):
            raise ValueError("LOCAL_ACOUSTIC_MEDIA_CHANGED_DURING_SCAN")
    report["status"] = "LOCAL_ACOUSTIC_MEASURED_SIGNALS_NOT_SPEECH_PROOF"
    save_semantic_review(report, target)
    return report
