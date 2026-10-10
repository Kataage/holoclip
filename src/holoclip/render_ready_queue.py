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

DEFAULT_PREVIEW_ROOT = Path("data/render-previews")
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
    duration: float = 0.0,
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
    execute: bool = False,
) -> dict:
    """At most one offline MP4 technical preview, never any network or publish."""
    if not execute:
        overview = inspect_completed_source_renders(queue=queue)
        overview["rendered_count"] = 0
        return overview
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
        _finish(
            queue, job_id=job_id, token=token, state="QA_PASSED", reason="NONE",
            output=output, sha256=str(details["output_sha256"]),
            plan_id=plan.id, duration=float(details["measured_duration"]),
        )
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
