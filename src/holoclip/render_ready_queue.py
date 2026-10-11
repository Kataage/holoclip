"""Offline completed-source queue -> deterministic local MP4 and measured A/V QA.

The source queue is authoritative for previous explicit *local preview* approval.
This adds a separate immutable render claim per acquisition job. A crashed render
is never retried automatically; existing output files are never overwritten.
No YouTube, yt-dlp, LLM, uploader, publication or production-ready transition.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata,
    evaluate_source_ranges, load_policy_rules,
)
from holoclip.domain import ClipTimeline, SourceType
from holoclip.providers.source.base import SourceMetadata
from holoclip.render_plan import (
    _no_links, compile_local_render_plan, render_local_preview,
)
from holoclip.render_source_queue import DEFAULT_QUEUE, _connect, _queue_path
from holoclip.render_source_queue import _recovery_cache_verified
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_subtitles import export_cut_aligned_srt
from holoclip.render_subtitle_burnin import burn_captioned_preview
from holoclip.render_audio_finish import level_preview_audio
from holoclip.render_visual_style import style_evidence
from holoclip.render_quality_diagnostics import evaluate_local_edit_quality
from holoclip.render_quality_review import store_review_ledger
from holoclip.render_review_calibration import archive_incremental_review
from holoclip.render_source_context import compare_local_source_context
from holoclip.render_review_triage import store_local_review_triage
from holoclip.render_audio_ab_preview import create_audio_ab_preview
from holoclip.render_subtitle_ab_preview import create_subtitle_ab_preview
from holoclip.render_cut_ab_preview import create_cut_seam_ab_preview

DEFAULT_PREVIEW_ROOT = Path("data/render-previews")
DEFAULT_TRANSCRIPT_ROOT = Path("data/transcripts")
_MAX_AUTO_SECONDS = 180.0
_JOB_ID = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_RENDER_STATES = frozenset({"RENDERING", "PAUSED", "QA_PASSED"})


def _db(queue: Path, *, write: bool) -> sqlite3.Connection | None:
    path = _queue_path(queue)
    existing = _connect(path, create=False)
    if existing is None:
        return None
    if not write:
        return existing
    existing.close()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_jobs ("
            "job_id TEXT PRIMARY KEY, state TEXT NOT NULL, token TEXT NOT NULL,"
            "reason TEXT NOT NULL, output_path TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, render_plan_id TEXT NOT NULL,"
            "measured_duration REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_caption_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "cue_count INTEGER NOT NULL, dropped_cue_count INTEGER NOT NULL,"
            "output_sha256 TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_burnin_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, subtitle_sha256 TEXT NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_audio_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, cut_count INTEGER NOT NULL,"
            "max_abs_gain_db REAL NOT NULL,"
            "before_mean_spread_db REAL, after_mean_spread_db REAL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_visual_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "style_version TEXT NOT NULL, zoom_cut_count INTEGER NOT NULL,"
            "dip_transition_count INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_quality_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "version TEXT NOT NULL, base_sha256 TEXT NOT NULL,"
            "final_sha256 TEXT NOT NULL, black_interval_count INTEGER NOT NULL,"
            "freeze_interval_count INTEGER NOT NULL,"
            "subtitle_checked INTEGER NOT NULL,"
            "subtitle_fast_cue_count INTEGER NOT NULL,"
            "subtitle_overflow_count INTEGER NOT NULL,"
            "audio_sample_mean_dbfs REAL, audio_sample_peak_dbfs REAL,"
            "flag_count INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_review_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, candidate_count INTEGER NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_review_archive_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, candidate_count INTEGER NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_source_context_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, sampled_event_count INTEGER NOT NULL,"
            "matched_count INTEGER NOT NULL, no_match_count INTEGER NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_review_triage_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "output_sha256 TEXT NOT NULL, candidate_count INTEGER NOT NULL,"
            "review_first_count INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_audio_ab_preview_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL, sample_count INTEGER NOT NULL,"
            "baseline_recovery_verified INTEGER NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_subtitle_ab_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL, variant_count INTEGER NOT NULL,"
            "cue_number INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS render_cut_ab_evidence ("
            "job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL, variant_count INTEGER NOT NULL,"
            "seam_seconds REAL, baseline_verified INTEGER NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.commit()
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("LOCAL_RENDER_AUTO_QA_STORAGE_UNAVAILABLE") from exc


def _report(status: str) -> dict:
    return {
        "status": status,
        "job_id": None,
        "rendered_count": 0,
        "youtube_contacted": False,
        "media_downloaded": False,
        "publication_performed": False,
        "human_editorial_qa_completed": False,
        "ready": False,
        "production_ready": False,
    }


def inspect_completed_source_renders(*, queue: Path = DEFAULT_QUEUE) -> dict:
    """Read-only counts; never creates queue, output or render state table."""
    report = _report("LOCAL_RENDER_AUTO_QA_QUEUE_NOT_CREATED")
    conn = _db(queue, write=False)
    if conn is None:
        return report
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_jobs'"
        ).fetchone() is not None
        counts = {"PENDING": 0, "RENDERING": 0, "PAUSED": 0, "QA_PASSED": 0}
        rows = conn.execute(
            "SELECT job_id FROM queue_jobs WHERE state='COMPLETE' "
            "ORDER BY created_at,job_id LIMIT 1001"
        ).fetchall()
        if len(rows) > 1000:
            raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
        state_map = {}
        if has_table:
            records = conn.execute(
                "SELECT job_id,state FROM render_jobs LIMIT 1001"
            ).fetchall()
            if len(records) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for row in records:
                if row["state"] not in _RENDER_STATES:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_STATE_UNTRUSTED")
                state_map[row["job_id"]] = row["state"]
        for row in rows:
            if not _JOB_ID.fullmatch(row["job_id"]):
                raise ValueError("LOCAL_RENDER_AUTO_QA_JOB_ID_INVALID")
            state = state_map.get(row["job_id"], "PENDING")
            counts[state] += 1
        caption_counts = {
            "srt_created": 0, "unavailable": 0,
            "no_full_utterances": 0,
        }
        caption_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_caption_evidence'"
        ).fetchone() is not None
        if caption_table:
            captions = conn.execute(
                "SELECT status FROM render_caption_evidence LIMIT 1001"
            ).fetchall()
            if len(captions) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in captions:
                status = item["status"]
                if status == "LOCAL_SUBTITLE_SRT_CREATED_UNVERIFIED_ASR":
                    caption_counts["srt_created"] += 1
                elif status in {"LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED",
                              "LOCAL_SUBTITLE_NOT_AVAILABLE"}:
                    caption_counts["unavailable"] += 1
                elif status == "LOCAL_SUBTITLE_NO_FULL_UTTERANCES":
                    caption_counts["no_full_utterances"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_CAPTION_STATE_UNTRUSTED")
        report["subtitle_counts"] = caption_counts
        burnin_counts = {"created": 0, "unavailable": 0, "failed": 0}
        burnin_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_burnin_evidence'"
        ).fetchone() is not None
        if burnin_table:
            burnin_rows = conn.execute(
                "SELECT status FROM render_burnin_evidence LIMIT 1001"
            ).fetchall()
            if len(burnin_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in burnin_rows:
                if item["status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL":
                    burnin_counts["created"] += 1
                elif item["status"] == "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE":
                    burnin_counts["unavailable"] += 1
                elif item["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED":
                    burnin_counts["failed"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_BURNIN_STATE_UNTRUSTED")
        report["burnin_counts"] = burnin_counts
        audio_counts = {"leveled": 0, "failed": 0}
        audio_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_audio_evidence'"
        ).fetchone() is not None
        if audio_table:
            audio_rows = conn.execute(
                "SELECT status FROM render_audio_evidence LIMIT 1001"
            ).fetchall()
            if len(audio_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in audio_rows:
                if item["status"] == "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL":
                    audio_counts["leveled"] += 1
                elif item["status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED":
                    audio_counts["failed"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_AUDIO_STATE_UNTRUSTED")
        report["audio_counts"] = audio_counts
        visual_counts = {"encoded": 0, "unverified": 0}
        visual_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_visual_evidence'"
        ).fetchone() is not None
        if visual_table:
            visual_rows = conn.execute(
                "SELECT status FROM render_visual_evidence LIMIT 1001"
            ).fetchall()
            if len(visual_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in visual_rows:
                if item["status"] == "LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY":
                    visual_counts["encoded"] += 1
                elif item["status"] == "LOCAL_RENDER_VISUAL_UNVERIFIED":
                    visual_counts["unverified"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_VISUAL_STATE_UNTRUSTED")
        report["visual_counts"] = visual_counts
        quality_counts = {
            "flags": 0, "no_flags": 0, "partial": 0, "unavailable": 0,
        }
        quality_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_quality_evidence'"
        ).fetchone() is not None
        if quality_table:
            quality_rows = conn.execute(
                "SELECT status FROM render_quality_evidence LIMIT 1001"
            ).fetchall()
            if len(quality_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            mapping = {
                "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS": "flags",
                "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS": "no_flags",
                "LOCAL_EDIT_DIAGNOSTIC_MEASURED_PARTIAL": "partial",
                "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE": "unavailable",
            }
            for item in quality_rows:
                if item["status"] not in mapping:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_QUALITY_STATE_UNTRUSTED")
                quality_counts[mapping[item["status"]]] += 1
        report["quality_counts"] = quality_counts
        review_counts = {"created": 0, "unavailable": 0}
        review_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_review_evidence'"
        ).fetchone() is not None
        if review_table:
            review_rows = conn.execute(
                "SELECT status FROM render_review_evidence LIMIT 1001"
            ).fetchall()
            if len(review_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in review_rows:
                if item["status"] == "LOCAL_EDIT_REVIEW_LEDGER_CREATED_UNVERIFIED_EDITORIAL":
                    review_counts["created"] += 1
                elif item["status"] == "LOCAL_EDIT_REVIEW_LEDGER_UNAVAILABLE":
                    review_counts["unavailable"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_REVIEW_STATE_UNTRUSTED")
        report["review_counts"] = review_counts
        archive_counts = {"created": 0, "unavailable": 0}
        archive_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_review_archive_evidence'"
        ).fetchone() is not None
        if archive_table:
            archive_rows = conn.execute(
                "SELECT status FROM render_review_archive_evidence LIMIT 1001"
            ).fetchall()
            if len(archive_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in archive_rows:
                if item["status"] == "LOCAL_EDIT_ARCHIVE_CREATED_UNLABELLED":
                    archive_counts["created"] += 1
                elif item["status"] == "LOCAL_EDIT_ARCHIVE_UNAVAILABLE":
                    archive_counts["unavailable"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_ARCHIVE_STATE_UNTRUSTED")
        report["archive_counts"] = archive_counts
        source_context_counts = {"sampled": 0, "no_video_events": 0, "unavailable": 0}
        source_context_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_source_context_evidence'"
        ).fetchone() is not None
        if source_context_table:
            source_context_rows = conn.execute(
                "SELECT status FROM render_source_context_evidence LIMIT 1001"
            ).fetchall()
            if len(source_context_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            mapping = {
                "LOCAL_SOURCE_CONTEXT_SAMPLED_UNLABELLED": "sampled",
                "LOCAL_SOURCE_CONTEXT_NO_VIDEO_EVENTS": "no_video_events",
                "LOCAL_SOURCE_CONTEXT_UNAVAILABLE": "unavailable",
            }
            for item in source_context_rows:
                if item["status"] not in mapping:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_SOURCE_CONTEXT_STATE_UNTRUSTED")
                source_context_counts[mapping[item["status"]]] += 1
        report["source_context_counts"] = source_context_counts
        triage_counts = {"created": 0, "unavailable": 0}
        triage_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_review_triage_evidence'"
        ).fetchone() is not None
        if triage_table:
            triage_rows = conn.execute(
                "SELECT status FROM render_review_triage_evidence LIMIT 1001"
            ).fetchall()
            if len(triage_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            for item in triage_rows:
                if item["status"] == "LOCAL_EDIT_TRIAGE_CREATED_UNVERIFIED_EDITORIAL":
                    triage_counts["created"] += 1
                elif item["status"] == "LOCAL_EDIT_TRIAGE_UNAVAILABLE":
                    triage_counts["unavailable"] += 1
                else:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_TRIAGE_STATE_UNTRUSTED")
        report["triage_counts"] = triage_counts
        ab_counts = {"created": 0, "not_applicable": 0, "unavailable": 0}
        ab_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_audio_ab_preview_evidence'"
        ).fetchone() is not None
        if ab_table:
            ab_rows = conn.execute(
                "SELECT status FROM render_audio_ab_preview_evidence LIMIT 1001"
            ).fetchall()
            if len(ab_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            mapping = {
                "LOCAL_AB_PREVIEW_CREATED_UNVERIFIED_EDITORIAL": "created",
                "LOCAL_AB_PREVIEW_NOT_APPLICABLE": "not_applicable",
                "LOCAL_AB_PREVIEW_UNAVAILABLE": "unavailable",
            }
            for item in ab_rows:
                if item["status"] not in mapping:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_AB_STATE_UNTRUSTED")
                ab_counts[mapping[item["status"]]] += 1
        report["ab_preview_counts"] = ab_counts
        subtitle_ab_counts = {
            "created": 0, "not_applicable": 0, "unavailable": 0,
        }
        subtitle_ab_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_subtitle_ab_evidence'"
        ).fetchone() is not None
        if subtitle_ab_table:
            subtitle_ab_rows = conn.execute(
                "SELECT status FROM render_subtitle_ab_evidence LIMIT 1001"
            ).fetchall()
            if len(subtitle_ab_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            mapping = {
                "LOCAL_SUBTITLE_AB_CREATED_UNVERIFIED_EDITORIAL": "created",
                "LOCAL_SUBTITLE_AB_NOT_APPLICABLE": "not_applicable",
                "LOCAL_SUBTITLE_AB_UNAVAILABLE": "unavailable",
            }
            for entry in subtitle_ab_rows:
                if entry["status"] not in mapping:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_SUBTITLE_AB_STATE_UNTRUSTED")
                subtitle_ab_counts[mapping[entry["status"]]] += 1
        report["subtitle_ab_counts"] = subtitle_ab_counts
        cut_ab_counts = {"created": 0, "not_applicable": 0, "unavailable": 0}
        has_cut_ab = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='render_cut_ab_evidence'"
        ).fetchone() is not None
        if has_cut_ab:
            cut_ab_rows = conn.execute(
                "SELECT status FROM render_cut_ab_evidence LIMIT 1001"
            ).fetchall()
            if len(cut_ab_rows) > 1000:
                raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_TOO_LARGE")
            states = {
                "LOCAL_CUT_AB_CREATED_UNVERIFIED_EDITORIAL": "created",
                "LOCAL_CUT_AB_NOT_APPLICABLE": "not_applicable",
                "LOCAL_CUT_AB_UNAVAILABLE": "unavailable",
            }
            for row in cut_ab_rows:
                if row["status"] not in states:
                    raise ValueError("LOCAL_RENDER_AUTO_QA_CUT_AB_STATE_UNTRUSTED")
                cut_ab_counts[states[row["status"]]] += 1
        report["cut_ab_counts"] = cut_ab_counts
        report["counts"] = counts
        report["source_complete_count"] = len(rows)
        report["status"] = "LOCAL_RENDER_AUTO_QA_STATUS"
        return report
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_RENDER_AUTO_QA_STORAGE_UNAVAILABLE") from exc
    finally:
        conn.close()


def _claim(queue: Path) -> tuple[dict | None, str, str]:
    """Atomic claim: a RENDERING/PAUSED job blocks further work globally."""
    conn = _db(queue, write=True)
    if conn is None:
        return None, "", "LOCAL_RENDER_AUTO_QA_QUEUE_NOT_CREATED"
    try:
        conn.execute("BEGIN IMMEDIATE")
        corrupted = conn.execute(
            "SELECT 1 FROM render_jobs WHERE state NOT IN "
            "('RENDERING','PAUSED','QA_PASSED') LIMIT 1"
        ).fetchone()
        if corrupted:
            raise ValueError("LOCAL_RENDER_AUTO_QA_STATE_UNTRUSTED")
        blocked = conn.execute(
            "SELECT state FROM render_jobs WHERE state IN ('PAUSED','RENDERING') "
            "LIMIT 1"
        ).fetchone()
        if blocked:
            return None, "", "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS"
        rows = conn.execute(
            "SELECT q.* FROM queue_jobs q LEFT JOIN render_jobs r "
            "ON q.job_id=r.job_id "
            "WHERE q.state='COMPLETE' AND r.job_id IS NULL "
            "ORDER BY q.created_at,q.job_id LIMIT 1"
        ).fetchone()
        if rows is None:
            return None, "", "LOCAL_RENDER_AUTO_QA_NO_READY_JOBS"
        row = dict(rows)
        if not _JOB_ID.fullmatch(row["job_id"]):
            raise ValueError("LOCAL_RENDER_AUTO_QA_JOB_ID_INVALID")
        token = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO render_jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (row["job_id"], "RENDERING", token, "NONE", "", "", "",
             0.0, time.time()),
        )
        conn.commit()
        return row, token, "LOCAL_RENDER_AUTO_QA_CLAIMED"
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_RENDER_AUTO_QA_STORAGE_UNAVAILABLE") from exc
    finally:
        conn.close()


def _finish(
    queue: Path, *, job_id: str, token: str, state: str, reason: str,
    output: Path | None = None, sha256: str = "", plan_id: str = "",
    duration: float = 0.0, caption_evidence: dict | None = None,
    burnin_evidence: dict | None = None,
    audio_evidence: dict | None = None,
    visual_evidence: dict | None = None,
    quality_evidence: dict | None = None,
    review_evidence: dict | None = None,
    archive_evidence: dict | None = None,
    source_context_evidence: dict | None = None,
    triage_evidence: dict | None = None,
    ab_evidence: dict | None = None,
    subtitle_ab_evidence: dict | None = None,
    cut_ab_evidence: dict | None = None,
) -> None:
    if state not in {"PAUSED", "QA_PASSED"} or reason not in {
        "NONE", "LOCAL_RENDER_AUTO_QA_VALIDATION_FAILED",
        "LOCAL_RENDER_AUTO_QA_RENDER_FAILED",
    }:
        raise ValueError("LOCAL_RENDER_AUTO_QA_STATE_UNTRUSTED")
    conn = _db(queue, write=True)
    if conn is None:
        raise ValueError("LOCAL_RENDER_AUTO_QA_QUEUE_NOT_CREATED")
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE render_jobs SET state=?,token='',reason=?,output_path=?,"
            "output_sha256=?,render_plan_id=?,measured_duration=?,updated_at=? "
            "WHERE job_id=? AND state='RENDERING' AND token=?",
            (state, reason, str(output) if output else "", sha256, plan_id,
             duration, time.time(), job_id, token),
        )
        if changed.rowcount != 1:
            raise ValueError("LOCAL_RENDER_AUTO_QA_CLAIM_LOST")
        if state == "QA_PASSED" and caption_evidence is not None:
            # The subtitle sidecar is optional evidence, not media QA.
            conn.execute(
                "INSERT INTO render_caption_evidence VALUES (?,?,?,?,?,?)",
                (
                    job_id, str(caption_evidence["status"]),
                    int(caption_evidence["cue_count"]),
                    int(caption_evidence["dropped_cue_count"]),
                    str(caption_evidence["output_sha256"] or ""),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and burnin_evidence is not None:
            conn.execute(
                "INSERT INTO render_burnin_evidence VALUES (?,?,?,?,?)",
                (
                    job_id, str(burnin_evidence["status"]),
                    str(burnin_evidence["output_sha256"]),
                    str(burnin_evidence["subtitle_sha256"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and audio_evidence is not None:
            conn.execute(
                "INSERT INTO render_audio_evidence VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id, str(audio_evidence["status"]),
                    str(audio_evidence["output_sha256"]),
                    int(audio_evidence["cut_count"]),
                    float(audio_evidence["max_abs_gain_db"]),
                    audio_evidence["before_mean_spread_db"],
                    audio_evidence["after_mean_spread_db"],
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and visual_evidence is not None:
            conn.execute(
                "INSERT INTO render_visual_evidence VALUES (?,?,?,?,?,?)",
                (
                    job_id, str(visual_evidence["status"]),
                    str(visual_evidence["style_version"]),
                    int(visual_evidence["zoom_cut_count"]),
                    int(visual_evidence["dip_transition_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and quality_evidence is not None:
            conn.execute(
                "INSERT INTO render_quality_evidence VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, str(quality_evidence["status"]),
                    str(quality_evidence["version"]),
                    str(quality_evidence["base_sha256"]),
                    str(quality_evidence["final_sha256"]),
                    int(quality_evidence["black_interval_count"]),
                    int(quality_evidence["freeze_interval_count"]),
                    int(bool(quality_evidence["subtitle_checked"])),
                    int(quality_evidence["subtitle_fast_cue_count"]),
                    int(quality_evidence["subtitle_overflow_count"]),
                    quality_evidence["audio_sample_mean_dbfs"],
                    quality_evidence["audio_sample_peak_dbfs"],
                    int(quality_evidence["flag_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and review_evidence is not None:
            conn.execute(
                "INSERT INTO render_review_evidence VALUES (?,?,?,?,?)",
                (
                    job_id, str(review_evidence["status"]),
                    str(review_evidence["sha256"]),
                    int(review_evidence["candidate_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and archive_evidence is not None:
            conn.execute(
                "INSERT INTO render_review_archive_evidence VALUES (?,?,?,?,?)",
                (
                    job_id, str(archive_evidence["status"]),
                    str(archive_evidence["sha256"]),
                    int(archive_evidence["candidate_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and source_context_evidence is not None:
            conn.execute(
                "INSERT INTO render_source_context_evidence VALUES (?,?,?,?,?,?,?)",
                (
                    job_id, str(source_context_evidence["status"]),
                    str(source_context_evidence["sha256"]),
                    int(source_context_evidence["sampled_event_count"]),
                    int(source_context_evidence["matched_count"]),
                    int(source_context_evidence["no_match_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and triage_evidence is not None:
            conn.execute(
                "INSERT INTO render_review_triage_evidence VALUES (?,?,?,?,?,?)",
                (
                    job_id, str(triage_evidence["status"]),
                    str(triage_evidence["sha256"]),
                    int(triage_evidence["candidate_count"]),
                    int(triage_evidence["review_first_count"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and ab_evidence is not None:
            conn.execute(
                "INSERT INTO render_audio_ab_preview_evidence VALUES (?,?,?,?,?,?)",
                (
                    job_id, str(ab_evidence["status"]),
                    str(ab_evidence["sha256"]),
                    int(ab_evidence["sample_count"]),
                    int(bool(ab_evidence["baseline_recovery_verified"])),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and subtitle_ab_evidence is not None:
            conn.execute(
                "INSERT INTO render_subtitle_ab_evidence VALUES (?,?,?,?,?,?)",
                (
                    job_id, str(subtitle_ab_evidence["status"]),
                    str(subtitle_ab_evidence["sha256"]),
                    int(subtitle_ab_evidence["variant_count"]),
                    int(subtitle_ab_evidence["cue_number"]),
                    time.time(),
                ),
            )
        if state == "QA_PASSED" and cut_ab_evidence is not None:
            conn.execute(
                "INSERT INTO render_cut_ab_evidence VALUES (?,?,?,?,?,?,?)",
                (
                    job_id, str(cut_ab_evidence["status"]),
                    str(cut_ab_evidence["sha256"]),
                    int(cut_ab_evidence["variant_count"]),
                    cut_ab_evidence["seam_seconds"],
                    int(bool(cut_ab_evidence["baseline_recovery_verified"])),
                    time.time(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _offline_render_policy(timeline: ClipTimeline, sources: list,
                           policy_file: Path) -> None:
    """Recheck cached source RENDER restrictions; unknown metadata fails closed."""
    rules = load_policy_rules(_no_links(policy_file))
    for source in sources:
        if source.asset.source_type not in {SourceType.STREAM, SourceType.VIDEO}:
            raise ValueError("LOCAL_RENDER_AUTO_QA_ORIGINAL_ID_UNVERIFIED")
        receipt = _no_links(source.path.parent / "media.info.json")
        if not receipt.is_file() or not 0 < receipt.stat().st_size <= 1048576:
            raise ValueError("LOCAL_RENDER_AUTO_QA_RECEIPT_UNVERIFIED")
        try:
            doc = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError("LOCAL_RENDER_AUTO_QA_RECEIPT_UNVERIFIED") from exc
        if (
            not isinstance(doc, dict)
            or doc.get("id") != source.asset.external_id
            or not isinstance(doc.get("title"), str)
            or not doc["title"].strip()
            or not isinstance(doc.get("description"), str)
        ):
            raise ValueError("LOCAL_RENDER_AUTO_QA_POLICY_METADATA_MISSING")
        metadata = SourceMetadata(
            provider="yt-dlp", url=f"https://www.youtube.com/watch?v={source.asset.external_id}",
            external_id=source.asset.external_id, title=doc["title"],
            description=doc["description"], channel_id=doc.get("channel_id"),
            channel=doc.get("channel"), duration=doc.get("duration"),
            live_status=doc.get("live_status"),
            availability=doc.get("availability"),
        )
        if metadata.live_status in {"is_live", "is_upcoming", "post_live"}:
            raise ValueError("LOCAL_RENDER_AUTO_QA_SOURCE_NOT_ARCHIVED")
        metadata_policy = evaluate_source_metadata(
            metadata, rules=rules, operation=PolicyOperation.RENDER,
        )
        if metadata_policy.decision != PolicyDecision.ALLOW:
            raise ValueError("LOCAL_RENDER_AUTO_QA_POLICY_NOT_ALLOWED")
        ranges = [
            (s.source_start, s.source_end)
            for s in timeline.segments if s.source_asset_id == source.asset.id
        ]
        range_policy = evaluate_source_ranges(
            metadata, ranges=ranges, rules=rules, operation=PolicyOperation.RENDER,
            deferred_findings=metadata_policy.findings,
        )
        if range_policy.decision != PolicyDecision.ALLOW:
            raise ValueError("LOCAL_RENDER_AUTO_QA_RANGE_POLICY_NOT_ALLOWED")


def advance_completed_source_renders(
    *, queue: Path = DEFAULT_QUEUE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    execute: bool = False,
) -> dict:
    """At most one offline MP4 technical preview, never any network or publish."""
    if not execute:
        overview = inspect_completed_source_renders(queue=queue)
        overview["rendered_count"] = 0
        return overview
    started_monotonic = time.monotonic()
    root = _no_links(preview_root)
    if root.exists() and not root.is_dir():
        raise ValueError("LOCAL_RENDER_AUTO_QA_OUTPUT_ROOT_UNSAFE")
    row, token, status = _claim(queue)
    report = _report(status)
    if row is None:
        return report
    job_id = row["job_id"]
    report["job_id"] = job_id
    try:
        timeline = ClipTimeline.model_validate_json(row["timeline_json"])
        if timeline.estimated_duration > _MAX_AUTO_SECONDS:
            raise ValueError("LOCAL_RENDER_AUTO_QA_DURATION_BUDGET")
        database = _no_links(Path(row["database_path"]))
        cache = _no_links(Path(row["cache_root"]))
        if not _recovery_cache_verified(timeline, database, cache):
            raise ValueError("LOCAL_RENDER_AUTO_QA_FULL_SOURCE_UNVERIFIED")
        sources = resolve_cached_render_sources(
            timeline, database=database, cache_root=cache,
            approve_local_preview=True,
        )
        _offline_render_policy(timeline, sources, Path(row["policy_file"]))
        root.mkdir(parents=True, exist_ok=True)
        output = _no_links(root / f"{job_id}.mp4")
        plan = compile_local_render_plan(timeline, sources, output)
        details = render_local_preview(plan)
        if details["status"] != "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY":
            raise ValueError("LOCAL_RENDER_AUTO_QA_RENDER_UNQUALIFIED")
        expected_style = style_evidence(timeline)
        measured_style = details.get("visual_style")
        visual_evidence = {
            "status": (
                "LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY"
                if measured_style == expected_style
                else "LOCAL_RENDER_VISUAL_UNVERIFIED"
            ),
            "style_version": (
                expected_style["version"] if measured_style == expected_style else ""
            ),
            "zoom_cut_count": expected_style["zoom_cut_count"],
            "dip_transition_count": expected_style["dip_transition_count"],
        }
        # Never conflate transcript availability or ASR accuracy with technical
        # MP4 readiness. Missing/ambiguous ASR does not falsify video QA.
        caption_evidence = export_cut_aligned_srt(
            timeline, sources, transcript_root=transcript_root,
            mp4_output=output,
        )
        # Per-cut bounded corrections happen offline, only after a successful
        # original MP4. Failures never demote the original technical QA.
        audio_evidence = level_preview_audio(
            mp4_output=output,
            video_sha256=str(details["output_sha256"]),
            timeline=timeline,
        )
        audio_ok = (
            audio_evidence["status"] == "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL"
        )
        burn_input = output.with_name(f"{output.stem}.leveled.mp4") if audio_ok else output
        burn_hash = (
            str(audio_evidence["output_sha256"]) if audio_ok
            else str(details["output_sha256"])
        )
        # Burn subtitles onto leveled audio if available, otherwise the
        # original preview. Original MP4 and SRT are never modified.
        try:
            burnin_evidence = burn_captioned_preview(
                mp4_output=burn_input,
                video_sha256=burn_hash,
                subtitle_sha256=caption_evidence["output_sha256"],
            )
        except (ValueError, OSError, TypeError, UnicodeError):
            burnin_evidence = {
                "status": "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED",
                "output_sha256": "",
                "subtitle_sha256": str(caption_evidence["output_sha256"] or ""),
            }
        # Independently inspect the base uncaptioned video (black/frozen
        # frames), final selected audio and the verified SRT text clock.
        # A warning is NOT cause to silently cut away a potentially intended
        # pause, dark scene or dramatic reaction.
        final_output = burn_input
        final_sha = burn_hash
        if burnin_evidence["status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL":
            final_output = output.with_name(f"{output.stem}.captioned.mp4")
            final_sha = str(burnin_evidence["output_sha256"])
        quality_evidence = evaluate_local_edit_quality(
            mp4_output=output,
            video_sha256=str(details["output_sha256"]),
            final_output=final_output, final_sha256=final_sha,
            timeline=timeline,
            caption_sha256=caption_evidence["output_sha256"],
            # A conservative reserve under the existing 55-minute Windows
            # scheduled-task limit. Do not risk its hard termination solely
            # for optional quality diagnostics.
            scan_allowed=time.monotonic() - started_monotonic < 3000.0,
        )
        # Review ledger is a create-only, machine-readable index of the
        # measured signal positions; it cannot approve or repair media.
        review_evidence = store_review_ledger(
            mp4_output=output, video_sha256=str(details["output_sha256"]),
            final_sha256=final_sha,
            diagnostic_status=str(quality_evidence["status"]),
            timeline=timeline,
            observed=quality_evidence.get("review_observations", []),
        )
        # Cheap append-only evaluation backlog handoff. Rehashes only the
        # small, just-created review JSON; later paged calibration independently
        # revalidates both video artifacts. Never delays a successful render
        # for missing optional review data.
        archive_evidence = archive_incremental_review(
            mp4_output=output,
            review_sha256=str(review_evidence["sha256"]),
            video_sha256=str(details["output_sha256"]),
            final_sha256=final_sha,
        )
        # Sample original *local* source windows for up to three previously
        # measured black/freeze events. This is independent review evidence:
        # matching origin signals are not proofs of correctness or defects.
        # Reserve time for completing the Windows worker transaction.
        source_context_evidence = compare_local_source_context(
            plan=plan, review_sha256=str(review_evidence["sha256"]),
            video_sha256=str(details["output_sha256"]),
            observed=quality_evidence.get("review_observations", []),
            scan_allowed=time.monotonic() - started_monotonic < 3000.0,
        )
        # The new triage step only reads small immutable event sidecars;
        # it sorts review work and issues non-executable inspection advice.
        triage_evidence = store_local_review_triage(
            mp4_output=output,
            review_sha256=str(review_evidence["sha256"]),
            source_context_sha256=str(source_context_evidence["sha256"]),
        )
        # Only a previously measured audio_peak can yield a bounded
        # experimental A/B audio snippet. All other warnings stay advisory.
        # This never replaces an original, leveled or captioned full MP4.
        ab_evidence = create_audio_ab_preview(
            mp4_output=output, video_sha256=str(details["output_sha256"]),
            final_output=final_output, final_sha256=final_sha,
            triage_sha256=str(triage_evidence["sha256"]),
            scan_allowed=time.monotonic() - started_monotonic < 3000.0,
        )
        # Sample the first identified fast subtitle in two fixed styles.
        # This reads the original SRT and an uncaptioned QA-checked MP4;
        # full-length A/V and subtitle timing are never modified.
        subtitle_ab_evidence = create_subtitle_ab_preview(
            mp4_output=output, video_sha256=str(details["output_sha256"]),
            input_video=burn_input, input_sha256=burn_hash,
            final_sha256=final_sha,
            subtitle_sha256=str(caption_evidence["output_sha256"]),
            triage_sha256=str(triage_evidence["sha256"]),
            scan_allowed=time.monotonic() - started_monotonic < 3000.0,
        )
        # Compare an actual join against a tiny experimental visual treatment
        # using *only* the original QA-checked output, never source downloads,
        # cut-clock changes or alternate full-length media. Priorite flagged
        # boundaries; skip existing dip fades; preserve task time for commit.
        cut_ab_evidence = create_cut_seam_ab_preview(
            plan=plan, video_sha256=str(details["output_sha256"]),
            triage_sha256=str(triage_evidence["sha256"]),
            scan_allowed=time.monotonic() - started_monotonic < 3000.0,
        )
        _finish(
            queue, job_id=job_id, token=token, state="QA_PASSED", reason="NONE",
            output=output, sha256=str(details["output_sha256"]),
            plan_id=plan.id, duration=float(details["measured_duration"]),
            caption_evidence=caption_evidence, burnin_evidence=burnin_evidence,
            audio_evidence=audio_evidence, visual_evidence=visual_evidence,
            quality_evidence=quality_evidence,
            review_evidence=review_evidence,
            archive_evidence=archive_evidence,
            source_context_evidence=source_context_evidence,
            triage_evidence=triage_evidence,
            ab_evidence=ab_evidence,
            subtitle_ab_evidence=subtitle_ab_evidence,
            cut_ab_evidence=cut_ab_evidence,
        )
        report["cut_ab_status"] = cut_ab_evidence["status"]
        report["cut_ab_sha256"] = cut_ab_evidence["sha256"]
        report["cut_ab_seam_seconds"] = cut_ab_evidence["seam_seconds"]
        report["cut_ab_variant_count"] = cut_ab_evidence["variant_count"]
        report["cut_ab_baseline_recovery_verified"] = (
            cut_ab_evidence["baseline_recovery_verified"]
        )
        report["subtitle_ab_status"] = subtitle_ab_evidence["status"]
        report["subtitle_ab_sha256"] = subtitle_ab_evidence["sha256"]
        report["subtitle_ab_variant_count"] = subtitle_ab_evidence["variant_count"]
        report["subtitle_ab_cue_number"] = subtitle_ab_evidence["cue_number"]
        report["ab_preview_status"] = ab_evidence["status"]
        report["ab_preview_sha256"] = ab_evidence["sha256"]
        report["ab_preview_sample_count"] = ab_evidence["sample_count"]
        report["ab_preview_baseline_recovery_verified"] = (
            ab_evidence["baseline_recovery_verified"]
        )
        report["triage_status"] = triage_evidence["status"]
        report["triage_sha256"] = triage_evidence["sha256"]
        report["triage_candidate_count"] = triage_evidence["candidate_count"]
        report["triage_review_first_count"] = triage_evidence["review_first_count"]
        report["source_context_status"] = source_context_evidence["status"]
        report["source_context_sha256"] = source_context_evidence["sha256"]
        report["source_context_sampled_event_count"] = (
            source_context_evidence["sampled_event_count"]
        )
        report["source_context_matched_count"] = source_context_evidence["matched_count"]
        report["source_context_no_match_count"] = source_context_evidence["no_match_count"]
        report["archive_status"] = archive_evidence["status"]
        report["archive_sha256"] = archive_evidence["sha256"]
        report["archive_candidate_count"] = archive_evidence["candidate_count"]
        report["review_status"] = review_evidence["status"]
        report["review_candidate_count"] = review_evidence["candidate_count"]
        report["review_sha256"] = review_evidence["sha256"]
        report["quality_status"] = quality_evidence["status"]
        report["quality_flag_count"] = quality_evidence["flag_count"]
        report["quality_black_interval_count"] = quality_evidence["black_interval_count"]
        report["quality_freeze_interval_count"] = quality_evidence["freeze_interval_count"]
        report["quality_subtitle_fast_cue_count"] = (
            quality_evidence["subtitle_fast_cue_count"]
        )
        report["visual_status"] = visual_evidence["status"]
        report["visual_zoom_cut_count"] = visual_evidence["zoom_cut_count"]
        report["visual_dip_transition_count"] = visual_evidence["dip_transition_count"]
        report["audio_status"] = audio_evidence["status"]
        report["audio_output_sha256"] = audio_evidence["output_sha256"]
        report["audio_cut_count"] = audio_evidence["cut_count"]
        report["burnin_status"] = burnin_evidence["status"]
        report["burnin_output_sha256"] = burnin_evidence["output_sha256"]
        report["subtitle_status"] = caption_evidence["status"]
        report["subtitle_cue_count"] = caption_evidence["cue_count"]
        report["subtitle_dropped_cue_count"] = caption_evidence["dropped_cue_count"]
        report["status"] = "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
        report["rendered_count"] = 1
        report["output_sha256"] = details["output_sha256"]
        report["plan_id"] = plan.id
        report["measured_duration"] = details["measured_duration"]
        return report
    except (ValueError, OSError, TypeError, UnicodeError, sqlite3.Error):
        _finish(
            queue, job_id=job_id, token=token, state="PAUSED",
            reason="LOCAL_RENDER_AUTO_QA_VALIDATION_FAILED",
        )
        report["status"] = "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
        return report
