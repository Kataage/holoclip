"""Offline speech-boundary and local audio evidence for finished extension A/B.

This diagnoses clock-time ASR coverage and measured local audio on already
rendered, SHA-verified A/B. It NEVER infers narrative or comedic improvement.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from pathlib import Path

from holoclip.render_audio_finish import _measure
from holoclip.render_cut_extension_finish_ab import (
    VERSION as MATCHED_VERSION,
    _stage_paths,
    _verify_stages,
    prepare_matched_finishing_trial,
)
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_resolver import _canonical_assets
from holoclip.render_same_source_transcript_context import _read_transcript

VERSION = "holoclip-finished-extension-speech-audio-audit-v1"
_MAX_RECEIPT = 65536
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_EPSILON = 0.001


def _load_matched(prepared: dict) -> tuple[dict, Path, Path, str]:
    """Re-derive lineage and verify final A/B files before reading media."""
    plan = prepared["trial"]
    original = plan["original"]
    receipt_file = _no_links(prepared["matched_receipt"])
    if (not receipt_file.is_file()
            or not 0 < receipt_file.stat().st_size <= _MAX_RECEIPT):
        raise ValueError("LOCAL_SPEECH_AB_MATCHED_RECEIPT_MISSING")
    raw = receipt_file.read_bytes()
    receipt_sha = hashlib.sha256(raw).hexdigest()
    try:
        saved = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_SPEECH_AB_RECEIPT_INVALID") from exc
    if not isinstance(saved, dict):
        raise ValueError("LOCAL_SPEECH_AB_RECEIPT_INVALID")
    requirements = {
        "version": MATCHED_VERSION,
        "status": "LOCAL_MATCHED_AB_STAGES_TECHNICALLY_COMPARABLE",
        "job_id": plan["job_id"],
        "trial_id": plan["trial_id"],
        "source_stream_sha256": plan["source_stream_sha256"],
        "trial_timeline_sha256": plan["trial_timeline_sha256"],
        "local_source_asr_sha256": plan["local_source_asr_sha256"],
        "baseline_source_mp4_sha256": original["baseline_sha256"],
        "trial_source_mp4_sha256": None,
        "stage_comparable": True,
        "editorial_comparable": False,
        "semantic_quality_measured": False,
        "auto_adoption_allowed": False,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
    }
    for key, expected in requirements.items():
        if key == "trial_source_mp4_sha256":
            if not isinstance(saved.get(key), str) or not _SHA.fullmatch(
                saved[key]
            ):
                raise ValueError("LOCAL_SPEECH_AB_RECEIPT_LINEAGE_CHANGED")
        elif saved.get(key) != expected:
            raise ValueError("LOCAL_SPEECH_AB_RECEIPT_LINEAGE_CHANGED")
    stage_paths = _stage_paths(original["baseline_media"])
    stages = prepared["baseline_stages"]
    if _hash(original["baseline_media"])[0] != original["baseline_sha256"]:
        raise ValueError("LOCAL_SPEECH_AB_ORIGINAL_CHANGED")
    verified = _verify_stages(stage_paths, stages, original["baseline_sha256"])
    if saved.get("baseline_stage_hashes") != verified:
        raise ValueError("LOCAL_SPEECH_AB_BASELINE_STAGES_CHANGED")
    final_kind = (
        "burnin" if stages["burnin"]["successful"] else
        "audio" if stages["audio"]["successful"] else "base"
    )
    baseline = stage_paths[final_kind]
    if (saved.get("baseline_final_filename") != baseline.name
            or saved.get("baseline_final_sha256") != verified[final_kind]):
        raise ValueError("LOCAL_SPEECH_AB_BASELINE_FINAL_CHANGED")
    b_paths = _stage_paths(plan["output"])
    trial = b_paths[final_kind]
    if (saved.get("trial_final_filename") != trial.name
            or not isinstance(saved.get("trial_final_sha256"), str)
            or not _SHA.fullmatch(saved["trial_final_sha256"])
            or not trial.is_file()
            or _hash(trial)[0] != saved["trial_final_sha256"]
            or _hash(plan["output"])[0] != saved["trial_source_mp4_sha256"]):
        raise ValueError("LOCAL_SPEECH_AB_TRIAL_CHANGED")
    for path, expected in (
        (baseline, original["baseline_measured_seconds"]),
        (trial, plan["trial"].estimated_duration),
    ):
        duration, video, audio = _probe(path)
        if (not video or not audio or not math.isfinite(duration)
                or abs(duration - expected) > 0.5):
            raise ValueError("LOCAL_SPEECH_AB_AV_UNTRUSTED")
        if path == trial and (
            not isinstance(saved.get("trial_final_seconds"), (int, float))
            or abs(duration - saved["trial_final_seconds"]) > 0.05
        ):
            raise ValueError("LOCAL_SPEECH_AB_MEASURED_DURATION_CHANGED")
    return saved, baseline, trial, receipt_sha


def _added_region(plan: dict) -> dict:
    before = sorted(
        plan["original"]["baseline_timeline"].segments,
        key=lambda s: s.output_order,
    )
    after = sorted(plan["trial"].segments, key=lambda s: s.output_order)
    idx = plan["source_cut_index"]
    if not 0 <= idx < len(before) or len(before) != len(after):
        raise ValueError("LOCAL_SPEECH_AB_CUT_UNTRUSTED")
    old, new = before[idx], after[idx]
    offset_a = sum(
        s.source_end - s.source_start for s in before[:idx]
    )
    offset_b = sum(
        s.source_end - s.source_start for s in after[:idx]
    )
    if plan["trial_action"] == "EXTEND_START":
        if not new.source_start < old.source_start or new.source_end != old.source_end:
            raise ValueError("LOCAL_SPEECH_AB_EXTENSION_UNTRUSTED")
        source_start, source_end = new.source_start, old.source_start
        b_start = offset_b
        a_start = offset_a
    elif plan["trial_action"] == "EXTEND_END":
        if not new.source_end > old.source_end or new.source_start != old.source_start:
            raise ValueError("LOCAL_SPEECH_AB_EXTENSION_UNTRUSTED")
        source_start, source_end = old.source_end, new.source_end
        b_start = offset_b + (old.source_end - old.source_start)
        a_start = offset_a + (old.source_end - old.source_start) - min(
            1.0, old.source_end - old.source_start
        )
    else:
        raise ValueError("LOCAL_SPEECH_AB_EXTENSION_UNTRUSTED")
    gained = source_end - source_start
    if not 0 < gained <= 8.0 + _EPSILON:
        raise ValueError("LOCAL_SPEECH_AB_EXTENSION_UNTRUSTED")
    return {
        "source_start": source_start,
        "source_end": source_end,
        "trial_audio_start": b_start,
        "trial_audio_end": b_start + min(1.0, gained),
        "baseline_audio_start": a_start,
        "baseline_audio_end": a_start + min(1.0, old.source_end - old.source_start),
        "baseline_cut_start": old.source_start,
        "baseline_cut_end": old.source_end,
        "trial_cut_start": new.source_start,
        "trial_cut_end": new.source_end,
        "additional_source_seconds": round(gained, 3),
        "changed_cut_index": idx,
        "action": plan["trial_action"],
    }


def _asr_boundary_evidence(prepared: dict, span: dict) -> dict:
    plan = prepared["trial"]
    asset_id = plan["trial"].segments[0].source_asset_id
    asset = _canonical_assets(
        plan["original"]["database"], {asset_id},
    )[asset_id]
    transcript = _read_transcript(
        _no_links(prepared["transcript_root"]), asset.external_id,
    )
    if transcript is None:
        return {
            "status": "SOURCE_ASR_NOT_AVAILABLE",
            "boundary_overlap_baseline_count": None,
            "boundary_overlap_trial_count": None,
            "added_interval_full_utterance_count": None,
            "added_interval_partial_utterance_count": None,
            "source_text_exported": False,
        }

    segments, digest, basis = transcript
    if digest != plan["local_source_asr_sha256"] or basis not in {
        "FILENAME_AND_METADATA_ID_MATCH_AUDIO_UNVERIFIED",
        "FILENAME_ONLY_IDENTITY_UNVERIFIED",
    }:
        # A filename-only association is allowed here ONLY after the original
        # extension proposal has independently anchored its exact bytes to a
        # trusted source manifest and the digest still agrees.
        raise ValueError("LOCAL_SPEECH_AB_ASR_LINEAGE_CHANGED")

    def crossings(start: float, end: float) -> int:
        return sum(
            start + _EPSILON < x.start < end - _EPSILON
            and x.start + _EPSILON < end < x.end - _EPSILON
            or x.start + _EPSILON < start < x.end - _EPSILON
            for x in segments
        )
    full = partial = 0
    for seg in segments:
        if seg.end <= span["source_start"] or seg.start >= span["source_end"]:
            continue
        if (seg.start >= span["source_start"] - _EPSILON
                and seg.end <= span["source_end"] + _EPSILON):
            full += 1
        else:
            partial += 1
    return {
        "status": "SOURCE_ASR_TIMING_OBSERVED_UNVERIFIED_AUDIO",
        "boundary_overlap_baseline_count": crossings(
            span["baseline_cut_start"], span["baseline_cut_end"]
        ),
        "boundary_overlap_trial_count": crossings(
            span["trial_cut_start"], span["trial_cut_end"]
        ),
        "added_interval_full_utterance_count": full,
        "added_interval_partial_utterance_count": partial,
        "source_text_exported": False,
    }


def _audio_sample(ffmpeg: str, video: Path, start: float, end: float) -> dict:
    if not (0 <= start < end and end - start <= 1.001):
        raise ValueError("LOCAL_SPEECH_AB_SAMPLE_RANGE_UNTRUSTED")
    mean, peak = _measure(ffmpeg, video, start, end)
    return {
        "mean_dbfs": round(mean, 2),
        "peak_dbfs": round(peak, 2),
        "duration_seconds": round(end - start, 3),
    }


def audit_finished_extension(
    prepared: dict, *, execute: bool = False,
) -> dict:
    """Dry-run is read-only and decoder-free; execute stores a new JSON receipt."""
    saved, baseline, trial, matched_sha = _load_matched(prepared)
    span = _added_region(prepared["trial"])
    result = {
        "version": VERSION,
        "status": "LOCAL_SPEECH_AB_AUDIT_PREVIEW_ONLY",
        "job_id": prepared["trial"]["job_id"],
        "trial_id": prepared["trial"]["trial_id"],
        "matched_receipt_sha256": matched_sha,
        "baseline_final_sha256": saved["baseline_final_sha256"],
        "trial_final_sha256": saved["trial_final_sha256"],
        "changed_cut_index": span["changed_cut_index"],
        "extension_action": span["action"],
        "added_source_start_seconds": round(span["source_start"], 3),
        "added_source_end_seconds": round(span["source_end"], 3),
        "added_source_seconds": span["additional_source_seconds"],
        "asr": _asr_boundary_evidence(prepared, span),
        "audio": {
            "status": "NOT_MEASURED",
            "baseline_adjacent": None, "trial_added": None,
        },
        "editorial_quality_improvement": None,
        "semantic_role_verified": False,
        "source_asr_linguistically_verified": False,
        "human_rating_available": False,
        "auto_adoption_allowed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False,
        "youtube_contacted": False,
    }
    if not execute:
        return result
    output = _no_links(prepared["matched_receipt"].with_name(
        f"{prepared['trial']['output'].stem}.speech-audit.json"
    ))
    if output.exists() or not output.parent.is_dir():
        raise ValueError("LOCAL_SPEECH_AB_RECEIPT_EXISTS_OR_UNSAFE")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        result["audio"]["status"] = "FFMPEG_NOT_AVAILABLE"
    else:
        try:
            result["audio"]["baseline_adjacent"] = _audio_sample(
                ffmpeg, baseline, span["baseline_audio_start"],
                span["baseline_audio_end"],
            )
            result["audio"]["trial_added"] = _audio_sample(
                ffmpeg, trial, span["trial_audio_start"],
                span["trial_audio_end"],
            )
            result["audio"]["status"] = "LOCAL_AUDIO_SIGNALS_MEASURED_NOT_PREFERENCE"
        except (ValueError, OSError, TypeError):
            result["audio"] = {
                "status": "AUDIO_MEASUREMENT_UNAVAILABLE",
                "baseline_adjacent": None, "trial_added": None,
            }
    # If either source file changed during decoder execution, do not commit.
    if (_hash(baseline)[0] != saved["baseline_final_sha256"]
            or _hash(trial)[0] != saved["trial_final_sha256"]
            or _hash(prepared["trial"]["original"]["baseline_media"])[0]
            != prepared["trial"]["original"]["baseline_sha256"]
            or _hash(prepared["trial"]["output"])[0]
            != saved["trial_source_mp4_sha256"]
            or hashlib.sha256(
                _no_links(prepared["matched_receipt"]).read_bytes()
            ).hexdigest() != matched_sha):
        raise ValueError("LOCAL_SPEECH_AB_MEDIA_CHANGED_DURING_AUDIT")
    result["status"] = "LOCAL_SPEECH_AB_EVIDENCE_RECORDED_UNQUALIFIED"
    save_semantic_review(result, output)
    return result
