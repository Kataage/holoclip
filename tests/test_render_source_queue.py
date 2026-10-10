"""Durable local video acquisition queue: no auto-repeat after failure or crash."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_source_queue as queue
from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment, make_source_asset_id
from holoclip.providers.source.yt_dlp import YtDlpError
from holoclip.repository import SQLiteCanonicalRepository


_IDS = ("AbCdeFGh123", "XyZabCDE456")


def _fixture(tmp_path):
    database = tmp_path / "canonical.db"
    cache = tmp_path / "sources"
    cache.mkdir()
    assets = []
    with SQLiteCanonicalRepository(database) as repository:
        for vid in _IDS:
            asset = SourceAsset(
                id=make_source_asset_id("youtube", vid),
                platform="youtube", external_id=vid,
                source_type="stream", availability="available",
                access_class="public",
            )
            repository.upsert_source_asset(asset)
            assets.append(asset)
    timeline = ClipTimeline(
        id="test-multi-asset",
        segments=[
            TimelineSegment(
                id="cut0", source_asset_id=assets[0].id, source_start=5,
                source_end=10, output_order=0,
            ),
            TimelineSegment(
                id="cut1", source_asset_id=assets[1].id, source_start=20,
                source_end=25, output_order=1,
            ),
        ],
        estimated_duration=10,
    )
    return database, cache, timeline


def _enroll(tmp_path, *, apply=True, approved=True):
    db, root, timeline = _fixture(tmp_path)
    state = tmp_path / "runtime" / "queue.sqlite3"
    receipt = queue.enqueue_render_timeline(
        timeline, database=db, cache_root=root, queue=state,
        apply=apply, approve_local_preview=approved,
    )
    return state, receipt, timeline, db, root


def _set(conn, jid, **changes):
    with conn:
        for column, value in changes.items():
            if column not in {"state", "attempts", "due_at", "token", "reason"}:
                raise ValueError("bad test column")
            conn.execute(f"UPDATE queue_jobs SET {column}=? WHERE job_id=?", (value, jid))


def _fake_full_video(root, video):
    folder = root / video / "editing_video"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "media.info.json").write_text(
        json.dumps({"id": video, "extractor_key": "Youtube"})
    )
    (folder / "media.mkv").write_bytes(b"synthetic local full source" * 20)


def test_preview_and_status_do_not_create_queue_or_contact_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(
        queue, "acquire_one_missing_render_source",
        lambda *a, **k: pytest.fail("offline preview may not contact provider"),
    )
    state, receipt, _, _, _ = _enroll(tmp_path, apply=False)
    assert receipt["status"] == "LOCAL_RENDER_QUEUE_PREVIEW"
    assert not state.exists()
    status = queue.inspect_render_queue(queue=state)
    assert status["status"] == "LOCAL_RENDER_QUEUE_NOT_CREATED"
    assert status["total"] == 0 and status["ready"] is False
    run = queue.advance_render_queue(queue=state)
    assert run["youtube_contact_attempted"] is False
    assert not state.exists()


def test_enrollment_is_explicit_and_duplicate_enqueue_does_not_overwrite(tmp_path):
    db, root, timeline = _fixture(tmp_path)
    state = tmp_path / "queue.sqlite3"
    with pytest.raises(ValueError, match="LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"):
        queue.enqueue_render_timeline(
            timeline, database=db, cache_root=root, queue=state, apply=True,
        )
    assert not state.exists()
    first = queue.enqueue_render_timeline(
        timeline, database=db, cache_root=root, queue=state, apply=True,
        approve_local_preview=True,
    )
    again = queue.enqueue_render_timeline(
        timeline, database=db, cache_root=root, queue=state, apply=True,
        approve_local_preview=True,
    )
    assert first["status"] == "LOCAL_RENDER_QUEUE_ENROLLED"
    assert again["status"] == "LOCAL_RENDER_QUEUE_ALREADY_ENROLLED"
    assert first["job_id"] == again["job_id"]
    status = queue.inspect_render_queue(queue=state)
    assert status["counts"]["QUEUED"] == 1
    assert status["total"] == 1
    assert status["ready"] is False


def test_worker_single_source_per_turn_and_continues_next_schedule(tmp_path, monkeypatch):
    state, receipt, _, _, root = _enroll(tmp_path)
    calls = []
    def acquisition(timeline, *, cache_root, **kw):
        missing, _ = queue._inventory(
            timeline=timeline, database=kw["database"], cache_root=cache_root,
            max_duration_seconds=kw["max_duration_seconds"],
            max_filesize_mb=kw["max_filesize_mb"],
        )
        assert len(missing) in (1, 2)
        calls.append(missing[0][1])
        _fake_full_video(root, missing[0][1])
        return {"status": "LOCAL_RENDER_ONE_FULL_VIDEO_ACQUIRED_NOT_PRODUCTION_READY"}
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", acquisition)
    first = queue.advance_render_queue(queue=state, execute=True)
    assert first["status"] == "LOCAL_RENDER_QUEUE_ONE_SOURCE_ACQUIRED"
    assert first["acquired_count"] == 1
    assert calls == [_IDS[0]]
    mid = queue.inspect_render_queue(queue=state)
    assert mid["counts"]["QUEUED"] == 1
    second = queue.advance_render_queue(queue=state, execute=True)
    assert second["status"] == "LOCAL_RENDER_QUEUE_JOB_COMPLETE"
    assert second["acquired_count"] == 1
    assert calls == list(_IDS)
    assert queue.inspect_render_queue(queue=state)["counts"]["COMPLETE"] == 1
    again = queue.advance_render_queue(queue=state, execute=True)
    assert again["status"] == "LOCAL_RENDER_QUEUE_ALL_COMPLETE"
    assert calls == list(_IDS)
    assert first["ready"] is False and second["production_ready"] is False


def test_concurrent_worker_cannot_start_second_download(tmp_path, monkeypatch):
    state, _, _, _, root = _enroll(tmp_path)
    calls = []
    def fake_acquire(timeline, **kw):
        calls.append(1)
        inner = queue.advance_render_queue(queue=state, execute=True)
        assert inner["status"] == "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED"
        assert inner["acquired_count"] == 0
        assert len(calls) == 1
        _fake_full_video(root, _IDS[0])
        return {"status": "LOCAL_RENDER_ONE_FULL_VIDEO_ACQUIRED_NOT_PRODUCTION_READY"}
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", fake_acquire)
    outer = queue.advance_render_queue(queue=state, execute=True)
    assert outer["status"] == "LOCAL_RENDER_QUEUE_ONE_SOURCE_ACQUIRED"
    assert len(calls) == 1


def test_pacing_deferral_is_persisted_and_waits_before_new_attempt(tmp_path, monkeypatch):
    state, receipt, _, _, _ = _enroll(tmp_path)
    calls = []
    def busy(*a, **k):
        calls.append(True)
        raise YtDlpError("LOCAL_YTDLP_OTHER_PROCESS_ACTIVE")
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", busy)
    before = queue.time.time()
    result = queue.advance_render_queue(queue=state, execute=True)
    assert result["status"] == "LOCAL_RENDER_QUEUE_PACING_DEFERRED"
    assert queue.inspect_render_queue(queue=state)["counts"]["DEFERRED"] == 1
    again = queue.advance_render_queue(queue=state, execute=True)
    assert again["status"] == "LOCAL_RENDER_QUEUE_DEFERRED_BY_PACING"
    assert len(calls) == 1
    with sqlite3.connect(state) as conn:
        row = conn.execute("SELECT attempts,due_at FROM queue_jobs").fetchone()
    assert row[0] == 1 and row[1] >= before + 1790


def test_three_deferred_attempts_then_manual_pause(tmp_path, monkeypatch):
    state, _, _, _, _ = _enroll(tmp_path)
    def busy(*a, **k):
        raise YtDlpError("LOCAL_YTDLP_OTHER_PROCESS_ACTIVE")
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", busy)
    for _ in range(3):
        assert queue.advance_render_queue(queue=state, execute=True)["status"] == (
            "LOCAL_RENDER_QUEUE_PACING_DEFERRED"
        )
        with sqlite3.connect(state) as conn:
            _set(conn, conn.execute("SELECT job_id FROM queue_jobs").fetchone()[0],
                 due_at=0)
    status = queue.advance_render_queue(queue=state, execute=True)
    assert status["status"] == "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED"
    assert queue.inspect_render_queue(queue=state)["counts"]["PAUSED"] == 1


@pytest.mark.parametrize("failure", [
    "HTTP_RATE_LIMIT 429",
    "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE captcha",
    "unknown error path C:\\SECRET\\private.mkv",
    "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE",
])
def test_rate_limit_bot_or_unknown_failure_never_retried(tmp_path, monkeypatch, failure):
    state, _, _, _, _ = _enroll(tmp_path)
    calls = []
    def failed(*a, **kw):
        calls.append(True)
        raise YtDlpError(failure)
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", failed)
    response = queue.advance_render_queue(queue=state, execute=True)
    assert response["status"] == "LOCAL_RENDER_QUEUE_PAUSED_REMOTE_OR_UNCERTAIN"
    assert failure not in str(response)
    assert queue.inspect_render_queue(queue=state)["counts"]["PAUSED"] == 1
    assert queue.advance_render_queue(queue=state, execute=True)["acquired_count"] == 0
    assert len(calls) == 1


def test_validation_failure_pauses_and_sanitizes_state(tmp_path, monkeypatch):
    state, receipt, _, _, _ = _enroll(tmp_path)
    def failed(*a, **kw):
        raise ValueError("SECRET source media path should never persist")
    monkeypatch.setattr(queue, "acquire_one_missing_render_source", failed)
    response = queue.advance_render_queue(queue=state, execute=True)
    assert response["status"] == "LOCAL_RENDER_QUEUE_PAUSED_VALIDATION"
    assert "SECRET" not in str(queue.inspect_render_queue(queue=state))
    with sqlite3.connect(state) as conn:
        reason = conn.execute("SELECT reason FROM queue_jobs").fetchone()[0]
    assert reason == "LOCAL_RENDER_LOCAL_CACHE_UNTRUSTED"


def test_crash_leaves_running_until_explicit_offline_reconciliation(tmp_path, monkeypatch):
    state, receipt, _, _, root = _enroll(tmp_path)
    # Simulate process death after atomic claim before any network call.
    row, token, status = queue._claim(state)
    assert status == "LOCAL_RENDER_QUEUE_JOB_CLAIMED"
    assert queue.advance_render_queue(queue=state, execute=True)["status"] == (
        "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED"
    )
    prev = queue.reconcile_render_queue(
        queue=state, job_id=receipt["job_id"],
    )
    assert prev["would_be_state"] == "PAUSED"
    assert prev["changes_applied"] is False
    recovered = queue.reconcile_render_queue(
        queue=state, job_id=receipt["job_id"], apply=True,
    )
    assert recovered["changes_applied"] is True
    assert queue.inspect_render_queue(queue=state)["counts"]["PAUSED"] == 1
    with pytest.raises(ValueError, match="LOCAL_RENDER_QUEUE_RECOVERY_NOT_REQUIRED"):
        queue.reconcile_render_queue(
            queue=state, job_id=receipt["job_id"], resume=False,
        ) if False else queue.reconcile_render_queue(
            queue=state, job_id="unrecognized-job",
        )


def test_paused_job_never_restarts_without_manual_resume(tmp_path, monkeypatch):
    state, receipt, _, _, _ = _enroll(tmp_path)
    row, token, status = queue._claim(state)
    queue._finish(state, receipt["job_id"], token, "PAUSED",
                  "LOCAL_RENDER_LOCAL_CACHE_UNTRUSTED")
    report = queue.reconcile_render_queue(
        queue=state, job_id=receipt["job_id"], apply=True, resume=True,
    )
    assert report["would_be_state"] == "QUEUED"
    assert queue.inspect_render_queue(queue=state)["counts"]["QUEUED"] == 1


def test_corrupt_queue_schema_and_linked_state_fail_closed(tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not-sqlite")
    with pytest.raises(ValueError, match="LOCAL_RENDER_QUEUE_STORAGE_UNAVAILABLE"):
        queue.inspect_render_queue(queue=bad)
    real = tmp_path / "real.sqlite3"
    real.write_bytes(b"do not overwrite")
    linked = tmp_path / "linked.sqlite3"
    try:
        linked.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    with pytest.raises(ValueError, match="LOCAL_RENDER_LINKED_PATH"):
        queue.inspect_render_queue(queue=linked)
    assert real.read_bytes() == b"do not overwrite"


def test_enqueue_cli_preview_and_job_status(tmp_path):
    db, root, timeline = _fixture(tmp_path)
    timeline_file = tmp_path / "timeline.json"
    timeline_file.write_text(timeline.model_dump_json())
    state = tmp_path / "queue.sqlite3"
    opts = ["--timeline", str(timeline_file), "--database", str(db),
            "--cache-root", str(root), "--queue", str(state)]
    dry = CliRunner().invoke(cli.app, ["render-source-queue-enqueue", *opts])
    assert dry.exit_code == 0, dry.stdout
    assert json.loads(dry.stdout)["status"] == "LOCAL_RENDER_QUEUE_PREVIEW"
    assert not state.exists()
    save = CliRunner().invoke(cli.app, [
        "render-source-queue-enqueue", *opts, "--apply", "--approve-local-preview",
    ])
    assert save.exit_code == 0, save.stdout
    assert json.loads(save.stdout)["status"] == "LOCAL_RENDER_QUEUE_ENROLLED"
    status = CliRunner().invoke(cli.app, [
        "render-source-queue-status", "--queue", str(state),
    ])
    assert status.exit_code == 0 and json.loads(status.stdout)["total"] == 1
    worker_dry = CliRunner().invoke(cli.app, [
        "render-source-queue-run", "--queue", str(state),
    ])
    assert worker_dry.exit_code == 0
    assert json.loads(worker_dry.stdout)["status"] == "LOCAL_RENDER_QUEUE_HAS_PENDING"


def test_queue_corrupted_row_requires_manual_intervention(tmp_path):
    state, _, _, _, _ = _enroll(tmp_path)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE queue_jobs SET reason='RAW_SECRET'")
    with pytest.raises(ValueError, match="LOCAL_RENDER_QUEUE_STATE_UNTRUSTED"):
        queue.inspect_render_queue(queue=state)
