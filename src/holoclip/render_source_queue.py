"""Durable, operator-enrolled missing render-video queue.

Exactly one paced source attempt per invocation. SQLite BEGIN IMMEDIATE prevents
two workers claiming the same job. A lost worker stays RUNNING until explicit
offline reconciliation; it is never automatically retried after a crash.
Unknown yt-dlp failures and rate-limit/captcha responses fail closed.

No media, LAN, or outbound action occurs in preview, enrollment or status.
"""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from pathlib import Path

from holoclip.domain import ClipTimeline, stable_id
from holoclip.providers.source.yt_dlp import YtDlpError, YtDlpTimeout
from holoclip.render_plan import _no_links, _probe
from holoclip.render_source_resolver import _canonical_assets, _find_complete_cache
from holoclip.render_source_acquisition import (
    MAX_DURATION_SECONDS, MAX_FILESIZE_MB,
    _DEFAULT_CACHE, _DEFAULT_DB, _DEFAULT_POLICY, _inventory,
    acquire_one_missing_render_source,
)

DEFAULT_QUEUE = Path("data/runtime/render-source-queue.sqlite3")
_QUEUE_VERSION = "holoclip-render-source-queue-v1"
_STATUS = frozenset({"QUEUED", "RUNNING", "DEFERRED", "PAUSED", "COMPLETE"})
_DEFER_ERRORS = frozenset({
    "LOCAL_YTDLP_OTHER_PROCESS_ACTIVE",
    "LOCAL_YTDLP_PACING_WAIT_TOO_LONG",
    "LOCAL_YTDLP_COORDINATOR_ACCESS_DEFERRED",
})
_MAX_ATTEMPTS = 3
_DEFER_SECONDS = 1800
_SAFE_REASON = frozenset({
    "NONE", "LOCAL_RENDER_NO_PROGRESS",
    "LOCAL_RENDER_INCOMPLETE_ACQUISITION",
    "LOCAL_RENDER_SOURCE_ACQUISITION_FAILED",
    "LOCAL_RENDER_QUEUED_WORKER_INTERRUPTED",
    "LOCAL_RENDER_MANUALLY_RESUMED",
    "LOCAL_RENDER_SOURCE_POLICY_BLOCKED",
    "LOCAL_RENDER_LOCAL_CACHE_UNTRUSTED",
    "LOCAL_RENDER_PACING_DEFERRED",
    "LOCAL_RENDER_REMOTE_ACCESS_PAUSED",
})


def _queue_path(path: Path) -> Path:
    value = _no_links(path)
    if value.suffix.lower() not in {".sqlite3", ".sqlite", ".db"}:
        raise ValueError("LOCAL_RENDER_QUEUE_UNSAFE_PATH")
    return value


def _connect(path: Path, *, create: bool) -> sqlite3.Connection | None:
    path = _queue_path(path)
    if not path.is_file():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    _no_links(path)
    # No concurrent schema mutation or silent network: SQLite is purely local.
    mode = "rwc" if create else "ro"
    try:
        conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        if create:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS queue_meta "
                "(version TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS queue_jobs ("
                "job_id TEXT PRIMARY KEY, timeline_json TEXT NOT NULL, "
                "database_path TEXT NOT NULL, cache_root TEXT NOT NULL, "
                "policy_file TEXT NOT NULL, max_duration INTEGER NOT NULL, "
                "max_filesize INTEGER NOT NULL, state TEXT NOT NULL, "
                "attempts INTEGER NOT NULL, due_at REAL NOT NULL, "
                "token TEXT NOT NULL, reason TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            row = conn.execute("SELECT version FROM queue_meta").fetchall()
            if not row:
                conn.execute(
                    "INSERT INTO queue_meta(version) VALUES (?)",
                    (_QUEUE_VERSION,),
                )
            elif len(row) != 1 or row[0][0] != _QUEUE_VERSION:
                raise ValueError("LOCAL_RENDER_QUEUE_VERSION_UNTRUSTED")
            conn.commit()
        else:
            row = conn.execute("SELECT version FROM queue_meta").fetchall()
            if len(row) != 1 or row[0][0] != _QUEUE_VERSION:
                raise ValueError("LOCAL_RENDER_QUEUE_VERSION_UNTRUSTED")
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise ValueError("LOCAL_RENDER_QUEUE_STORAGE_UNAVAILABLE") from exc


