"""Finish only a verified B3 trial with the same optional A/B/B2 treatments.

No overwrite or automatic acceptance. Local render/audio/subtitle operations
require owner approval; any output is a *technical*, not creative, comparison.
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
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT, _offline_render_policy
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_subtitle_burnin import burn_captioned_preview
from holoclip.render_subtitles import export_cut_aligned_srt
from holoclip.render_three_way_b3_local_render import (
    VERSION as B3_VERSION, _evidence, _summary,
)
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock
from holoclip.render_three_way_existing_media_pilot import _completed_jobs

VERSION = "holoclip-guarded-b3-matched-finishing-v1"
_MAX_RECEIPT_BYTES = 65536
_MAX_CANDIDATES = 2
_B3_QA = "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"


def _render_receipt(found: dict, inputs: dict, candidate_index: int) -> tuple[dict, str]:
    """Check the existing B3 render receipt against *current* canonical input."""
    base = _summary(inputs, found, candidate_index)
    target = _no_links(found["target"])
    path = _no_links(found["receipt"])
    if (not path.is_file() or path.is_symlink()
            or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES
            or not target.is_file() or target.is_symlink()):
        raise ValueError("LOCAL_B3_FINISH_RENDER_EVIDENCE_MISSING_OR_UNSAFE")
    raw = path.read_bytes()
    try:
        evidence = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_B3_FINISH_RENDER_RECEIPT_INVALID") from exc
    invariant = {
        key: value for key, value in base.items() if key not in {
            "status", "new_b3_local_mp4_rendered",
            "a_b_b2_b3_duration_compared",
        }
    }
    if (not isinstance(evidence, dict)
            or evidence.get("version") != B3_VERSION
            or evidence.get("status")
            != "LOCAL_B3_RENDERED_BASIC_AV_QA_NOT_EDITED_QUALITY"
            or any(evidence.get(key) != value
                   for key, value in invariant.items())
            or evidence.get("b3_technical_qa") != _B3_QA
            or evidence.get("new_b3_local_mp4_rendered") is not True
            or evidence.get("a_b_b2_b3_duration_compared") is not True
            or evidence.get("publication_performed") is not False
            or evidence.get("automatic_adoption_allowed") is not False
            or evidence.get("ready") is not False
            or evidence.get("production_ready") is not False):
        raise ValueError("LOCAL_B3_FINISH_RENDER_LINEAGE_UNTRUSTED")
    digest, size = _hash(target)
    duration, video, audio = _probe(target)
    reported = evidence.get("b3_measured_seconds")
    if (size == 0 or evidence.get("b3_mp4_sha256") != digest
            or type(reported) not in (float, int) or not math.isfinite(reported)
            or not video or not audio or not math.isfinite(duration)
            or abs(duration - found["timeline"].estimated_duration) > .5
            or abs(duration - reported) > .01
            or evidence.get("a_b_b2_finished_mp4_sha256") != found["media_sha"]):
        raise ValueError("LOCAL_B3_FINISH_RENDER_MEDIA_CHANGED_OR_UNTRUSTED")
    return evidence, hashlib.sha256(raw).hexdigest()


def _inputs(
    *, queue: Path, example_store: Path, growth_state: Path,
    preview_root: Path, transcript_root: Path, job_id: str,
    proposal_index: int, refinement_index: int,
) -> dict:
    return {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "proposal_index": proposal_index, "refinement_index": refinement_index,
    }


def _snapshot_stages(found: dict) -> dict:
    """Pin A/B/B2 stage bytes plus base videos; refuse orphan siblings."""
    b2 = found["b2"]
    matched = b2["prepared"]
    original = found["trial"]["original"]
    stages = matched["baseline_stages"]
    roots = {
        "a": original["baseline_media"],
        "b": found["trial"]["output"],
        "b2": b2["output"],
    }
    tracked = {}
    for role, media in roots.items():
        paths = _stage_paths(_no_links(media))
        if role == "a":
            _verify_stages(paths, stages, original["baseline_sha256"])
        for stage in ("audio", "caption", "burnin"):
            path = paths[stage]
            if stages[stage]["successful"]:
                if not path.is_file() or path.is_symlink():
                    raise ValueError("LOCAL_B3_FINISH_OLD_STAGE_MISSING")
                tracked[str(path)] = _hash(path)[0]
            elif path.exists() or path.is_symlink():
                raise ValueError("LOCAL_B3_FINISH_UNEXPECTED_OLD_STAGE")
        tracked[str(media)] = _hash(_no_links(media))[0]
    tracked[str(_no_links(found["target"]))] = _hash(found["target"])[0]
    return tracked


def _verify_snapshot(tracked: dict) -> None:
    for raw, sha in tracked.items():
        path = _no_links(Path(raw))
        if not path.is_file() or path.is_symlink() or _hash(path)[0] != sha:
            raise ValueError("LOCAL_B3_FINISH_PREDECESSOR_CHANGED")


def _summary_finish(found: dict, technical: dict, render_sha: str) -> dict:
    matched = found["b2"]["prepared"]
    stages = matched["baseline_stages"]
    return {
        "version": VERSION,
        "status": "LOCAL_B3_FINISH_READ_ONLY_PREVIEW",
        "job_id": found["trial"]["job_id"],
        "trial_id": found["trial"]["trial_id"],
        "b3_id": found["identity"],
        "b3_render_receipt_sha256": render_sha,
        "b3_original_mp4_sha256": technical["b3_mp4_sha256"],
        "b3_timeline_sha256": found["choice"]["proposed_timeline_sha256"],
        "source_asr_sha256": found["source_sha"],
        "edited_audio_hint_digest": found["audio_sig"],
        "a_b_b2_finished_mp4_sha256": found["media_sha"],
        "b3_mp4_filename": found["target"].name,
        "b3_matched_receipt_filename": (
            f"{found['target'].stem}.matched.json"
        ),
        "baseline_audio_leveling_expected": stages["audio"]["successful"],
        "baseline_caption_expected": stages["caption"]["successful"],
        "baseline_burnin_expected": stages["burnin"]["successful"],
        "b3_final_filename": None,
        "b3_final_sha256": None,
        "b3_measured_final_seconds": None,
        "b3_audio_status": "NOT_REQUESTED",
        "b3_caption_status": "NOT_REQUESTED",
        "b3_burnin_status": "NOT_REQUESTED",
        "b3_caption_cue_count": None,
        "b3_caption_dropped_cue_count": None,
        "b3_audio_max_abs_gain_db": None,
        "b3_optional_stages_match_a_b_b2": False,
        "human_auditory_qa_completed": False,
        "actual_audio_quality_improvement": None,
        "editorial_quality_improvement": None,
        "automatic_winner_selected": False,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


def run_existing_b3_matched_finishing(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None,
    proposal_index: int = 0, refinement_index: int = 0, candidate_index: int = 0,
    execute: bool = False, approve_local_preview: bool = False,
) -> dict:
    """Create only new B3 optional-stage outputs after B3 basic render QA."""
    if (type(execute) is not bool or type(approve_local_preview) is not bool
            or type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)
            or type(candidate_index) is not int
            or not 0 <= candidate_index < _MAX_CANDIDATES):
        raise ValueError("LOCAL_B3_FINISH_ARGUMENTS_INVALID")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(p)) for p in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    )
    if execute and approve_local_preview is not True:
        raise ValueError("LOCAL_B3_FINISH_EXPLICIT_APPROVAL_REQUIRED")
    jobs, _total = _completed_jobs(queue, job_id=job_id, max_jobs=1)
    if not jobs:
        return {
            "version": VERSION, "status": "LOCAL_B3_FINISH_NO_COMPLETED_SOURCE_JOB",
            "ready": False, "production_ready": False,
        }
    inputs = _inputs(
        queue=queue, example_store=example_store,
        growth_state=growth_state, preview_root=preview_root,
        transcript_root=transcript_root, job_id=jobs[0],
        proposal_index=proposal_index, refinement_index=refinement_index,
    )

    def work() -> dict:
        found = _evidence(inputs, candidate_index)
        if found["choice"] is None:
            raise ValueError("LOCAL_B3_FINISH_NO_ELIGIBLE_CANDIDATE")
        technical, tech_sha = _render_receipt(found, inputs, candidate_index)
        report = _summary_finish(found, technical, tech_sha)
        if not execute:
            return report
        target = _no_links(found["target"].with_name(
            f"{found['target'].stem}.matched.json"
        ))
        paths = _stage_paths(found["target"])
        if (target.exists() or target.is_symlink()
                or not target.parent.is_dir()):
            raise ValueError("LOCAL_B3_FINISH_RECEIPT_EXISTS_OR_UNSAFE")
        for kind in ("audio", "caption", "burnin"):
            if paths[kind].exists() or paths[kind].is_symlink():
                raise ValueError("LOCAL_B3_FINISH_STAGE_EXISTS_OR_UNSAFE")
        before = _snapshot_stages(found)
        original = found["trial"]["original"]
        stages = found["b2"]["prepared"]["baseline_stages"]
        sources = resolve_cached_render_sources(
            found["timeline"], database=original["database"],
            cache_root=original["cache_root"], approve_local_preview=True,
        )
        _offline_render_policy(
            found["timeline"], sources, original["policy_file"],
        )
        fresh = _evidence(inputs, candidate_index)
        _, again_sha = _render_receipt(fresh, inputs, candidate_index)
        if (again_sha != tech_sha or _summary_finish(
                fresh, technical, again_sha) != report):
            raise ValueError("LOCAL_B3_FINISH_SOURCE_CHANGED_BEFORE_STAGE")
        _verify_snapshot(before)
        outputs = {}
        if stages["caption"]["successful"]:
            outputs["caption"] = export_cut_aligned_srt(
                found["timeline"], sources,
                transcript_root=found["b2"]["prepared"]["transcript_root"],
                mp4_output=found["target"],
            )
        if stages["audio"]["successful"]:
            outputs["audio"] = level_preview_audio(
                mp4_output=found["target"],
                video_sha256=technical["b3_mp4_sha256"],
                timeline=found["timeline"],
            )
        caption_ok = (not stages["caption"]["successful"]
                      or outputs.get("caption", {}).get("status") == _SUCCESS["caption"])
        audio_ok = (not stages["audio"]["successful"]
                    or outputs.get("audio", {}).get("status") == _SUCCESS["audio"])
        final = found["target"]
        sha = technical["b3_mp4_sha256"]
        if audio_ok and stages["audio"]["successful"]:
            final, sha = paths["audio"], outputs["audio"]["output_sha256"]
        if stages["burnin"]["successful"] and caption_ok and audio_ok:
            outputs["burnin"] = burn_captioned_preview(
                mp4_output=final, video_sha256=sha,
                subtitle_sha256=outputs["caption"]["output_sha256"],
            )
            if outputs["burnin"]["status"] == _SUCCESS["burnin"]:
                final, sha = paths["burnin"], outputs["burnin"]["output_sha256"]
        burn_ok = (not stages["burnin"]["successful"]
                   or outputs.get("burnin", {}).get("status") == _SUCCESS["burnin"])
        # Each individual claimed-success stage must exist and match its
        # reported digest, even when it is not the final playback MP4.
        for kind, output in outputs.items():
            if output.get("status") == _SUCCESS[kind]:
                sibling = paths[kind]
                if not sibling.is_file() or sibling.is_symlink():
                    raise ValueError("LOCAL_B3_FINISH_STAGE_NOT_CREATED")
                stage_sha, stage_size = _hash(sibling)
                if (not stage_size
                        or stage_sha != output.get("output_sha256")):
                    raise ValueError("LOCAL_B3_FINISH_STAGE_HASH_INVALID")
        comparable = caption_ok and audio_ok and burn_ok
        measured = None
        if comparable:
            digest, size = _hash(final)
            if not size or digest != sha:
                raise ValueError("LOCAL_B3_FINISH_FINAL_SHA_CHANGED")
            duration, video, audio = _probe(final)
            if (not video or not audio or not math.isfinite(duration)
                    or abs(duration - found["timeline"].estimated_duration) > .5):
                raise ValueError("LOCAL_B3_FINISH_FINAL_AV_UNTRUSTED")
            measured = round(duration, 3)
        after = _evidence(inputs, candidate_index)
        present, after_sha = _render_receipt(after, inputs, candidate_index)
        if (after_sha != tech_sha or present != technical
                or _summary_finish(after, technical, after_sha) != report):
            raise ValueError("LOCAL_B3_FINISH_EVIDENCE_CHANGED_DURING_STAGE")
        _verify_snapshot(before)
        if comparable and _hash(final)[0] != sha:
            raise ValueError("LOCAL_B3_FINISH_FINAL_CHANGED_DURING_STAGE")
        report.update({
            "status": ("LOCAL_B3_FINISH_STAGES_TECHNICALLY_COMPARABLE"
                       if comparable else "LOCAL_B3_FINISH_INCOMPLETE"),
            "b3_optional_stages_match_a_b_b2": comparable,
            "b3_final_filename": final.name if comparable else None,
            "b3_final_sha256": sha if comparable else None,
            "b3_measured_final_seconds": measured,
            "b3_audio_status": outputs.get("audio", {}).get("status", "NOT_REQUESTED"),
            "b3_caption_status": outputs.get("caption", {}).get("status", "NOT_REQUESTED"),
            "b3_burnin_status": outputs.get("burnin", {}).get("status", "NOT_REQUESTED"),
            "b3_caption_cue_count": outputs.get("caption", {}).get("cue_count"),
            "b3_caption_dropped_cue_count":
                outputs.get("caption", {}).get("dropped_cue_count"),
            "b3_audio_max_abs_gain_db": outputs.get("audio", {}).get("max_abs_gain_db"),
        })
        save_semantic_review(report, target)
        return report

    if execute:
        with _exclusive_run_lock(preview_root, inputs["job_id"]):
            return work()
    return work()
