"""Once-approved analyze/discover candidate -> durable original-source queue bridge."""
from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.domain import SourceAsset, make_source_asset_id
from holoclip.evaluation import build_run
from holoclip.models import ClipCandidate
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.render_candidate_autoqueue import (
    enroll_candidate_run, read_candidate_autoqueue_config,
    setup_candidate_autoqueue,
)
from holoclip.render_source_queue import inspect_render_queue

ID = "AbCdEf123_0"


def _case(tmp_path):
    db = tmp_path / "canonical.db"
    root = tmp_path / "local-sources"
    root.mkdir()
    queue = tmp_path / "renderqueue.sqlite3"
    config = tmp_path / "candidate-autoqueue.json"
    with SQLiteCanonicalRepository(db) as repo:
        repo.upsert_source_asset(SourceAsset(
            id=make_source_asset_id("youtube", ID),
            platform="youtube", external_id=ID, source_type="stream",
            availability="available", access_class="public", duration=3600,
        ))
    return db, root, queue, config


def _run(*, stream_id=ID, scores=(0.94, 0.92), warnings=None):
    candidates = []
    for k, confidence in enumerate(scores):
        candidates.append(ClipCandidate(
            start=60 + 120 * k, end=85 + 120 * k,
            working_title=f"Candidate {k}",
            setup="context", development="reaction", payoff="laugh",
            why_interesting="surprising response",
            confidence=confidence,
            warnings=warnings or [],
        ))
    return build_run(
        stream_id=stream_id, transcript_path="data/transcripts/original.json",
        candidates=candidates,
    )


def _setup(db, root, q, config, *, apply=True, approve=True, **options):
    return setup_candidate_autoqueue(
        config=config, database=db, cache_root=root, queue=q,
        apply=apply, approve_local_preview=approve, **options,
    )


def test_setup_previews_without_mutating_anything(tmp_path):
    db, root, q, config = _case(tmp_path)
    report = _setup(db, root, q, config, apply=False)
    assert report["status"] == "LOCAL_RENDER_AUTOQUEUE_SETUP_PREVIEW"
    assert not config.exists() and not q.exists()
    assert read_candidate_autoqueue_config(config) is None
    assert report["ready"] is False