def _validated_timeline(timeline: ClipTimeline) -> ClipTimeline:
    document = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    if len(document.model_dump_json()) > 1_048_576:
        raise ValueError("LOCAL_RENDER_QUEUE_TIMELINE_TOO_LARGE")
    return document


def _queue_identity(
    timeline: ClipTimeline, *, database: Path, cache_root: Path,
    policy_file: Path, max_duration_seconds: int, max_filesize_mb: int,
) -> str:
    return stable_id(
        "renderqueue", _QUEUE_VERSION, timeline.model_dump(mode="json"),
        str(database), str(cache_root), str(policy_file),
        max_duration_seconds, max_filesize_mb, length=32,
    )


def enqueue_render_timeline(
    timeline: ClipTimeline, *, queue: Path = DEFAULT_QUEUE,
    database: Path = _DEFAULT_DB, cache_root: Path = _DEFAULT_CACHE,
    policy_file: Path = _DEFAULT_POLICY,
    max_duration_seconds: int = MAX_DURATION_SECONDS,
    max_filesize_mb: int = MAX_FILESIZE_MB,
    apply: bool = False, approve_local_preview: bool = False,
) -> dict:
    """Preview first; explicit apply+local-only permission stores job snapshot."""
    document = _validated_timeline(timeline)
    queue = _queue_path(queue)
    database, cache_root, policy_file = (
        _no_links(database), _no_links(cache_root), _no_links(policy_file)
    )
    missing, counts = _inventory(
        timeline=document, database=database, cache_root=cache_root,
        max_duration_seconds=max_duration_seconds,
        max_filesize_mb=max_filesize_mb,
    )
    jid = _queue_identity(
        document, database=database, cache_root=cache_root,
        policy_file=policy_file, max_duration_seconds=max_duration_seconds,
        max_filesize_mb=max_filesize_mb,
    )
    report = {
        "status": "LOCAL_RENDER_QUEUE_PREVIEW",
        "job_id": jid, "missing_count": counts["missing"],
        "cached_count": counts["cached"],
        "missing_source_asset_ids": [aid for aid, _ in missing],
        "queue_written": False, "youtube_contacted": False,
        "ready": False, "production_ready": False,
    }
    if not apply:
        return report
    if approve_local_preview is not True:
        raise ValueError("LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    conn = _connect(queue, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state FROM queue_jobs WHERE job_id=?", (jid,),
        ).fetchone()
        if row is None and conn.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] >= 1000:
            raise ValueError("LOCAL_RENDER_QUEUE_TOO_LARGE")
        if row is not None:
            report["status"] = "LOCAL_RENDER_QUEUE_ALREADY_ENROLLED"
            return report
        now = time.time()
        conn.execute(
            "INSERT INTO queue_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid, document.model_dump_json(), str(database), str(cache_root),
                str(policy_file), max_duration_seconds, max_filesize_mb,
                "QUEUED" if missing else "COMPLETE", 0, 0.0, "",
                "NONE", now, now,
            ),
        )
        conn.commit()
        report["status"] = (
            "LOCAL_RENDER_QUEUE_ENROLLED" if missing
            else "LOCAL_RENDER_QUEUE_ALREADY_CACHED"
        )
        report["queue_written"] = True
        return report
    finally:
        conn.close()


