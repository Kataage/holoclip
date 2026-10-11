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



def test_srt_generated_from_existing_asr_and_caption_qa_persisted(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    monkeypatch.setattr(ready, "burn_captioned_preview", lambda **kwargs: {
        "status": "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL",
        "output_sha256": "f" * 64,
        "subtitle_sha256": kwargs["subtitle_sha256"] or "",
    })
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / f"{VIDEO}.json").write_text(
        json.dumps([
            {"start": 10.25, "end": 11.25, "text": "こんにちは"},
            {"start": 12.5, "end": 14.0, "text": "cut boundary partial"},
            {"start": 20.5, "end": 21.5, "text": "嬉しい！"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    done = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, transcript_root=transcripts,
        execute=True,
    )
    assert done["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert done["subtitle_status"] == "LOCAL_SUBTITLE_SRT_CREATED_UNVERIFIED_ASR"
    assert done["subtitle_cue_count"] == 2
    assert done["subtitle_dropped_cue_count"] == 1
    assert done["burnin_status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL"
    assert done["burnin_output_sha256"] == "f" * 64
    video = root / f"{receipt['job_id']}.mp4"
    sidecar = video.with_suffix(".srt")
    assert video.is_file() and sidecar.is_file()
    srt = sidecar.read_text(encoding="utf-8")
    assert "00:00:00,250 --> 00:00:01,250" in srt
    assert "00:00:03,500 --> 00:00:04,500" in srt
    assert "cut boundary partial" not in srt
    with sqlite3.connect(queue) as conn:
        row = conn.execute(
            "SELECT status,cue_count,dropped_cue_count,output_sha256 "
            "FROM render_caption_evidence WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()
    assert row[0] == done["subtitle_status"]
    assert row[1:3] == (2, 1)
    assert len(row[3]) == 64
    status = ready.inspect_completed_source_renders(queue=queue)
    assert status["subtitle_counts"]["srt_created"] == 1
    assert status["burnin_counts"]["created"] == 1
    with sqlite3.connect(queue) as conn:
        burn = conn.execute(
            "SELECT status,output_sha256,subtitle_sha256 FROM render_burnin_evidence "
            "WHERE job_id=?", (receipt["job_id"],),
        ).fetchone()
    assert burn[0] == done["burnin_status"]
    assert burn[1] == "f" * 64
    assert burn[2] == row[3]
    assert done["ready"] is False


def test_missing_transcript_does_not_misclassify_mp4_qa(tmp_path, monkeypatch):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    done = ready.advance_completed_source_renders(
        queue=queue, preview_root=root,
        transcript_root=tmp_path / "no-transcripts", execute=True,
    )
    assert done["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert done["subtitle_status"] == "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    assert not (root / f"{receipt['job_id']}.srt").exists()
    status = ready.inspect_completed_source_renders(queue=queue)
    assert status["subtitle_counts"]["unavailable"] == 1
    assert status["burnin_counts"]["unavailable"] == 1
    assert status["counts"]["QA_PASSED"] == 1


def test_optional_burnin_encoder_failure_preserves_successful_video_qa(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / f"{VIDEO}.json").write_text(
        json.dumps([{"start": 10.2, "end": 11.0, "text": "安全な字幕"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ready, "burn_captioned_preview",
        lambda **kw: (_ for _ in ()).throw(ValueError("synthetic FFmpeg failure")),
    )
    outcome = ready.advance_completed_source_renders(
        queue=queue, preview_root=root,
        transcript_root=transcripts, execute=True,
    )
    assert outcome["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert outcome["burnin_status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert (root / f"{receipt['job_id']}.mp4").exists()
    assert (root / f"{receipt['job_id']}.srt").exists()
    assert ready.inspect_completed_source_renders(queue=queue)["burnin_counts"]["failed"] == 1
    with sqlite3.connect(queue) as conn:
        assert conn.execute(
            "SELECT state FROM render_jobs WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()[0] == "QA_PASSED"


def test_audio_qa_is_persisted_and_burnin_uses_leveled_output(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    observed = []
    def level(*, mp4_output, video_sha256, timeline):
        observed.append(("audio", mp4_output.name, timeline.id))
        mp4_output.with_name(f"{mp4_output.stem}.leveled.mp4").write_bytes(
            b"leveled test placeholder"
        )
        return {
            "status": "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL",
            "output_sha256": "e" * 64, "cut_count": 2,
            "max_abs_gain_db": 4.0,
            "before_mean_spread_db": 9.0, "after_mean_spread_db": 3.0,
        }
    def burn(*, mp4_output, video_sha256, subtitle_sha256):
        observed.append(("burn", mp4_output.name, video_sha256))
        return {
            "status": "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
            "output_sha256": "", "subtitle_sha256": subtitle_sha256 or "",
        }
    monkeypatch.setattr(ready, "level_preview_audio", level)
    monkeypatch.setattr(ready, "burn_captioned_preview", burn)
    finished = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert finished["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert finished["audio_status"] == "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL"
    assert observed == [
        ("audio", f"{receipt['job_id']}.mp4", timeline.id),
        ("burn", f"{receipt['job_id']}.leveled.mp4", "e" * 64),
    ]
    with sqlite3.connect(queue) as conn:
        row = conn.execute(
            "SELECT status,output_sha256,cut_count,max_abs_gain_db,"
            "before_mean_spread_db,after_mean_spread_db "
            "FROM render_audio_evidence WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()
        state = conn.execute("SELECT state FROM render_jobs").fetchone()[0]
    assert row == ("LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL",
                   "e" * 64, 2, 4.0, 9.0, 3.0)
    assert state == "QA_PASSED"
    assert ready.inspect_completed_source_renders(queue=queue)["audio_counts"]["leveled"] == 1


def test_optional_audio_failure_keeps_original_qa_and_caption_flow(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    monkeypatch.setattr(ready, "level_preview_audio", lambda **kw: {
        "status": "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED",
        "output_sha256": "", "cut_count": 0, "max_abs_gain_db": 0.0,
        "before_mean_spread_db": None, "after_mean_spread_db": None,
    })
    called = []
    monkeypatch.setattr(ready, "burn_captioned_preview", lambda **kw: (
        called.append(kw["mp4_output"]) or {
            "status": "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
            "output_sha256": "", "subtitle_sha256": "",
        }
    ))
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert result["audio_status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"
    assert called == [root / f"{receipt['job_id']}.mp4"]
    assert ready.inspect_completed_source_renders(queue=queue)["audio_counts"]["failed"] == 1


def test_readonly_completed_source_count_not_shadowed_by_optional_media_rows(tmp_path):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    # New optional evidence tables may be present without any burn-in rows.
    connection = ready._db(queue, write=True)
    assert connection is not None
    connection.close()
    result = ready.inspect_completed_source_renders(queue=queue)
    assert result["source_complete_count"] == 1
    assert result["counts"]["PENDING"] == 1
    assert result["burnin_counts"]["created"] == 0
    assert result["audio_counts"]["leveled"] == 0


def test_visual_evidence_saves_render_plan_style_receipt_without_editorial_claim(
    tmp_path, monkeypatch,
):
    from holoclip.render_visual_style import style_evidence

    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    stub = ready.render_local_preview

    def fake_with_visual_profile(plan):
        result = stub(plan)
        result["visual_style"] = style_evidence(plan.timeline)
        return result

    monkeypatch.setattr(ready, "render_local_preview", fake_with_visual_profile)
    outcome = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert outcome["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert outcome["visual_status"] == "LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY"
    assert outcome["visual_zoom_cut_count"] == 0
    assert outcome["visual_dip_transition_count"] == 0
    assert outcome["ready"] is False and outcome["publication_performed"] is False
    with sqlite3.connect(queue) as connection:
        row = connection.execute(
            "SELECT status,style_version,zoom_cut_count,dip_transition_count "
            "FROM render_visual_evidence WHERE job_id=?", (receipt["job_id"],),
        ).fetchone()
    assert row == (
        "LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY",
        "holoclip-visual-style-v1", 0, 0,
    )
    overview = ready.inspect_completed_source_renders(queue=queue)
    assert overview["visual_counts"] == {"encoded": 1, "unverified": 0}
    assert overview["source_complete_count"] == 1


def test_missing_visual_renderer_proof_never_claims_effects_applied(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    outcome = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert outcome["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert outcome["visual_status"] == "LOCAL_RENDER_VISUAL_UNVERIFIED"
    report = ready.inspect_completed_source_renders(queue=queue)
    assert report["counts"]["QA_PASSED"] == 1
    assert report["visual_counts"]["unverified"] == 1


def test_edit_diagnostic_uses_final_artifact_and_persists_flags(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)

    def leveled(*, mp4_output, video_sha256, timeline):
        mp4_output.with_name(f"{mp4_output.stem}.leveled.mp4").write_bytes(
            b"verified leveled audio"
        )
        return {
            "status": "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL",
            "output_sha256": "e" * 64, "cut_count": 2,
            "max_abs_gain_db": 2.0,
            "before_mean_spread_db": 8.0, "after_mean_spread_db": 3.0,
        }

    monkeypatch.setattr(ready, "level_preview_audio", leveled)
    monkeypatch.setattr(ready, "burn_captioned_preview", lambda **kw: {
        "status": "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
        "output_sha256": "", "subtitle_sha256": kw["subtitle_sha256"] or "",
    })
    seen = []

    def measured(**kwargs):
        seen.append(kwargs)
        return {
            "status": "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
            "version": "holoclip-edit-diagnostic-v1",
            "base_sha256": kwargs["video_sha256"],
            "final_sha256": kwargs["final_sha256"],
            "black_interval_count": 2,
            "freeze_interval_count": 1,
            "subtitle_checked": False,
            "subtitle_fast_cue_count": 0,
            "subtitle_overflow_count": 0,
            "audio_sample_mean_dbfs": -28.5,
            "audio_sample_peak_dbfs": -1.0,
            "flag_count": 3,
            "ready": False,
            "production_ready": False,
        }

    monkeypatch.setattr(ready, "evaluate_local_edit_quality", measured)
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert result["quality_status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS"
    assert result["quality_flag_count"] == 3
    assert seen[0]["mp4_output"] == root / f"{receipt['job_id']}.mp4"
    assert seen[0]["final_output"] == root / f"{receipt['job_id']}.leveled.mp4"
    assert seen[0]["final_sha256"] == "e" * 64
    assert result["ready"] is False and result["publication_performed"] is False
    with sqlite3.connect(queue) as conn:
        row = conn.execute(
            "SELECT status,version,base_sha256,final_sha256,"
            "black_interval_count,freeze_interval_count,"
            "subtitle_checked,subtitle_fast_cue_count,"
            "subtitle_overflow_count,audio_sample_mean_dbfs,"
            "audio_sample_peak_dbfs,flag_count "
            "FROM render_quality_evidence WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()
        state = conn.execute("SELECT state FROM render_jobs").fetchone()[0]
    assert row == (
        "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
        "holoclip-edit-diagnostic-v1",
        "a" * 64, "e" * 64, 2, 1, 0, 0, 0, -28.5, -1.0, 3,
    )
    assert state == "QA_PASSED"
    assert ready.inspect_completed_source_renders(queue=queue)["quality_counts"] == {
        "flags": 1, "no_flags": 0, "partial": 0, "unavailable": 0,
    }


def test_optional_diagnostic_failure_never_revokes_render_qa(
    tmp_path, monkeypatch,
):
    db, cache, queue, root, timeline, receipt = _case(tmp_path)
    _probe(monkeypatch)
    _fake_render(monkeypatch)
    result = ready.advance_completed_source_renders(
        queue=queue, preview_root=root, execute=True,
    )
    assert result["status"] == "LOCAL_RENDER_AUTO_QA_PASSED_TECHNICAL_ONLY"
    assert result["quality_status"] == "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"
    assert result["quality_flag_count"] == 0
    assert (root / f"{receipt['job_id']}.mp4").exists()
    assert ready.inspect_completed_source_renders(queue=queue)["quality_counts"][
        "unavailable"
    ] == 1
    with sqlite3.connect(queue) as conn:
        assert conn.execute("SELECT state FROM render_jobs").fetchone()[0] == "QA_PASSED"
