"""Matched optional audio/caption stages for already grounded extension MP4 A/B.

The baseline stays read-only and is checked against the canonical render
evidence rows. Trial B is new-only. This compares media *treatments*, not
the creative value of different cut contents or human semantic accuracy.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from holoclip.render_audio_finish import level_preview_audio
from holoclip.render_cut_extension_mp4_ab import (
    prepare_cut_extension_mp4_trial, run_cut_extension_mp4_trial,
)
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT, _offline_render_policy
from holoclip.render_source_queue import DEFAULT_QUEUE, _connect
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_same_source_geometry import (
    DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE, DEFAULT_TRANSCRIPT_ROOT,
)
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_subtitle_burnin import burn_captioned_preview
from holoclip.render_subtitles import export_cut_aligned_srt

VERSION = "holoclip-matched-cut-extension-finishing-ab-v1"
_SHA_LEN = 64
_MAX_RECEIPT = 65536
_SUCCESS = {
    "audio": "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL",
    "caption": "LOCAL_SUBTITLE_SRT_CREATED_UNVERIFIED_ASR",
    "burnin": "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL",
}
_ALLOWED = {
    "audio": {_SUCCESS["audio"], "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"},
    "caption": {
        _SUCCESS["caption"], "LOCAL_SUBTITLE_NOT_AVAILABLE",
        "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED", "LOCAL_SUBTITLE_NO_FULL_UTTERANCES",
    },
    "burnin": {
        _SUCCESS["burnin"], "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
        "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED",
    },
}


def _evidence_rows(queue: Path, job_id: str) -> dict:
    """Existing DB is query-only, no tables created or upgraded."""
    conn = _connect(queue, create=False)
    if conn is None:
        raise ValueError("LOCAL_MATCHED_AB_QUEUE_MISSING")
    try:
        conn.execute("PRAGMA query_only=ON")
        found = {}
        for kind, table in (
            ("audio", "render_audio_evidence"),
            ("caption", "render_caption_evidence"),
            ("burnin", "render_burnin_evidence"),
        ):
            check = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if check is None:
                raise ValueError("LOCAL_MATCHED_AB_BASELINE_EVIDENCE_MISSING")
            row = conn.execute(
                f"SELECT status,output_sha256 FROM {table} WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] not in _ALLOWED[kind]:
                raise ValueError("LOCAL_MATCHED_AB_BASELINE_EVIDENCE_UNTRUSTED")
            success = row["status"] == _SUCCESS[kind]
            value = row["output_sha256"]
            if success and (
                not isinstance(value, str) or len(value) != _SHA_LEN
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("LOCAL_MATCHED_AB_BASELINE_HASH_UNTRUSTED")
            found[kind] = {
                "status": row["status"], "successful": success,
                "sha256": value if success else None,
            }
        # A caption burn requires a known caption source; an unmatched
        # original processing chain is never an eligible comparison.
        if found["burnin"]["successful"] and not found["caption"]["successful"]:
            raise ValueError("LOCAL_MATCHED_AB_BASELINE_STAGE_INCONSISTENT")
        return found
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_MATCHED_AB_BASELINE_EVIDENCE_UNAVAILABLE") from exc
    finally:
        conn.close()


def _stage_paths(original: Path) -> dict[str, Path]:
    return {
        "base": _no_links(original),
        "audio": _no_links(original.with_name(f"{original.stem}.leveled.mp4")),
        "caption": _no_links(original.with_suffix(".srt")),
        "burnin": _no_links(original.with_name(f"{original.stem}.captioned.mp4")),
    }


def _verify_stages(paths: dict[str, Path], stages: dict, base_sha: str) -> dict:
    """Check exact original stage filenames and bytes; reject orphan stages."""
    verified = {"base": base_sha}
    for kind in ("audio", "caption", "burnin"):
        path = paths[kind]
        stage = stages[kind]
        if stage["successful"]:
            if not path.is_file() or _hash(path)[0] != stage["sha256"]:
                raise ValueError("LOCAL_MATCHED_AB_BASELINE_STAGE_CHANGED")
            verified[kind] = stage["sha256"]
        elif path.exists():
            raise ValueError("LOCAL_MATCHED_AB_BASELINE_STAGE_AMBIGUOUS")
    return verified


def _trial_receipt(prepared: dict) -> dict:
    """No client-supplied paths or untrusted MP4 versions can bypass lineage."""
    file = _no_links(prepared["receipt"])
    if not file.is_file() or not 0 < file.stat().st_size <= _MAX_RECEIPT:
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_RECEIPT_MISSING")
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_RECEIPT_INVALID")
    original = prepared["original"]
    required = {
        "status": "LOCAL_EXTENSION_AB_RENDERED_TECHNICAL_ONLY",
        "job_id": prepared["job_id"], "trial_id": prepared["trial_id"],
        "trial_mp4_filename": prepared["output"].name,
        "trial_receipt_filename": file.name,
        "baseline_mp4_sha256_reverified": original["baseline_sha256"],
        "local_source_asr_sha256": prepared["local_source_asr_sha256"],
        "source_stream_sha256": prepared["source_stream_sha256"],
        "trial_timeline_sha256": prepared["trial_timeline_sha256"],
        "auto_adoption_allowed": False, "ready": False,
        "production_ready": False, "publication_performed": False,
    }
    if any(data.get(key) != expected for key, expected in required.items()):
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_RECEIPT_INCONSISTENT")
    target = _no_links(prepared["output"])
    if not target.is_file() or not isinstance(data.get("trial_mp4_sha256"), str):
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_MP4_MISSING")
    if _hash(target)[0] != data["trial_mp4_sha256"]:
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_MP4_CHANGED")
    duration, video, audio = _probe(target)
    if (not video or not audio or not math.isfinite(duration)
            or abs(duration - prepared["trial"].estimated_duration) > 0.5):
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_AV_CHANGED")
    return data


def prepare_matched_finishing_trial(
    *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = 40,
    job_id: str | None = None,
    proposal_index: int = 0,
) -> dict:
    """Re-derive both the original render and ASR-grounded extended timeline."""
    trial = prepare_cut_extension_mp4_trial(
        queue=queue, example_store=example_store, growth_state=growth_state,
        preview_root=preview_root, transcript_root=transcript_root,
        after_job_id=after_job_id, max_jobs=max_jobs,
        job_id=job_id, proposal_index=proposal_index,
    )
    stages = _evidence_rows(queue, trial["job_id"])
    file = _no_links(trial["output"].with_name(
        f"{trial['output'].stem}.matched.json"
    ))
    return {"trial": trial, "baseline_stages": stages,
            "matched_receipt": file, "transcript_root": _no_links(transcript_root)}


def _summary(prepared: dict) -> dict:
    trial = prepared["trial"]
    return {
        "version": VERSION,
        "status": "LOCAL_MATCHED_AB_NOT_EXECUTED",
        "job_id": trial["job_id"], "trial_id": trial["trial_id"],
        "baseline_mp4_filename": trial["baseline_mp4_filename"],
        "trial_mp4_filename": trial["trial_mp4_filename"],
        "matched_receipt_filename": prepared["matched_receipt"].name,
        "baseline_audio_leveling_expected": prepared["baseline_stages"]["audio"]["successful"],
        "baseline_srt_expected": prepared["baseline_stages"]["caption"]["successful"],
        "baseline_burnin_expected": prepared["baseline_stages"]["burnin"]["successful"],
        "baseline_stage_statuses": {
            key: val["status"] for key, val in prepared["baseline_stages"].items()
        },
        "source_stream_sha256": trial["source_stream_sha256"],
        "trial_timeline_sha256": trial["trial_timeline_sha256"],
        "local_source_asr_sha256": trial["local_source_asr_sha256"],
        "baseline_source_mp4_sha256": trial["original"]["baseline_sha256"],
        "stage_comparable": False,
        "editorial_comparable": False,
        "source_text_verified": False,
        "semantic_quality_measured": False,
        "quality_difference": None,
        "real_media_checked": False,
        "auto_adoption_allowed": False, "ready": False,
        "production_ready": False, "publication_performed": False,
        "youtube_contacted": False,
    }


def run_matched_finishing_trial(prepared: dict, *, execute: bool = False,
                                approve_local_preview: bool = False) -> dict:
    result = _summary(prepared)
    if not execute:
        return result
    if approve_local_preview is not True:
        raise ValueError("LOCAL_MATCHED_AB_EXPLICIT_PERMISSION_REQUIRED")
    original = prepared["trial"]["original"]
    base = original["baseline_media"]
    output = prepared["trial"]["output"]
    receipt = prepared["matched_receipt"]
    if receipt.exists() or not receipt.parent.is_dir():
        raise ValueError("LOCAL_MATCHED_AB_RECEIPT_EXISTS")
    stage_paths = _stage_paths(base)
    stages = prepared["baseline_stages"]
    # Original A, every claimed optional stage and postprocessed artifact
    # are SHA-checked BEFORE even considering generating a new trial B.
    if _hash(base)[0] != original["baseline_sha256"]:
        raise ValueError("LOCAL_MATCHED_AB_ORIGINAL_CHANGED")
    initial = _verify_stages(stage_paths, stages, original["baseline_sha256"])
    baseline_final_kind = (
        "burnin" if stages["burnin"]["successful"] else
        "audio" if stages["audio"]["successful"] else "base"
    )
    base_duration, base_video, base_audio = _probe(stage_paths[baseline_final_kind])
    if not base_video or not base_audio or abs(
        base_duration - original["baseline_measured_seconds"]
    ) > 0.5:
        raise ValueError("LOCAL_MATCHED_AB_BASELINE_FINAL_AV_UNTRUSTED")
    b_paths = _stage_paths(output)
    for kind in ("audio", "caption", "burnin"):
        if b_paths[kind].exists():
            raise ValueError("LOCAL_MATCHED_AB_EXISTING_TRIAL_STAGE")
    if output.exists() or prepared["trial"]["receipt"].exists():
        if not output.is_file() or not prepared["trial"]["receipt"].is_file():
            raise ValueError("LOCAL_MATCHED_AB_PARTIAL_TRIAL_EXISTS")
        base_trial = _trial_receipt(prepared["trial"])
    else:
        base_trial = run_cut_extension_mp4_trial(
            prepared["trial"], execute=True, approve_local_preview=True,
        )
        base_trial = _trial_receipt(prepared["trial"])
    # Re-evaluate source RENDER policy and source mapping for the proposed
    # extensions as of finishing time, including previously created trial B.
    sources = resolve_cached_render_sources(
        prepared["trial"]["trial"],
        database=original["database"], cache_root=original["cache_root"],
        approve_local_preview=True,
    )
    _offline_render_policy(
        prepared["trial"]["trial"], sources, original["policy_file"],
    )
    trial_sha = base_trial["trial_mp4_sha256"]
    evidence = {}
    if stages["caption"]["successful"]:
        evidence["caption"] = export_cut_aligned_srt(
            prepared["trial"]["trial"], sources,
            transcript_root=prepared["transcript_root"], mp4_output=output,
        )
    if stages["audio"]["successful"]:
        evidence["audio"] = level_preview_audio(
            mp4_output=output, video_sha256=trial_sha,
            timeline=prepared["trial"]["trial"],
        )
    caption_ok = (
        not stages["caption"]["successful"] or
        evidence.get("caption", {}).get("status") == _SUCCESS["caption"]
    )
    audio_ok = (
        not stages["audio"]["successful"] or
        evidence.get("audio", {}).get("status") == _SUCCESS["audio"]
    )
    b_final = output
    b_final_sha = trial_sha
    if stages["audio"]["successful"] and audio_ok:
        b_final = b_paths["audio"]
        b_final_sha = evidence["audio"]["output_sha256"]
    if stages["burnin"]["successful"] and caption_ok and audio_ok:
        evidence["burnin"] = burn_captioned_preview(
            mp4_output=b_final, video_sha256=b_final_sha,
            subtitle_sha256=evidence["caption"]["output_sha256"],
        )
        if evidence["burnin"]["status"] == _SUCCESS["burnin"]:
            b_final = b_paths["burnin"]
            b_final_sha = evidence["burnin"]["output_sha256"]
    burn_ok = (
        not stages["burnin"]["successful"] or
        evidence.get("burnin", {}).get("status") == _SUCCESS["burnin"]
    )
    stage_match = caption_ok and audio_ok and burn_ok
    trial_final_measured_seconds = None
    if stage_match:
        if _hash(b_final)[0] != b_final_sha:
            raise ValueError("LOCAL_MATCHED_AB_TRIAL_FINAL_CHANGED")
        td, tv, ta = _probe(b_final)
        if not tv or not ta or abs(td - prepared["trial"]["trial"].estimated_duration) > 0.5:
            raise ValueError("LOCAL_MATCHED_AB_TRIAL_FINAL_AV_UNTRUSTED")
        trial_final_measured_seconds = round(td, 3)
    # Recheck original and all previously pinned A stages *after* FFmpeg;
    # do not report completion if A was modified while B was processing.
    # _verify_stages only rehashes optional siblings, not the base MP4.
    if _hash(base)[0] != original["baseline_sha256"]:
        raise ValueError("LOCAL_MATCHED_AB_ORIGINAL_CHANGED_DURING_FINISH")
    if _verify_stages(stage_paths, stages, original["baseline_sha256"]) != initial:
        raise ValueError("LOCAL_MATCHED_AB_ORIGINAL_STAGE_CHANGED")
    if _hash(output)[0] != trial_sha:
        raise ValueError("LOCAL_MATCHED_AB_TRIAL_BASE_CHANGED")
    result.update({
        "status": (
            "LOCAL_MATCHED_AB_STAGES_TECHNICALLY_COMPARABLE"
            if stage_match else "LOCAL_MATCHED_AB_FINISH_INCOMPLETE_NOT_COMPARABLE"
        ),
        "baseline_final_filename": stage_paths[baseline_final_kind].name,
        "baseline_final_sha256": initial[baseline_final_kind],
        "trial_final_filename": b_final.name if stage_match else None,
        "trial_final_sha256": b_final_sha if stage_match else None,
        "trial_source_mp4_sha256": trial_sha,
        "trial_caption_status": evidence.get("caption", {}).get("status", "NOT_REQUESTED"),
        "trial_audio_status": evidence.get("audio", {}).get("status", "NOT_REQUESTED"),
        "trial_burnin_status": evidence.get("burnin", {}).get("status", "NOT_REQUESTED"),
        "baseline_stage_hashes": initial,
        "trial_caption_cue_count": evidence.get("caption", {}).get("cue_count"),
        "trial_caption_dropped_cue_count":
            evidence.get("caption", {}).get("dropped_cue_count"),
        "trial_audio_max_abs_gain_db": evidence.get("audio", {}).get("max_abs_gain_db"),
        "baseline_final_seconds": round(base_duration, 3),
        "trial_final_seconds": trial_final_measured_seconds,
        "trial_expected_seconds": round(
            prepared["trial"]["trial"].estimated_duration, 3
        ),
        "stage_comparable": stage_match,
        "real_media_checked": True,
        "editorial_comparable": False,
        "source_text_verified": False,
        "semantic_quality_measured": False,
        "quality_difference": None,
        "ready": False, "production_ready": False,
        "auto_adoption_allowed": False, "publication_performed": False,
    })
    save_semantic_review(result, receipt)
    return result