def inspect_render_queue(*, queue: Path = DEFAULT_QUEUE) -> dict:
    """No database creation and no network. State codes only, not local paths."""
    result = {
        "status": "LOCAL_RENDER_QUEUE_NOT_CREATED",
        "counts": {key: 0 for key in sorted(_STATUS)},
        "total": 0, "next_job_id": None,
        "next_state": None,
        "no_automatic_retry_after_uncertainty": True,
        "ready": False, "production_ready": False,
    }
    conn = _connect(queue, create=False)
    if conn is None:
        return result
    try:
        rows = conn.execute(
            "SELECT job_id,state,attempts,due_at,reason FROM queue_jobs "
            "ORDER BY created_at, job_id LIMIT 1001"
        ).fetchall()
        if len(rows) > 1000:
            raise ValueError("LOCAL_RENDER_QUEUE_TOO_LARGE")
        for row in rows:
            if row["state"] not in _STATUS or row["reason"] not in _SAFE_REASON:
                raise ValueError("LOCAL_RENDER_QUEUE_STATE_UNTRUSTED")
            if not 0 <= row["attempts"] <= _MAX_ATTEMPTS:
                raise ValueError("LOCAL_RENDER_QUEUE_STATE_UNTRUSTED")
            result["counts"][row["state"]] += 1
        result["total"] = len(rows)
        pending = next((r for r in rows if r["state"] != "COMPLETE"), None)
        if pending is None:
            result["status"] = "LOCAL_RENDER_QUEUE_ALL_COMPLETE"
        else:
            result["status"] = "LOCAL_RENDER_QUEUE_NEEDS_ATTENTION" if pending["state"] in {
                "PAUSED", "RUNNING",
            } else "LOCAL_RENDER_QUEUE_HAS_PENDING"
            result["next_job_id"] = pending["job_id"]
            result["next_state"] = pending["state"]
        return result
    finally:
        conn.close()


def _claim(queue: Path) -> tuple[dict | None, str, str]:
    """Oldest incomplete job serializes all workers and hostwide deferrals."""
    conn = _connect(queue, create=False)
    if conn is None:
        return None, "", "LOCAL_RENDER_QUEUE_NOT_CREATED"
    try:
        # Read-only connection cannot BEGIN IMMEDIATE. Reopen writable
        # explicitly but NEVER create a missing queue.
        conn.close()
        path = _queue_path(queue)
        conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM queue_jobs WHERE state != 'COMPLETE' "
            "ORDER BY created_at, job_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None, "", "LOCAL_RENDER_QUEUE_ALL_COMPLETE"
        if row["state"] not in _STATUS or row["reason"] not in _SAFE_REASON:
            raise ValueError("LOCAL_RENDER_QUEUE_STATE_UNTRUSTED")
        if row["state"] in {"RUNNING", "PAUSED"}:
            return None, "", "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED"
        if row["state"] == "DEFERRED" and row["due_at"] > time.time():
            return None, "", "LOCAL_RENDER_QUEUE_DEFERRED_BY_PACING"
        if row["attempts"] >= _MAX_ATTEMPTS:
            conn.execute(
                "UPDATE queue_jobs SET state='PAUSED',reason=?,updated_at=? "
                "WHERE job_id=?", ("LOCAL_RENDER_REMOTE_ACCESS_PAUSED", time.time(),
                                  row["job_id"]),
            )
            conn.commit()
            return None, "", "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED"
        token = secrets.token_hex(16)
        conn.execute(
            "UPDATE queue_jobs SET state='RUNNING',token=?,attempts=attempts+1,"
            "updated_at=? WHERE job_id=? AND state IN ('QUEUED','DEFERRED')",
            (token, time.time(), row["job_id"]),
        )
        conn.commit()
        return dict(row), token, "LOCAL_RENDER_QUEUE_JOB_CLAIMED"
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_RENDER_QUEUE_STORAGE_UNAVAILABLE") from exc
    finally:
        conn.close()


def _finish(queue: Path, jid: str, token: str, state: str, reason: str,
            *, due_at: float = 0.0) -> None:
    if state not in _STATUS or reason not in _SAFE_REASON:
        raise ValueError("LOCAL_RENDER_QUEUE_STATE_UNTRUSTED")
    path = _queue_path(queue)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE queue_jobs SET state=?,token='',reason=?,due_at=?,"
                "updated_at=? WHERE job_id=? AND state='RUNNING' AND token=?",
                (state, reason, due_at, time.time(), jid, token),
            )
            if changed.rowcount != 1:
                raise ValueError("LOCAL_RENDER_QUEUE_CLAIM_LOST")
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_RENDER_QUEUE_STORAGE_UNAVAILABLE") from exc


