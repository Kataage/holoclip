"""Finish an existing source-ASR B2 video using the identical *optional stages* as A/B.

All three videos stay untouched. This matches processing types (not creative
quality, transcript correctness, or human evaluation). Hosted CI uses mocks.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from holoclip.render_audio_finish import level_preview_audio
from holoclip.render_cut_extension_finish_ab import (
    _SUCCESS, _stage_paths, _verify_stages,
)
from holoclip.render_cut_extension_speech_audit import _load_matched
from holoclip.render_cut_extension_speech_b2 import (
    VERSION as B2_VERSION, prepare_speech_b2_preview,
)
from holoclip.render_cut_extension_speech_refine import suggest_speech_boundary_refinements
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_ready_queue import _offline_render_policy
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_subtitle_burnin import burn_captioned_preview
from holoclip.render_subtitles import export_cut_aligned_srt

VERSION = "holoclip-speech-b2-matched-finishing-v1"
_MAX_RECEIPT_BYTES = 65536
_SUCCESS_B2 = "LOCAL_B2_RENDERED_THREE_WAY_TECHNICAL_ONLY"


def _b2_receipt(prepared: dict) -> tuple[dict, str]:
    """Read only an already-rendered, current SHA-linked B2 technical trial."""
    b2 = prepared["b2"]
    plan = b2["prepared"]["trial"]
    path = _no_links(b2["receipt"])
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES:
        raise ValueError("LOCAL_B2_FINISH_RENDER_RECEIPT_MISSING")
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_B2_FINISH_RENDER_RECEIPT_INVALID") from exc
    if not isinstance(data, dict):
        raise ValueError("LOCAL_B2_FINISH_RENDER_RECEIPT_INVALID")
    required = {
        "version": B2_VERSION,
        "status": _SUCCESS_B2,
        "job_id": plan["job_id"], "trial_id": plan["trial_id"],
        "b2_id": b2["b2_id"],
        "b2_mp4_filename": b2["output"].name,
        "b2_receipt_filename": path.name,
        "b2_timeline_sha256": b2["choice"]["proposed_timeline_sha256"],
        "matched_ab_receipt_sha256": b2["proposal"]["matched_receipt_sha256"],
        "baseline_a_final_sha256": b2["proposal"]["baseline_final_sha256"],
        "trial_b_final_sha256": b2["proposal"]["trial_final_sha256"],
        "local_source_asr_sha256": b2["proposal"]["source_asr_sha256"],
        "b2_technical_qa": "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
        "a_b_b2_media_compared": True,
        "b2_caption_audio_finishing_matched_to_a_b": False,
        "auto_adoption_allowed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False,
    }
    if any(data.get(key) != value for key, value in required.items()):
        raise ValueError("LOCAL_B2_FINISH_RENDER_LINEAGE_UNTRUSTED")
    b2_media = _no_links(b2["output"])
    if not b2_media.is_file():
        raise ValueError("LOCAL_B2_FINISH_MP4_MISSING")
    measured, video, audio = _probe(b2_media)
    if (not video or not audio or not math.isfinite(measured)
            or abs(measured - b2["timeline"].estimated_duration) > .5
            or type(data.get("b2_measured_seconds")) not in (int, float)
            or not math.isfinite(data["b2_measured_seconds"])
            or abs(measured - data["b2_measured_seconds"]) > .05
            or _hash(b2_media)[0] != data.get("b2_mp4_sha256")):
        raise ValueError("LOCAL_B2_FINISH_MP4_CHANGED_OR_UNTRUSTED")
    return data, hashlib.sha256(raw).hexdigest()


def prepare_speech_b2_finishing(**kwargs) -> dict:
    """Derive B2 and the same A-stage evidence without imported trial JSON."""
    b2 = prepare_speech_b2_preview(**kwargs)
    matched = b2["prepared"]
    _load_matched(matched)
    return {
        "b2": b2,
        "finished_receipt": _no_links(b2["output"].with_name(
            f"{b2['output'].stem}.matched.json"
        )),
    }


def _summary(prepared: dict, b2_receipt_sha: str) -> dict:
    b2 = prepared["b2"]
    matched = b2["prepared"]
    plan = matched["trial"]
    stages = matched["baseline_stages"]
    return {
        "version": VERSION,
        "status": "LOCAL_B2_FINISH_PREVIEW_ONLY",
        "job_id": plan["job_id"],
        "trial_id": plan["trial_id"],
        "b2_id": b2["b2_id"],
        "b2_render_receipt_sha256": b2_receipt_sha,
        "matched_ab_receipt_sha256": b2["proposal"]["matched_receipt_sha256"],
        "source_asr_sha256": b2["proposal"]["source_asr_sha256"],
        "baseline_a_final_sha256": b2["proposal"]["baseline_final_sha256"],
        "trial_b_final_sha256": b2["proposal"]["trial_final_sha256"],
        "b2_original_mp4_sha256": _hash(b2["output"])[0],
        "b2_final_filename": None,
        "b2_final_sha256": None,
        "b2_measured_final_seconds": None,
        "b2_timeline_expected_seconds": round(b2["timeline"].estimated_duration, 3),
        "baseline_audio_leveling_expected": stages["audio"]["successful"],
        "baseline_caption_expected": stages["caption"]["successful"],
        "baseline_burnin_expected": stages["burnin"]["successful"],
        "stage_comparable_to_a_and_b": False,
        "b2_caption_audio_finishing_matched_to_a_b": False,
        "editorial_quality_improvement": None,
        "human_rating_available": False,
        "source_asr_linguistically_verified": False,
        "auto_adoption_allowed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False, "youtube_contacted": False,
    }


def finish_speech_b2(
    prepared: dict, *, execute: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Generate create-only B2 siblings; never edit A, B, original B2 or queue."""
    b2 = prepared["b2"]
    matched = b2["prepared"]
    plan = matched["trial"]
    original = plan["original"]
    stages = matched["baseline_stages"]

    technical, b2_receipt_sha = _b2_receipt(prepared)
    baseline_matched, a_video, b_video, matched_sha = _load_matched(matched)
    if matched_sha != b2["proposal"]["matched_receipt_sha256"]:
        raise ValueError("LOCAL_B2_FINISH_MATCHED_AB_CHANGED")
    if suggest_speech_boundary_refinements(matched) != b2["proposal"]:
        raise ValueError("LOCAL_B2_FINISH_SOURCE_EVIDENCE_CHANGED")
    result = _summary(prepared, b2_receipt_sha)
    if not execute:
        return result
    if approve_local_preview is not True:
        raise ValueError("LOCAL_B2_FINISH_EXPLICIT_PERMISSION_REQUIRED")
    target = prepared["finished_receipt"]
    if target.exists() or not target.parent.is_dir():
        raise ValueError("LOCAL_B2_FINISH_RECEIPT_EXISTS_OR_UNSAFE")
    path = b2["output"]
    paths = _stage_paths(path)
    for stage in ("audio", "caption", "burnin"):
        if paths[stage].exists():
            raise ValueError("LOCAL_B2_FINISH_STAGE_EXISTS_OR_UNSAFE")

    # Pin all old visible assets before any FFmpeg work, not only the final
    # A and B. B's intermediate stages were not SHA-recorded by #241, so
    # treat their current bytes as continuity snapshots, not source truth.
    base_a = original["baseline_media"]
    base_b = plan["output"]
    a_paths = _stage_paths(base_a)
    a_stage_hashes = _verify_stages(
        a_paths, stages, original["baseline_sha256"]
    )
    old_hashes = {
        "base_a": original["baseline_sha256"],
        "base_b": baseline_matched["trial_source_mp4_sha256"],
        "final_a": baseline_matched["baseline_final_sha256"],
        "final_b": baseline_matched["trial_final_sha256"],
        "b2": technical["b2_mp4_sha256"],
    }
    b_paths = _stage_paths(base_b)
    b_stage_hashes = {}
    for kind in ("audio", "caption", "burnin"):
        child = b_paths[kind]
        if stages[kind]["successful"] and not child.is_file():
            raise ValueError("LOCAL_B2_FINISH_B_STAGE_MISSING")
        if not stages[kind]["successful"] and child.exists():
            raise ValueError("LOCAL_B2_FINISH_B_STAGE_AMBIGUOUS")
        if stages[kind]["successful"]:
            b_stage_hashes[kind] = _hash(child)[0]
    if _hash(base_a)[0] != old_hashes["base_a"] or _hash(base_b)[0] != old_hashes["base_b"]:
        raise ValueError("LOCAL_B2_FINISH_OLD_BASE_CHANGED")
    sources = resolve_cached_render_sources(
        b2["timeline"], database=original["database"],
        cache_root=original["cache_root"], approve_local_preview=True,
    )
    _offline_render_policy(b2["timeline"], sources, original["policy_file"])

    outputs = {}
    base_sha = technical["b2_mp4_sha256"]
    if stages["caption"]["successful"]:
        outputs["caption"] = export_cut_aligned_srt(
            b2["timeline"], sources,
            transcript_root=matched["transcript_root"], mp4_output=path,
        )
    if stages["audio"]["successful"]:
        outputs["audio"] = level_preview_audio(
            mp4_output=path, video_sha256=base_sha, timeline=b2["timeline"],
        )
    caption_ok = (
        not stages["caption"]["successful"] or
        outputs.get("caption", {}).get("status") == _SUCCESS["caption"]
    )
    audio_ok = (
        not stages["audio"]["successful"] or
        outputs.get("audio", {}).get("status") == _SUCCESS["audio"]
    )
    final = path
    final_sha = base_sha
    if audio_ok and stages["audio"]["successful"]:
        final = paths["audio"]
        final_sha = outputs["audio"]["output_sha256"]
    if (stages["burnin"]["successful"] and caption_ok and audio_ok):
        outputs["burnin"] = burn_captioned_preview(
            mp4_output=final, video_sha256=final_sha,
            subtitle_sha256=outputs["caption"]["output_sha256"],
        )
        if outputs["burnin"]["status"] == _SUCCESS["burnin"]:
            final = paths["burnin"]
            final_sha = outputs["burnin"]["output_sha256"]
    burn_ok = (
        not stages["burnin"]["successful"] or
        outputs.get("burnin", {}).get("status") == _SUCCESS["burnin"]
    )
    parity = caption_ok and audio_ok and burn_ok
    measured = None
    if parity:
        digest, size = _hash(final)
        if size == 0 or digest != final_sha:
            raise ValueError("LOCAL_B2_FINISH_FINAL_CHANGED")
        seconds, video, audio = _probe(final)
        if (not video or not audio or not math.isfinite(seconds)
                or abs(seconds - b2["timeline"].estimated_duration) > .5):
            raise ValueError("LOCAL_B2_FINISH_FINAL_AV_UNTRUSTED")
        measured = round(seconds, 3)

    # Re-derive the ASR, A/B stage receipt, all existing media hashes and
    # the B2 original digest after optional encodes. No success receipt if
    # any side of the comparison mutated during finishing.
    fresh_technical, fresh_receipt_sha = _b2_receipt(prepared)
    if fresh_receipt_sha != b2_receipt_sha or fresh_technical != technical:
        raise ValueError("LOCAL_B2_FINISH_TECHNICAL_RECEIPT_CHANGED")
    refreshed_ab, refreshed_a, refreshed_b, refreshed_sha = _load_matched(matched)
    if (refreshed_sha != matched_sha or refreshed_ab != baseline_matched
            or refreshed_a != a_video or refreshed_b != b_video):
        raise ValueError("LOCAL_B2_FINISH_MATCHED_AB_CHANGED_DURING_WORK")
    if suggest_speech_boundary_refinements(matched) != b2["proposal"]:
        raise ValueError("LOCAL_B2_FINISH_SOURCE_CHANGED_DURING_WORK")
    if _verify_stages(a_paths, stages, original["baseline_sha256"]) != a_stage_hashes:
        raise ValueError("LOCAL_B2_FINISH_A_STAGES_CHANGED")
    if any(_hash(b_paths[name])[0] != digest
           for name, digest in b_stage_hashes.items()):
        raise ValueError("LOCAL_B2_FINISH_B_STAGES_CHANGED")
    check = {
        "base_a": base_a, "base_b": base_b,
        "final_a": a_video, "final_b": b_video, "b2": path,
    }
    if any(_hash(check[key])[0] != digest for key, digest in old_hashes.items()):
        raise ValueError("LOCAL_B2_FINISH_PRIOR_MP4_CHANGED")
    if parity and _hash(final)[0] != final_sha:
        raise ValueError("LOCAL_B2_FINISH_FINAL_CHANGED_DURING_WORK")

    result.update({
        "status": (
            "LOCAL_B2_FINISH_STAGES_TECHNICALLY_COMPARABLE"
            if parity else "LOCAL_B2_FINISH_INCOMPLETE_NOT_COMPARABLE"
        ),
        "stage_comparable_to_a_and_b": parity,
        "b2_caption_audio_finishing_matched_to_a_b": parity,
        "b2_audio_status": outputs.get("audio", {}).get("status", "NOT_REQUESTED"),
        "b2_caption_status": outputs.get("caption", {}).get("status", "NOT_REQUESTED"),
        "b2_burnin_status": outputs.get("burnin", {}).get("status", "NOT_REQUESTED"),
        "b2_caption_cue_count": outputs.get("caption", {}).get("cue_count"),
        "b2_caption_dropped_cue_count": outputs.get("caption", {}).get("dropped_cue_count"),
        "b2_audio_max_abs_gain_db": outputs.get("audio", {}).get("max_abs_gain_db"),
        "b2_final_filename": final.name if parity else None,
        "b2_final_sha256": final_sha if parity else None,
        "b2_measured_final_seconds": measured,
        "source_asr_linguistically_verified": False,
        "editorial_quality_improvement": None,
        "ready": False, "production_ready": False,
        "auto_adoption_allowed": False, "publication_performed": False,
    })
    save_semantic_review(result, target)
    return result
