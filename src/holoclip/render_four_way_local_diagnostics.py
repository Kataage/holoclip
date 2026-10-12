"""Source-pinned four-way local *technical* and acoustic edit diagnostics.

Reuse the existing full-video diagnostic and bounded FFmpeg audio samplers
across finished A/B/B2/B3. No model inference, transcript export, download,
edit adoption, or publishing. All media and optional stages stay untouched.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from holoclip.render_audio_finish import _measure
from holoclip.render_cut_extension_finish_ab import _SUCCESS, _stage_paths
from holoclip.render_cut_extension_three_way_diagnostics import _source_evidence
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_quality_diagnostics import _run
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_b3_local_render import _evidence
from holoclip.render_three_way_b3_matched_finishing import (
    VERSION as FINISH_VERSION, _render_receipt, _snapshot_stages,
    _summary_finish, _verify_snapshot,
)
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock
from holoclip.render_three_way_existing_media_pilot import _completed_jobs

VERSION = "holoclip-four-way-local-technical-acoustic-qa-v1"
_ROLES = ("a", "b", "b2", "b3")
_MAX_FINISH_BYTES = 65536
_AUDIO_WINDOW = .6
_MAX_DURATION = 180.0
_STATUS = "LOCAL_B3_FINISH_STAGES_TECHNICALLY_COMPARABLE"


def _b3_finished(found: dict, inputs: dict, candidate_index: int) -> dict:
    """Trust only a presently re-derivable and SHA-matching B3 finishing result."""
    technical, render_sha = _render_receipt(found, inputs, candidate_index)
    original = _summary_finish(found, technical, render_sha)
    output = _no_links(found["target"].with_name(
        f"{found['target'].stem}.matched.json"
    ))
    if (not output.is_file() or output.is_symlink()
            or not 0 < output.stat().st_size <= _MAX_FINISH_BYTES):
        raise ValueError("LOCAL_FOUR_WAY_B3_FINISH_NOT_FOUND")
    raw = output.read_bytes()
    try:
        receipt = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_FOUR_WAY_B3_FINISH_INVALID_JSON") from exc
    required = {
        key: value for key, value in original.items() if key not in {
            "status", "b3_final_filename", "b3_final_sha256",
            "b3_measured_final_seconds", "b3_audio_status",
            "b3_caption_status", "b3_burnin_status",
            "b3_caption_cue_count", "b3_caption_dropped_cue_count",
            "b3_audio_max_abs_gain_db", "b3_optional_stages_match_a_b_b2",
        }
    }
    if (not isinstance(receipt, dict)
            or receipt.get("version") != FINISH_VERSION
            or receipt.get("status") != _STATUS
            or any(receipt.get(k) != value for k, value in required.items())
            or receipt.get("b3_optional_stages_match_a_b_b2") is not True
            or receipt.get("publication_performed") is not False
            or receipt.get("ready") is not False
            or receipt.get("automatic_adoption_allowed") is not False):
        raise ValueError("LOCAL_FOUR_WAY_B3_FINISH_LINEAGE_CHANGED")
    baseline_stages = found["b2"]["prepared"]["baseline_stages"]
    sibling = _stage_paths(found["target"])
    for name in ("audio", "caption", "burnin"):
        expected = (_SUCCESS[name] if baseline_stages[name]["successful"]
                    else "NOT_REQUESTED")
        if receipt.get(f"b3_{name}_status") != expected:
            raise ValueError("LOCAL_FOUR_WAY_B3_STAGE_PARITY_CHANGED")
        path = sibling[name]
        if baseline_stages[name]["successful"]:
            if (not path.is_file() or path.is_symlink()
                    or not _hash(path)[1]):
                raise ValueError("LOCAL_FOUR_WAY_B3_STAGE_MISSING")
        elif path.exists() or path.is_symlink():
            raise ValueError("LOCAL_FOUR_WAY_B3_UNEXPECTED_STAGE")
    final_kind = (
        "burnin" if baseline_stages["burnin"]["successful"] else
        "audio" if baseline_stages["audio"]["successful"] else "base"
    )
    final = sibling[final_kind]
    sha, size = _hash(final)
    measured = receipt.get("b3_measured_final_seconds")
    if (not size or receipt.get("b3_final_filename") != final.name
            or receipt.get("b3_final_sha256") != sha
            or type(measured) not in (float, int)
            or not math.isfinite(measured)
            or abs(measured - found["timeline"].estimated_duration) > .5):
        raise ValueError("LOCAL_FOUR_WAY_B3_FINAL_CHANGED")
    return {
        "final": final, "base": found["target"], "timeline": found["timeline"],
        "base_sha256": technical["b3_mp4_sha256"], "final_sha256": sha,
        "caption_sha256": (
            _hash(sibling["caption"])[0]
            if baseline_stages["caption"]["successful"] else None
        ),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "finish_receipt_path": output,
        "final_measured_seconds": measured,
    }


def _clock_windows(timeline, index: int, side: str) -> list[dict]:
    """Uniform 600ms output-clock probes at each version's active cut edge."""
    segments = sorted(timeline.segments, key=lambda item: item.output_order)
    if not 0 <= index < len(segments) or len(segments) > 32:
        raise ValueError("LOCAL_FOUR_WAY_EDIT_INDEX_INVALID")
    before = sum(s.source_end - s.source_start for s in segments[:index])
    edge = before if side == "EXTEND_START" else (
        before + segments[index].source_end - segments[index].source_start
    )
    total = timeline.estimated_duration
    if (not math.isfinite(edge) or not math.isfinite(total)
            or not 0 <= edge <= total <= _MAX_DURATION):
        raise ValueError("LOCAL_FOUR_WAY_OUTPUT_EDGE_INVALID")
    spans = [
        ("BEFORE_EDIT_EDGE_NOT_SPEECH_PROOF", max(0., edge - _AUDIO_WINDOW), edge),
        ("AFTER_EDIT_EDGE_NOT_SPEECH_PROOF", edge, min(total, edge + _AUDIO_WINDOW)),
    ]
    result = []
    for kind, lo, hi in spans:
        if hi - lo >= .06:
            result.append({
                "kind": kind,
                "output_start_seconds": round(lo, 3),
                "output_end_seconds": round(hi, 3),
                "mean_dbfs": None, "peak_dbfs": None,
                "near_silent_energy_hint": None,
            })
    if not result:
        raise ValueError("LOCAL_FOUR_WAY_NO_AUDIO_PROBE_WINDOW")
    return result


