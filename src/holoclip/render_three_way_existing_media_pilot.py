"""One-command, existing-media-only A/B/B2 technical evidence pilot.

The queue is merely a discovery index. No acquisition, renders, source edits,
outbound services, automatic candidate choice, or creative quality promotion.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from holoclip.render_cut_extension_three_way_acoustic import (
    prepare_three_way_acoustic, run_three_way_acoustic,
)
from holoclip.render_cut_extension_three_way_diagnostics import (
    _source_evidence, prepare_three_way_diagnostics, run_three_way_diagnostics,
)
from holoclip.render_cut_extension_three_way_triage import _load_diagnostics
from holoclip.render_plan import _no_links
from holoclip.render_human_edit_context import DEFAULT_HUMAN_STORE, DEFAULT_GROWTH_STATE
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_source_queue import DEFAULT_QUEUE, _connect

VERSION = "holoclip-three-way-existing-media-pilot-v1"
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_MAX_JOBS = 4
_MAX_INVENTORY = 1000
_MAX_ACOUSTIC_BYTES = 65536
_EXPECTED_ACOUSTIC_STATUS = "LOCAL_ACOUSTIC_MEASURED_SIGNALS_NOT_SPEECH_PROOF"
_SKIPPED = "LOCAL_PILOT_TRIPLET_NOT_QUALIFIED_OR_MISSING"


def _completed_jobs(queue: Path, *, job_id: str | None, max_jobs: int) -> tuple[list[str], int]:
    """Bounded deterministic read-only queue scan; never create a database."""
    if type(max_jobs) is not int or not 1 <= max_jobs <= _MAX_JOBS:
        raise ValueError("LOCAL_PILOT_INVALID_JOB_LIMIT")
    if job_id is not None and (not isinstance(job_id, str) or not _JOB.fullmatch(job_id)):
        raise ValueError("LOCAL_PILOT_JOB_ID_INVALID")
    conn = _connect(_no_links(queue), create=False)
    if conn is None:
        return [], 0
    try:
        rows = conn.execute(
            "SELECT job_id,state FROM queue_jobs ORDER BY created_at,job_id LIMIT 1001"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) > _MAX_INVENTORY:
        raise ValueError("LOCAL_PILOT_QUEUE_UNBOUNDED")
    known = {"QUEUED", "RUNNING", "DEFERRED", "PAUSED", "COMPLETE"}
    if any(not _JOB.fullmatch(row["job_id"]) or row["state"] not in known for row in rows):
        raise ValueError("LOCAL_PILOT_QUEUE_UNTRUSTED")
    completed = [row["job_id"] for row in rows if row["state"] == "COMPLETE"]
    if job_id is not None:
        completed = [jid for jid in completed if jid == job_id]
    return completed[:max_jobs], len(completed)


def _existing_diagnostic(prepared: dict, observed: dict) -> dict:
    verified, _, _ = _load_diagnostics({"diagnostics": prepared}, observed)
    return verified


def _existing_acoustic(prepared: dict, observed: dict, diagnostic: dict) -> dict:
    path = _no_links(prepared["receipt"])
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_ACOUSTIC_BYTES:
        raise ValueError("LOCAL_PILOT_ACOUSTIC_RECEIPT_INVALID")
    try:
        payload = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise ValueError("LOCAL_PILOT_ACOUSTIC_RECEIPT_INVALID") from exc
    if not isinstance(payload, dict):
        raise ValueError("LOCAL_PILOT_ACOUSTIC_RECEIPT_INVALID")
    import holoclip.render_cut_extension_three_way_acoustic as acoustic

    # No assertion of provenance stronger than the original unsigned receipt.
    needed = {
        "version": acoustic.VERSION,
        "status": _EXPECTED_ACOUSTIC_STATUS,
        "job_id": observed["job_id"],
        "trial_id": observed["trial_id"],
        "b2_id": observed["b2_id"],
        "source_asr_sha256": observed["asr_sha256"],
        "source_receipt_sha256": observed["receipt_hashes"],
        "ready": False, "production_ready": False,
        "auto_adoption_allowed": False, "publication_performed": False,
        "asr_audio_content_independently_verified": False,
        "human_narrative_quality_verified": False,
    }
    if any(payload.get(name) != value for name, value in needed.items()):
        raise ValueError("LOCAL_PILOT_ACOUSTIC_LINEAGE_CHANGED")
    variants = payload.get("variants")
    if not isinstance(variants, dict) or set(variants) != {"a", "b", "b2"}:
        raise ValueError("LOCAL_PILOT_ACOUSTIC_VARIANTS_INVALID")
    count = 0
    for role, version in observed["roles"].items():
        item = variants[role]
        if (not isinstance(item, dict)
                or item.get("final_video_sha256") != version["final_sha256"]
                or not isinstance(item.get("audio_samples"), list)
                or not 2 <= len(item["audio_samples"]) <= 5):
            raise ValueError("LOCAL_PILOT_ACOUSTIC_MEDIA_CHANGED")
        for sample in item["audio_samples"]:
            if (not isinstance(sample, dict)
                    or type(sample.get("near_silent_energy_hint")) is not bool
                    or type(sample.get("mean_dbfs")) not in (int, float)
                    or type(sample.get("peak_dbfs")) not in (int, float)
                    or not -100 <= sample["mean_dbfs"] <= 6
                    or not -100 <= sample["peak_dbfs"] <= 6):
                raise ValueError("LOCAL_PILOT_ACOUSTIC_SAMPLES_INVALID")
            count += 1
    if (payload.get("measured_audio_sample_count") != count
            or payload.get("three_way_diagnostic_sha256") !=
            _load_diagnostics({"diagnostics": prepared["dialogue"]["review"]["diagnostics"]},
                              observed)[1]):
        raise ValueError("LOCAL_PILOT_ACOUSTIC_DIAGNOSTIC_CHANGED")
    if diagnostic.get("status") != "LOCAL_THREE_WAY_TECHNICAL_REVIEW_SIGNALS_ONLY":
        raise ValueError("LOCAL_PILOT_TECHNICAL_DIAGNOSTIC_NOT_MEASURED")
    return payload


def _case(
    *, job_id: str, queue: Path, example_store: Path, growth_state: Path,
    preview_root: Path, transcript_root: Path, proposal_index: int,
    refinement_index: int, execute: bool,
) -> dict:
    result = {
        "job_id": job_id,
        "proposal_index": proposal_index,
        "refinement_index": refinement_index,
        "status": _SKIPPED,
        "finished_triplet_evidence_valid": False,
        "technical_scan": "NOT_EVALUATED",
        "local_acoustic_scan": "NOT_EVALUATED",
        "technical_warning_counts": None,
        "acoustic_sample_count": 0,
        "local_mp4_media_read": False,
        "creative_quality_verified": False,
        "auto_adoption_allowed": False,
        "ready": False,
    }
    kwargs = {
        "queue": queue,
        "example_store": example_store,
        "growth_state": growth_state,
        "preview_root": preview_root,
        "transcript_root": transcript_root,
        "job_id": job_id,
        "max_jobs": 40,
        "proposal_index": proposal_index,
        "refinement_index": refinement_index,
    }
    try:
        prepared = prepare_three_way_diagnostics(**kwargs)
        observed = _source_evidence(prepared)
    except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
        return result
    result["finished_triplet_evidence_valid"] = True
    result["status"] = "LOCAL_PILOT_VERIFIED_COMPLETED_TRIPLET"
    if prepared["receipt"].exists():
        diagnostic = _existing_diagnostic(prepared, observed)
        result["technical_scan"] = "EXISTING_RECEIPT_VERIFIED"
    elif execute:
        diagnostic = run_three_way_diagnostics(prepared, execute=True)
        result["technical_scan"] = "NEW_LOCAL_DECODED_EVIDENCE"
        result["local_mp4_media_read"] = True
    else:
        result["technical_scan"] = "NEEDS_LOCAL_DECODE"
        result["local_acoustic_scan"] = "AWAITING_TECHNICAL_SCAN"
        result["status"] = "LOCAL_PILOT_READY_FOR_MEASURED_SCAN"
        return result

    result["technical_warning_counts"] = {
        role: diagnostic["variants"][role]["technical_diagnostic"]["flag_count"]
        for role in ("a", "b", "b2")
    }
    acoustic = prepare_three_way_acoustic(**kwargs)
    if acoustic["receipt"].exists():
        current = _source_evidence(prepared)
        measured = _existing_acoustic(acoustic, current, diagnostic)
        result["local_acoustic_scan"] = "EXISTING_RECEIPT_VERIFIED"
        result["acoustic_sample_count"] = measured["measured_audio_sample_count"]
    elif execute:
        measured = run_three_way_acoustic(acoustic, execute=True)
        result["local_acoustic_scan"] = "NEW_LOCAL_DECODED_EVIDENCE"
        result["local_mp4_media_read"] = True
        result["acoustic_sample_count"] = measured["measured_audio_sample_count"]
    else:
        result["local_acoustic_scan"] = "NEEDS_LOCAL_DECODE"
        result["status"] = "LOCAL_PILOT_READY_FOR_MEASURED_SCAN"
        return result

    result["status"] = "LOCAL_PILOT_TECHNICAL_AND_AUDIO_EVIDENCE_AVAILABLE"
    return result


def run_existing_media_pilot(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = Path("data/render-previews"),
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, max_jobs: int = 2,
    proposal_index: int = 0, refinement_index: int = 0,
    execute: bool = False,
) -> dict:
    """Inventory existing completed jobs and optionally scan their real local MP4s."""
    if type(execute) is not bool:
        raise ValueError("LOCAL_PILOT_INVALID_EXECUTION_FLAG")
    if type(proposal_index) is not int or proposal_index not in (0, 1):
        raise ValueError("LOCAL_PILOT_INVALID_PROPOSAL")
    if type(refinement_index) is not int or refinement_index not in (0, 1):
        raise ValueError("LOCAL_PILOT_INVALID_REFINEMENT")
    queue, example_store, growth_state, preview_root, transcript_root = [
        _no_links(Path(item)) for item in (
            queue, example_store, growth_state, preview_root, transcript_root,
        )
    ]
    candidates, total = _completed_jobs(queue, job_id=job_id, max_jobs=max_jobs)
    results = []
    for jid in candidates:
        try:
            result = _case(
                job_id=jid, queue=queue, example_store=example_store,
                growth_state=growth_state, preview_root=preview_root,
                transcript_root=transcript_root,
                proposal_index=proposal_index, refinement_index=refinement_index,
                execute=execute,
            )
        except (ValueError, TypeError, OSError, UnicodeError, sqlite3.Error):
            # A malformed/stale *existing* evidence record must not be
            # auto-replaced or silently counted as valid local footage.
            result = {
                "job_id": jid,
                "proposal_index": proposal_index,
                "refinement_index": refinement_index,
                "status": "LOCAL_PILOT_EVIDENCE_REJECTED_OR_SCAN_FAILED",
                "finished_triplet_evidence_valid": False,
                "technical_scan": "BLOCKED",
                "local_acoustic_scan": "BLOCKED",
                "technical_warning_counts": None,
                "acoustic_sample_count": 0,
                "local_mp4_media_read": False,
                "creative_quality_verified": False,
                "auto_adoption_allowed": False,
                "ready": False,
            }
        results.append(result)
    return {
        "version": VERSION,
        "status": (
            "LOCAL_PILOT_EXISTING_MEDIA_SCAN_RECORDED_UNQUALIFIED" if execute
            else "LOCAL_PILOT_EXISTING_MEDIA_INVENTORY_ONLY"
        ),
        "local_queue_present": queue.is_file(),
        "completed_queue_jobs_matching_filter": total,
        "selected_job_count": len(candidates),
        "cases": results,
        "completed_technical_and_audio_case_count": sum(
            row["status"] == "LOCAL_PILOT_TECHNICAL_AND_AUDIO_EVIDENCE_AVAILABLE"
            for row in results
        ),
        "new_local_decoded_media_case_count": sum(
            row["local_mp4_media_read"] for row in results
        ),
        "source_video_downloads_performed": False,
        "internet_services_contacted": False,
        "external_asr_or_llm_invoked": False,
        "real_world_editorial_accuracy_verified": False,
        "existing_in_sample_references_not_creative_gold": True,
        "audio_energy_not_speech_recognition": True,
        "manual_operator_source_data_needed": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
    }
