"""Completed source acquisition -> local-only preview render with durable technical QA."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_plan
from holoclip import render_ready_queue as ready
from holoclip import render_source_queue as source_queue
from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment, make_source_asset_id
from holoclip.repository import SQLiteCanonicalRepository


VIDEO = "abcDEFgh012"


def _case(tmp_path, *, cached=True, duration=100.0):
    db = tmp_path / "holoclip.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    queue = tmp_path / "runtime" / "renderqueue.sqlite3"
    root = tmp_path / "rendered"
    asset = SourceAsset(
        id=make_source_asset_id("youtube", VIDEO),
        platform="youtube", external_id=VIDEO, source_type="stream",
        availability="available", access_class="public",
        duration=duration,
    )
    with SQLiteCanonicalRepository(db) as repository:
        repository.upsert_source_asset(asset)
    timeline = ClipTimeline(
        id="test-clip",
        segments=[
            TimelineSegment(
                id="a", source_asset_id=asset.id, source_start=10,
                source_end=13, output_order=0,
            ),
            TimelineSegment(
                id="b", source_asset_id=asset.id, source_start=20,
                source_end=23, output_order=1,
            ),
        ], estimated_duration=6,
    )
    if cached:
        _add_cache(cache)
    enrollment = source_queue.enqueue_render_timeline(
        timeline, queue=queue, database=db, cache_root=cache,
        approve_local_preview=True, apply=True,
    )
    return db, cache, queue, root, timeline, enrollment


def _add_cache(root, *, description="", original_duration=100.0,
               title="Archived public stream"):
    folder = root / VIDEO / "editing_video"
    folder.mkdir(parents=True, exist_ok=True)
    receipt = {
        "id": VIDEO, "extractor_key": "Youtube",
        "title": title, "description": description,
        "duration": original_duration, "live_status": "not_live",
        "availability": "public",
    }
    (folder / "media.info.json").write_text(json.dumps(receipt), encoding="utf-8")
    (folder / "media.mkv").write_bytes(b"offline synthetic A/V video bytes" * 30)


def _probe(monkeypatch, *, measured_duration=100.0):
    monkeypatch.setattr(
        source_queue, "_probe",
        lambda path: (measured_duration, True, True),
    )
    monkeypatch.setattr(
        render_plan, "_probe",
        lambda path: (measured_duration, True, True),
    )


def _fake_render(monkeypatch, *, count=None, status=None):
    def render(plan):
        if count is not None:
            count.append(plan.id)
        plan.output.write_bytes(b"offline QA-verified MP4 placeholder")
        return {
            "status": status or "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
            "output_sha256": "a" * 64,
            "measured_duration": plan.expected_duration,
        }
    monkeypatch.setattr(ready, "render_local_preview", render)


def test_offline_status_no_table_creation_or_outputs(tmp_path):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    result = ready.inspect_completed_source_renders(queue=queue)
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_STATUS"
    assert result["counts"]["PENDING"] == 1
    assert result["counts"]["QA_PASSED"] == 0
    assert result["ready"] is False
    with sqlite3.connect(queue) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='render_jobs'"
        ).fetchone() is None
    assert not root.exists()


def test_missing_queue_preview_does_not_create_db(tmp_path):
    queue = tmp_path / "no.sqlite3"
    result = ready.advance_completed_source_renders(queue=queue)
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_QUEUE_NOT_CREATED"
    assert not queue.exists()


def test_source_job_not_finished_never_renders(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path, cached=False)
    _probe(monkeypatch)
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("must not render while original video is missing"),
    )
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_NO_READY_JOBS"
    assert not root.exists()


def test_successful_local_two_cut_render_persists_basic_qa_and_idempotence(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    calls = []
    _fake_render(monkeypatch, count=calls)
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert result["rendered_count"] == 1
    assert result["publication_performed"] is False
    assert result["ready"] is False and result["human_editorial_qa_completed"] is False
    output = root / f"{receipt['job_id']}.mp4"
    assert output.is_file()
    with sqlite3.connect(queue) as connection:
        row = connection.execute(
            "SELECT state,output_path,output_sha256,render_plan_id,"
            "measured_duration FROM render_jobs"
        ).fetchone()
    assert row[0] == "QA_PASSED"
    assert Path(row[1]) == output
    assert row[2] == "a" * 64 and row[3] == calls[0]
    assert row[4] == pytest.approx(6)
    next_ = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert next_["status"] == "LOCAL_RENDER_AUTO_QA_NO_READY_JOBS"
    assert len(calls) == 1
    assert ready.inspect_completed_source_renders(queue=queue)["counts"]["QA_PASSED"] == 1


def test_concurrent_render_worker_does_not_claim_twice(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    calls = []
    def nested(plan):
        calls.append(plan.id)
        inner = ready.advance_completed_source_renders(
            queue=queue, preview_root=root, execute=True,
        )
        assert inner["status"] == "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS"
        assert inner["rendered_count"] == 0
        plan.output.write_bytes(b"fake output")
        return {
            "status": "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
            "output_sha256": "b" * 64,
            "measured_duration": 6,
        }
    monkeypatch.setattr(ready, "render_local_preview", nested)
    outer = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert outer["rendered_count"] == 1 and len(calls) == 1


def test_missing_or_partial_original_never_reaches_ffmpeg(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch, measured_duration=15)
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("partial cache must fail before FFmpeg"),
    )
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert not root.exists()
    assert ready.inspect_completed_source_renders(queue=queue)["counts"]["PAUSED"] == 1
    assert ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )["status"] == "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS"


@pytest.mark.parametrize("content", ["切り抜き禁止", "do not clip this stream"])
def test_cached_no_clipping_description_blocks_render(tmp_path, monkeypatch, content):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    info = cache / VIDEO / "editing_video" / "media.info.json"
    payload = json.loads(info.read_text())
    payload["description"] = content
    info.write_text(json.dumps(payload))
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("no clipping source must not be rendered"),
    )
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert not root.exists()


def test_cached_receipt_missing_description_fails_closed(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    info = cache / VIDEO / "editing_video" / "media.info.json"
    payload = json.loads(info.read_text())
    payload.pop("description")
    info.write_text(json.dumps(payload))
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert not root.exists()


def test_policy_range_blocked_by_registry_before_ffmpeg(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    with sqlite3.connect(queue) as connection:
        policy = tmp_path / "policy.jsonl"
        policy.write_text(json.dumps({
            "id": "block-range", "decision": "block",
            "reason": "restricted scene", "operations": ["render"],
            "source_id": VIDEO, "start": 10, "end": 15,
        }) + "\n")
        connection.execute(
            "UPDATE queue_jobs SET policy_file=?",
            (str(policy),),
        )
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("blocked range"),
    )
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert not root.exists()


def test_render_failure_stays_paused_without_retries(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    calls = []
    def failed(plan):
        calls.append(True)
        raise ValueError("PRIVATE_FFMPEG_RAW_ERROR")
    monkeypatch.setattr(ready, "render_local_preview", failed)
    outcome = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert outcome["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert "PRIVATE" not in str(outcome)
    again = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert again["status"] == "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS"
    assert calls == [True]
    with sqlite3.connect(queue) as connection:
        reason = connection.execute("SELECT reason FROM render_jobs").fetchone()[0]
    assert reason == "LOCAL_RENDER_AUTO_QA_VALIDATION_FAILED"


def test_crashed_render_claim_never_retries(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    row, token, status = ready._claim(queue)
    assert status == "LOCAL_RENDER_AUTO_QA_CLAIMED"
    assert row["job_id"] == receipt["job_id"]
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("crashed worker must not rerun"),
    )
    again = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert again["status"] == "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS"
    assert ready.inspect_completed_source_renders(queue=queue)["counts"]["RENDERING"] == 1


def test_existing_output_cannot_be_overwritten(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    root.mkdir()
    output = root / f"{receipt['job_id']}.mp4"
    output.write_bytes(b"EXISTING_OUTPUT_KEEP")
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("existing output should be rejected pre-execution"),
    )
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert output.read_bytes() == b"EXISTING_OUTPUT_KEEP"


def test_worker_default_cli_is_readonly_and_execute_is_explicit(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    args = [
        "render-ready-preview", "--queue", str(queue),
        "--preview-root", str(root),
    ]
    dry = CliRunner().invoke(cli.app, args)
    assert dry.exit_code == 0, dry.stdout
    assert json.loads(dry.stdout)["counts"]["PENDING"] == 1
    assert not root.exists()
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    actual = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert actual.exit_code == 0, actual.stdout
    assert json.loads(actual.stdout)["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert json.loads(actual.stdout)["ready"] is False


def test_render_duration_ceiling_fails_closed_without_ffmpeg(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    with sqlite3.connect(queue) as connection:
        payload = json.loads(connection.execute(
            "SELECT timeline_json FROM queue_jobs"
        ).fetchone()[0])
        payload["segments"][1]["source_end"] = 220
        payload["estimated_duration"] = 203
        connection.execute("UPDATE queue_jobs SET timeline_json=?", (json.dumps(payload),))
    monkeypatch.setattr(
        ready, "render_local_preview",
        lambda plan: pytest.fail("over-limit auto render must not launch"),
    )
    report = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert report["status"] == "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW"
    assert not root.exists()