def _context(inputs: dict, candidate_index: int) -> dict:
    found = _evidence(inputs, candidate_index)
    if found["choice"] is None:
        raise ValueError("LOCAL_FOUR_WAY_NO_SUPPORTED_B3")
    verified = _b3_finished(found, inputs, candidate_index)
    earlier = _source_evidence(found["prepared"]["diagnostics"])
    tracked = _snapshot_stages(found)
    roles = {name: dict(earlier["roles"][name]) for name in ("a", "b", "b2")}
    roles["b3"] = verified
    # Source- and receipt-bound SHA snapshots of *every* predecessor artifact.
    receipts = {
        **earlier["receipt_hashes"],
        "b3_render": hashlib.sha256(
            _no_links(found["receipt"]).read_bytes()
        ).hexdigest(),
        "b3_matched": verified["receipt_sha256"],
    }
    trial = found["trial"]
    index, side = trial["source_cut_index"], trial["trial_action"]
    if type(index) is not int or side not in ("EXTEND_START", "EXTEND_END"):
        raise ValueError("LOCAL_FOUR_WAY_EDIT_PROVENANCE_INVALID")
    return {
        "found": found, "roles": roles, "tracked": tracked,
        "receipt_sha256": receipts, "source_asr_sha256": found["source_sha"],
        "job_id": trial["job_id"], "trial_id": trial["trial_id"],
        "b2_id": found["b2"]["b2_id"], "b3_id": found["identity"],
        "candidate_index": candidate_index,
        "source_cut_index": index, "trial_action": side,
        "audio_windows": {
            name: _clock_windows(roles[name]["timeline"], index, side)
            for name in _ROLES
        },
        "b3_matched_receipt": verified["finish_receipt_path"],
    }