def advance_render_queue(
    *, queue: Path = DEFAULT_QUEUE, execute: bool = False,
) -> dict:
    """At most one network source attempt. Never automatically heal a crash."""
    base = {
        "status": "LOCAL_RENDER_QUEUE_PREVIEW_ONLY",
        "job_id": None, "acquired_count": 0,
        "youtube_contact_attempted": False,
        "max_acquisitions_this_run": 1,
        "ready": False, "production_ready": False,
    }
    if not execute:
        base["status"] = inspect_render_queue(queue=queue)["status"]
        return base
    row, token, status = _claim(queue)
    if row is None:
        base["status"] = status
        return base
    jid = row["job_id"]
    base["job_id"] = jid
    try:
        document = ClipTimeline.model_validate_json(row["timeline_json"])
        inputs = {
            "database": Path(row["database_path"]),
            "cache_root": Path(row["cache_root"]),
            "policy_file": Path(row["policy_file"]),
            "max_duration_seconds": row["max_duration"],
            "max_filesize_mb": row["max_filesize"],
        }
        base["youtube_contact_attempted"] = True
        acquired = acquire_one_missing_render_source(
            document, **inputs, execute=True, approve_local_preview=True,
        )
        if acquired["status"] == "LOCAL_RENDER_ALL_FULL_SOURCES_PRESENT":
            _finish(queue, jid, token, "COMPLETE", "NONE")
            base["status"] = "LOCAL_RENDER_QUEUE_JOB_COMPLETE"
        elif acquired["status"] == "LOCAL_RENDER_ONE_FULL_VIDEO_ACQUIRED_NOT_PRODUCTION_READY":
            base["acquired_count"] = 1
            new_missing, _ = _inventory(timeline=document, **{
                k: inputs[k] for k in ("database", "cache_root",
                                      "max_duration_seconds", "max_filesize_mb")
            })
            _finish(queue, jid, token, "QUEUED" if new_missing else "COMPLETE", "NONE")
            base["status"] = (
                "LOCAL_RENDER_QUEUE_ONE_SOURCE_ACQUIRED" if new_missing
                else "LOCAL_RENDER_QUEUE_JOB_COMPLETE"
            )
        else:
            _finish(queue, jid, token, "PAUSED", "LOCAL_RENDER_NO_PROGRESS")
            base["status"] = "LOCAL_RENDER_QUEUE_PAUSED_UNKNOWN_RESULT"
    except (YtDlpError, YtDlpTimeout) as exc:
        # Only exact codes that mean a *different process* currently owns
        # the reservation may be retried. Any captcha/rate restriction, SSH
        # failure, downloader failure or unknown stderr is manual pause.
        if str(exc) in _DEFER_ERRORS:
            _finish(
                queue, jid, token, "DEFERRED", "LOCAL_RENDER_PACING_DEFERRED",
                due_at=time.time() + _DEFER_SECONDS,
            )
            base["status"] = "LOCAL_RENDER_QUEUE_PACING_DEFERRED"
        else:
            _finish(queue, jid, token, "PAUSED", "LOCAL_RENDER_REMOTE_ACCESS_PAUSED")
            base["status"] = "LOCAL_RENDER_QUEUE_PAUSED_REMOTE_OR_UNCERTAIN"
    except (ValueError, OSError, TypeError, UnicodeError):
        _finish(queue, jid, token, "PAUSED", "LOCAL_RENDER_LOCAL_CACHE_UNTRUSTED")
        base["status"] = "LOCAL_RENDER_QUEUE_PAUSED_VALIDATION"
    return base


def _recovery_cache_verified(document: ClipTimeline, database: Path,
                             cache_root: Path) -> bool:
    """Real A/V probe and duration-vs-original check, never just file exists.

    Missing original duration evidence remains PAUSED; a truncated video may
    still possess a valid yt-dlp receipt and a nonzero byte count.
    """
    try:
        required = {s.source_asset_id for s in document.segments}
        assets = _canonical_assets(database, required)
        for aid in required:
            asset = assets[aid]
            media = _find_complete_cache(cache_root, asset.external_id)
            receipt = media.parent / "media.info.json"
            if not 1 <= receipt.stat().st_size <= 1024 * 1024:
                return False
            info = json.loads(receipt.read_text(encoding="utf-8"))
            original_duration = asset.duration if asset.duration is not None else info.get("duration")
            if type(original_duration) not in (int, float):
                return False
            original = float(original_duration)
            if not math.isfinite(original) or original <= 0:
                return False
            measured, video, audio = _probe(media)
            latest_cut = max(
                s.source_end for s in document.segments if s.source_asset_id == aid
            )
            if (not video or not audio or measured < latest_cut
                    or abs(measured - original) > max(2.0, original * 0.01)):
                return False
        return True
    except (ValueError, KeyError, TypeError, OSError, UnicodeError):
        return False