def test_one_time_explicit_setup_no_overwrite_and_no_network(tmp_path):
    db, root, q, config = _case(tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"):
        _setup(db, root, q, config, approve=False)
    assert not config.exists()
    first = _setup(db, root, q, config)
    assert first["status"] == "LOCAL_RENDER_AUTOQUEUE_CONFIGURED"
    original = config.read_bytes()
    repeat = _setup(db, root, q, config, min_confidence=0.75)
    assert repeat["status"] == "LOCAL_RENDER_AUTOQUEUE_ALREADY_CONFIGURED"
    assert config.read_bytes() == original
    assert not q.exists()
    assert "private" not in first
    assert first["ready"] is False


def test_eligible_top_candidate_enrolled_once_without_manual_timeline(tmp_path):
    db, root, q, config = _case(tmp_path)
    _setup(db, root, q, config)
    run = _run()
    one = enroll_candidate_run(run, config=config)
    assert one["status"] == "LOCAL_RENDER_AUTOQUEUE_ENROLLED"
    assert one["enrolled_count"] == 1
    assert one["skipped_count"] == 1
    assert one["youtube_contacted"] is False
    assert inspect_render_queue(queue=q)["counts"]["QUEUED"] == 1
    with sqlite3.connect(q) as conn:
        payload = conn.execute("SELECT timeline_json FROM queue_jobs").fetchone()[0]
    doc = json.loads(payload)
    assert doc["segments"][0]["source_asset_id"] == make_source_asset_id("youtube", ID)
    assert doc["segments"][0]["source_start"] == 60
    assert doc["segments"][0]["source_end"] == 85
    again = enroll_candidate_run(run, config=config)
    assert again["status"] == "LOCAL_RENDER_AUTOQUEUE_ALREADY_PROCESSED"
    assert again["already_enrolled_count"] == 1
    assert inspect_render_queue(queue=q)["total"] == 1


def test_multiple_eligible_candidates_respect_configured_budget(tmp_path):
    db, root, q, config = _case(tmp_path)
    _setup(db, root, q, config, max_candidates_per_run=2)
    result = enroll_candidate_run(_run(scores=(0.96, 0.91, 0.85)), config=config)
    assert result["enrolled_count"] == 2
    assert result["skipped_count"] == 1
    assert inspect_render_queue(queue=q)["total"] == 2


@pytest.mark.parametrize(("stream_id", "scores", "warnings"), [
    ("not-the-video-id", (0.99,), None),
    ("AbCdEf123_1", (0.99,), None),
    (ID, (0.4,), None),
    (ID, (0.98,), ["context_risk"]),
    (ID, (), None),
])
def test_unknown_or_low_quality_candidates_do_not_enqueue(
    tmp_path, stream_id, scores, warnings,
):
    db, root, q, config = _case(tmp_path)
    _setup(db, root, q, config)
    result = enroll_candidate_run(
        _run(stream_id=stream_id, scores=scores, warnings=warnings),
        config=config,
    )
    assert result["enrolled_count"] == 0
    assert not q.exists()


def test_forged_candidate_id_does_not_enqueue(tmp_path):
    db, root, q, config = _case(tmp_path)
    _setup(db, root, q, config)
    run = _run()
    run.candidates[0].id = "forged"
    result = enroll_candidate_run(run, config=config)
    assert result["enrolled_count"] == 0
    assert not q.exists()


def test_candidate_beyond_measured_stream_duration_not_queued(tmp_path):
    db, root, q, config = _case(tmp_path)
    _setup(db, root, q, config)
    run = _run()
    run.candidates[0].candidate.end = 4000
    from holoclip.evaluation import make_candidate_id
    run.candidates[0].id = make_candidate_id(ID, run.candidates[0].candidate)
    result = enroll_candidate_run(run, config=config)
    assert result["enrolled_count"] == 0
    assert not q.exists()


def test_non_stream_source_is_not_eligible_even_with_matching_id(tmp_path):
    db, root, q, config = _case(tmp_path)
    with SQLiteCanonicalRepository(db) as repo:
        original = repo.get_source_asset(make_source_asset_id("youtube", ID))
        repo.upsert_source_asset(original.model_copy(update={"source_type": "clip"}))
    _setup(db, root, q, config)
    result = enroll_candidate_run(_run(), config=config)
    assert result["status"] == "LOCAL_RENDER_AUTOQUEUE_SOURCE_UNVERIFIED"
    assert not q.exists()


def test_corrupt_config_fails_closed_and_does_not_create_queue(tmp_path):
    db, root, q, config = _case(tmp_path)
    config.write_text('{"version":"fake","secret":"NOT_FOR_REPORT"}')
    with pytest.raises(ValueError, match="LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED"):
        enroll_candidate_run(_run(), config=config)
    assert not q.exists()


def test_cli_setup_preview_then_one_time_enable(tmp_path):
    db, root, q, config = _case(tmp_path)
    args = [
        "render-candidate-autoqueue-setup", "--config", str(config),
        "--queue", str(q), "--database", str(db),
        "--cache-root", str(root),
    ]
    dry = CliRunner().invoke(cli.app, args)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["status"] == "LOCAL_RENDER_AUTOQUEUE_SETUP_PREVIEW"
    assert not config.exists()
    done = CliRunner().invoke(cli.app, [
        *args, "--apply", "--approve-local-preview",
    ])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["status"] == "LOCAL_RENDER_AUTOQUEUE_CONFIGURED"
    assert config.exists() and not q.exists()


def test_analyze_discover_hook_sanitizes_errors(monkeypatch, capsys):
    run = _run()
    seen = []
    monkeypatch.setattr(
        cli, "enroll_candidate_run",
        lambda candidate_run: seen.append(candidate_run) or {
            "status": "LOCAL_RENDER_AUTOQUEUE_ENROLLED",
            "enrolled_count": 1, "already_enrolled_count": 0,
        },
    )
    cli._autoqueue_generated_candidates(run)
    assert seen == [run]
    assert "LOCAL_RENDER_AUTOQUEUE_ENROLLED" in capsys.readouterr().out
    monkeypatch.setattr(
        cli, "enroll_candidate_run",
        lambda x: (_ for _ in ()).throw(
            ValueError("SECRET_SOURCE_PATH")
        ),
    )
    cli._autoqueue_generated_candidates(run)
    visible = capsys.readouterr().out
    assert "LOCAL_RENDER_AUTOQUEUE_BLOCKED" in visible
    assert "SECRET_SOURCE_PATH" not in visible