def _public(context: dict) -> dict:
    roles = context["roles"]
    return {
        "version": VERSION,
        "status": "LOCAL_FOUR_WAY_DIAGNOSTIC_PREVIEW_NO_DECODING",
        "job_id": context["job_id"], "trial_id": context["trial_id"],
        "b2_id": context["b2_id"], "b3_id": context["b3_id"],
        "candidate_index": context["candidate_index"],
        "source_cut_index": context["source_cut_index"],
        "source_asr_sha256": context["source_asr_sha256"],
        "source_receipt_sha256": context["receipt_sha256"],
        "variants": {
            role: {
                "base_sha256": roles[role]["base_sha256"],
                "final_sha256": roles[role]["final_sha256"],
                "technical_diagnostic": None,
                "edge_audio_samples": context["audio_windows"][role],
            } for role in _ROLES
        },
        "comparison": None,
        "actual_videos_scanned": 0,
        "actual_audio_samples_measured": 0,
        "source_clock_word_completeness_proven": False,
        "audio_energy_is_not_speech_or_meaning": True,
        "full_video_diagnostics_are_not_creative_quality": True,
        "transcript_text_exported": False,
        "human_audio_review_completed": False,
        "editorial_quality_improvement": None,
        "best_variant": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


def _ensure_same_before_after(before: dict, later: dict) -> None:
    if (later["receipt_sha256"] != before["receipt_sha256"]
            or later["source_asr_sha256"] != before["source_asr_sha256"]
            or later["tracked"] != before["tracked"]
            or later["audio_windows"] != before["audio_windows"]
            or later["b3_id"] != before["b3_id"]):
        raise ValueError("LOCAL_FOUR_WAY_EVIDENCE_CHANGED_DURING_SCAN")
    _verify_snapshot(before["tracked"])
    for role in _ROLES:
        old, now = before["roles"][role], later["roles"][role]
        if (old["base_sha256"] != now["base_sha256"]
                or old["final_sha256"] != now["final_sha256"]
                or old["caption_sha256"] != now["caption_sha256"]
                or _hash(old["base"])[0] != old["base_sha256"]
                or _hash(old["final"])[0] != old["final_sha256"]):
            raise ValueError("LOCAL_FOUR_WAY_MEDIA_CHANGED_DURING_SCAN")


def run_four_way_diagnostics(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    execute: bool = False, approve_local_preview: bool = False,
) -> dict:
    if (type(execute) is not bool or type(approve_local_preview) is not bool
            or type(proposal_index) is not int or proposal_index not in (0, 1)
            or type(refinement_index) is not int or refinement_index not in (0, 1)
            or type(candidate_index) is not int or candidate_index not in (0, 1)):
        raise ValueError("LOCAL_FOUR_WAY_ARGUMENTS_INVALID")
    if execute and approve_local_preview is not True:
        raise ValueError("LOCAL_FOUR_WAY_EXPLICIT_SCAN_APPROVAL_REQUIRED")
    queue, example_store, growth_state, preview_root, transcript_root = (
        _no_links(Path(p)) for p in (
            queue, example_store, growth_state, preview_root, transcript_root
        )
    )
    jobs, _ = _completed_jobs(queue, job_id=job_id, max_jobs=1)
    if not jobs:
        return {
            "version": VERSION,
            "status": "LOCAL_FOUR_WAY_NO_COMPLETED_SOURCE_JOB",
            "ready": False, "production_ready": False,
        }
    inputs = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": jobs[0],
        "proposal_index": proposal_index, "refinement_index": refinement_index,
    }

    def scan() -> dict:
        before = _context(inputs, candidate_index)
        report = _public(before)
        if not execute:
            return report
        target = _no_links(before["b3_matched_receipt"].with_name(
            f"{before['found']['target'].stem}.four-way-diagnostics.json"
        ))
        if (target.exists() or target.is_symlink()
                or not target.parent.is_dir()):
            raise ValueError("LOCAL_FOUR_WAY_OUTPUT_EXISTS_OR_UNSAFE")
        for role in _ROLES:
            variant = before["roles"][role]
            scan_result = _run(
                base=variant["base"], base_sha256=variant["base_sha256"],
                final=variant["final"], final_sha256=variant["final_sha256"],
                timeline=variant["timeline"],
                caption_sha256=variant["caption_sha256"],
            )
            if (not isinstance(scan_result, dict)
                    or not str(scan_result.get("status", "")).startswith(
                        "LOCAL_EDIT_DIAGNOSTIC_MEASURED_"
                    )
                    or scan_result.get("base_sha256") != variant["base_sha256"]
                    or scan_result.get("final_sha256") != variant["final_sha256"]
                    or type(scan_result.get("flag_count")) is not int
                    or not 0 <= scan_result["flag_count"] <= 600):
                raise ValueError("LOCAL_FOUR_WAY_VIDEO_DIAGNOSTIC_UNTRUSTED")
            report["variants"][role]["technical_diagnostic"] = scan_result
            report["actual_videos_scanned"] += 1
        executable = shutil.which("ffmpeg")
        if executable is None:
            raise ValueError("LOCAL_FOUR_WAY_FFMPEG_UNAVAILABLE")
        for role in _ROLES:
            variant = before["roles"][role]
            path = _no_links(variant["final"])
            if _hash(path)[0] != variant["final_sha256"]:
                raise ValueError("LOCAL_FOUR_WAY_AUDIO_INPUT_CHANGED")
            for sample in report["variants"][role]["edge_audio_samples"]:
                start, end = (
                    sample["output_start_seconds"], sample["output_end_seconds"]
                )
                mean, peak = _measure(executable, path, start, end)
                if (not all(math.isfinite(v) for v in (mean, peak))
                        or not -100 <= mean <= 6
                        or not -100 <= peak <= 6 or peak < mean - .5):
                    raise ValueError("LOCAL_FOUR_WAY_AUDIO_SAMPLE_UNTRUSTED")
                sample.update({
                    "mean_dbfs": round(mean, 2),
                    "peak_dbfs": round(peak, 2),
                    "near_silent_energy_hint": mean <= -50 or peak <= -48,
                })
                report["actual_audio_samples_measured"] += 1
        after = _context(inputs, candidate_index)
        _ensure_same_before_after(before, after)
        prior = report["variants"]["b2"]["technical_diagnostic"]
        current = report["variants"]["b3"]["technical_diagnostic"]
        report["comparison"] = {
            "b3_minus_b2_flag_count": current["flag_count"] - prior["flag_count"],
            "b3_minus_b2_black_intervals": (
                current["black_interval_count"] - prior["black_interval_count"]
            ),
            "b3_minus_b2_freeze_intervals": (
                current["freeze_interval_count"] - prior["freeze_interval_count"]
            ),
            "all_four_use_same_technical_analyzer": True,
            "all_four_use_same_edit_edge_window_width": True,
            "edge_windows_are_not_identical_source_clock_speech": True,
            "flag_counts_are_not_creative_or_publishability_scores": True,
            "no_automatic_winner_selected": True,
        }
        report["status"] = "LOCAL_FOUR_WAY_TECHNICAL_ACOUSTIC_SIGNALS_ONLY"
        save_semantic_review(report, target)
        return report

    if execute:
        with _exclusive_run_lock(preview_root, inputs["job_id"]):
            return scan()
    return scan()