def reconcile_render_queue(
    *, queue: Path = DEFAULT_QUEUE, job_id: str, apply: bool = False,
    resume: bool = False,
) -> dict:
    """Explicit offline recovery; RUNNING only becomes PAUSED or COMPLETE.

    Never silently re-download an uncertain, partially written archive.
    A PAUSED job requires separate manual resume, never auto-resume.
    """
    path = _queue_path(queue)
    conn = _connect(path, create=False)
    if conn is None:
        raise ValueError("LOCAL_RENDER_QUEUE_NOT_CREATED")
    try:
        row = conn.execute(
            "SELECT * FROM queue_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("LOCAL_RENDER_QUEUE_JOB_UNKNOWN")
        if row["state"] not in {"RUNNING", "PAUSED", "DEFERRED"}:
            raise ValueError("LOCAL_RENDER_QUEUE_RECOVERY_NOT_REQUIRED")
        if (row["state"] == "RUNNING"
                and time.time() - row["updated_at"] < 7200):
            # A timed-out inspection is not proof the old process is gone.
            # Wait past the acquisition/native-child deadline before allowing
            # human-triggered recovery of an uncertain in-flight transfer.
            raise ValueError("LOCAL_RENDER_QUEUE_RUN_MAY_STILL_BE_ACTIVE")
        doc = ClipTimeline.model_validate_json(row["timeline_json"])
        try:
            missing, _ = _inventory(
                timeline=doc, database=Path(row["database_path"]),
                cache_root=Path(row["cache_root"]),
                max_duration_seconds=row["max_duration"],
                max_filesize_mb=row["max_filesize"],
            )
        except ValueError:
            # Corrupt or partial media should not trap a crashed RUNNING job
            # forever. Explicit recovery changes it to PAUSED, not QUEUED.
            missing = [("UNVERIFIED", "UNVERIFIED")]
        complete = not missing and _recovery_cache_verified(
            doc, Path(row["database_path"]), Path(row["cache_root"]),
        )
        target = "COMPLETE" if complete else ("QUEUED" if resume
            and row["state"] == "PAUSED" else "PAUSED")
        result = {
            "status": "LOCAL_RENDER_QUEUE_RECOVERY_PREVIEW" if not apply
            else "LOCAL_RENDER_QUEUE_RECOVERY_APPLIED",
            "job_id": job_id,
            "would_be_state": target,
            "media_fully_cached": complete,
            "changes_applied": False,
            "ready": False, "production_ready": False,
        }
        if not apply:
            return result
        # Resuming a PAUSED job requires explicit --resume, and only after
        # verifying the operator has addressed the failure. We never resume
        # RUNNING/DEFERRED directly, avoiding stale worker double requests.
        if resume and row["state"] != "PAUSED":
            raise ValueError("LOCAL_RENDER_QUEUE_UNSAFE_RESUME")
    finally:
        conn.close()
    with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT state,token FROM queue_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if current is None or current[0] != row["state"] or current[1] != row["token"]:
            raise ValueError("LOCAL_RENDER_QUEUE_STATE_CHANGED")
        db.execute(
            "UPDATE queue_jobs SET state=?,attempts=?,token='',due_at=0,"
            "reason=?,updated_at=? WHERE job_id=?",
            (target, 0 if target == "QUEUED" else row["attempts"],
             "LOCAL_RENDER_MANUALLY_RESUMED" if target == "QUEUED"
             else "LOCAL_RENDER_QUEUED_WORKER_INTERRUPTED" if not complete else "NONE",
             time.time(), job_id),
        )
    result["changes_applied"] = True
    return result
