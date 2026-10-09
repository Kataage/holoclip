"""A run's audit is a bounded fact, not gold or silent duplicate success."""
import asyncio
import json

import pytest

from holoclip.source_pattern_worker import (
    VERSION, _state_lock, run_locked_source_pattern_collection,
)

NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
}


def _run(tmp_path, **overrides):
    options = dict(
        store=tmp_path / "examples.jsonl",
        state_path=tmp_path / "state.json",
        output=tmp_path / "videos",
        report_path=tmp_path / "last-run.json",
        policy_rules=[],
        cycles=2, discover=False,
    )
    options.update(overrides)
    provider = options.pop("provider", None)
    return asyncio.run(run_locked_source_pattern_collection(provider, **options))


def test_success_writes_audit_without_gold(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def fake(_provider, **_kwargs):
        return {
            "version": "test-collector",
            "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
            **NO_GOLD,
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", fake)
    result = _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["version"] == VERSION
    assert report["status"] == result["status"]
    assert report["result"]["status"] == result["status"]
    assert result["report_path"] == str(tmp_path / "last-run.json")
    assert report["started_at_utc"] <= report["finished_at_utc"]
    assert report["ready"] is False
    assert report["reviewed_gold_available"] is False
    cache = report["result"]["source_cache_byte_attestation"]
    assert cache["status"] == "UNKNOWN_NO_SOURCE_CACHE_EVIDENCE"
    assert cache["source_identity_verified"] is False
    assert cache["actual_public_video_verified"] is False
    assert cache["ready"] is False
    # Existing lock file is normal: the OS lock is released on exit.
    assert (tmp_path / ".source-pattern-collector.lock").exists()
    result2 = _run(tmp_path)
    assert result2["status"] == result["status"]


def test_post_run_byte_audit_targets_new_attempts_not_first_old_clip(
    tmp_path, monkeypatch,
):
    from holoclip import source_pattern_worker as worker

    async def fake(_provider, **_kwargs):
        return {
            "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
            "cycle_diagnostics": [
                {"attempted_clip_keys": ["old", "fresh"]},
                {"attempted_clip_keys": ["fresh", "newest"]},
            ],
            **NO_GOLD,
        }

    seen = []

    def audit(**kwargs):
        seen.append(kwargs.get("clip_keys"))
        return {
            "status": "UNKNOWN_PARTIAL_BOUNDED_CACHE_AUDIT",
            "source_identity_verified": False, "ready": False,
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", fake)
    monkeypatch.setattr(worker, "audit_cached_source_video_sections", audit)
    result = _run(tmp_path)
    assert seen == [["old", "fresh", "newest"]]
    assert result["source_cache_byte_attestation"]["ready"] is False
    assert result["ready"] is False


def test_second_process_lock_refuses_network_without_replacing_audit(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def forbidden(_provider, **_kwargs):
        raise AssertionError("overlapping process entered real collection")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    state = tmp_path / "state.json"
    with _state_lock(state) as acquired:
        assert acquired is True
        outcome = _run(tmp_path)
    assert outcome["status"] == "BLOCKED_CONCURRENT_COLLECTION"
    assert outcome["ready"] is False
    assert not (tmp_path / "last-run.json").exists()


def test_crash_is_audited_and_lock_released(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def broken(_provider, **_kwargs):
        raise RuntimeError("network location must not be copied to audit")

    monkeypatch.setattr(worker, "collect_public_source_patterns", broken)
    with pytest.raises(RuntimeError):
        _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["status"] == "UNKNOWN_COLLECTION_EXECUTION_FAILURE"
    assert report["error_class"] == "RuntimeError"
    assert "network location" not in json.dumps(report)
    assert report["result"] is None
    assert report["ready"] is False
    with _state_lock(tmp_path / "state.json") as acquired:
        assert acquired is True


def test_forged_success_claim_cannot_be_written_as_valid_audit(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def promoted(_provider, **_kwargs):
        return {
            "status": "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS",
            **{**NO_GOLD, "ready": True},
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", promoted)
    with pytest.raises(ValueError, match="gold"):
        _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["status"] == "UNKNOWN_COLLECTION_EXECUTION_FAILURE"


def test_lock_path_symlink_refused(tmp_path):
    (tmp_path / ".source-pattern-collector.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        _run(tmp_path)


@pytest.mark.parametrize("which", ["state", "examples"])
def test_report_cannot_clobber_source_files(tmp_path, which):
    path = tmp_path / ("state.json" if which == "state" else "examples.jsonl")
    with pytest.raises(ValueError, match="overwrite"):
        _run(tmp_path, report_path=path)
    assert not path.exists()


def test_scheduled_collector_respects_cooldown_then_resumes_after_local_expiry(
    tmp_path, monkeypatch,
):
    from holoclip import source_pattern_worker as worker
    from holoclip import source_ytdlp_pacing as pacing
    from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector

    now = [10_000.0]
    monkeypatch.setattr(pacing.time, "time", lambda: now[0])
    gate = pacing.LocalYtDlpPacer(tmp_path / "runtime" / "gate.sqlite3")
    gate.impose_cooldown("HTTP_RATE_LIMIT")

    class Provider:
        pacer = gate

    measurements = []

    async def collector(_provider, **_kwargs):
        measurements.append(True)
        return {
            "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
            "new_unique_video_samples_this_run": 0,
            "new_distinct_candidate_streams_this_run": 0,
            **NO_GOLD,
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", collector)

    report = _run(tmp_path, provider=Provider())
    assert report["status"] == "DEFERRED_LOCAL_YTDLP_ACCESS"
    assert report["collection_started"] is False
    assert report["new_unique_video_samples_this_run"] is None
    assert report["new_distinct_candidate_streams_this_run"] is None
    assert report["local_access_state"] == (
        "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
    )
    assert report["local_cooldown_reason"] == "HTTP_RATE_LIMIT"
    assert measurements == []
    assert not (tmp_path / "state.json").exists()

    saved = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert saved["status"] == "DEFERRED_LOCAL_YTDLP_ACCESS"
    assert saved["result"]["new_unique_video_samples_this_run"] is None
    assert saved["ready"] is False
    diagnosis = diagnose_source_pattern_collector(
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "last-run.json",
        launcher_path=tmp_path / "launcher.json",
        check_tools=False, media_output=tmp_path / "videos",
    )
    assert diagnosis["status"] == "DEFERRED_LOCAL_YTDLP_ACCESS"
    assert diagnosis["local_collection_health"]["primary_blocker"] == (
        "LOCAL_YTDLP_ACCESS_PAUSED"
    )
    assert diagnosis["new_unique_video_samples_last_run"] is None
    assert diagnosis["last_run_access_deferred"] is True

    # Scheduled recurrence after the *actual* locally recorded six-hour
    # cooldown enters the regular bounded collector. The reader performs
    # no probing; actual yt-dlp still rechecks its own exclusive gate.
    now[0] += 21600
    resumed = _run(tmp_path, provider=Provider())
    assert resumed["status"] == "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS"
    assert resumed["new_unique_video_samples_this_run"] == 0
    assert measurements == [True]
    assert resumed["ready"] is False


def test_active_lease_skips_entire_collection_without_false_zeroes(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker
    from holoclip.source_ytdlp_pacing import LocalYtDlpPacer

    async def forbidden(_provider, **_kwargs):
        pytest.fail("an active host lease must not start a second collector")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    gate = LocalYtDlpPacer(tmp_path / "shared.db")

    class Provider:
        pacer = gate

    with gate.reserve(timeout_seconds=90):
        report = _run(tmp_path, provider=Provider())
    assert report["status"] == "DEFERRED_LOCAL_YTDLP_ACCESS"
    assert report["local_access_state"] == "ACTIVE_OTHER_LOCAL_YTDLP_PROCESS"
    assert report["new_unique_video_samples_this_run"] is None


def test_corrupt_local_pacing_state_blocks_scheduled_collector(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker
    from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector
    from holoclip.source_ytdlp_pacing import LocalYtDlpPacer

    async def forbidden(_provider, **_kwargs):
        pytest.fail("unsafe local yt-dlp state may never start collection")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    path = tmp_path / "access.sqlite3"
    path.write_text("corrupt SQLite", encoding="utf-8")
    gate = LocalYtDlpPacer(path)

    class Provider:
        pacer = gate

    blocked = _run(tmp_path, provider=Provider())
    assert blocked["status"] == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
    assert blocked["collection_started"] is False
    assert blocked["new_unique_video_samples_this_run"] is None
    diagnostic = diagnose_source_pattern_collector(
        state_path=tmp_path / "state.json",
        report_path=tmp_path / "last-run.json",
        launcher_path=tmp_path / "launcher.json",
        check_tools=False, media_output=tmp_path / "videos",
    )
    assert diagnostic["status"] == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
    assert diagnostic["local_collection_health"]["primary_blocker"] == (
        "LOCAL_YTDLP_PACING_STORAGE_BLOCKED"
    )
    assert diagnostic["ready"] is False


def test_deferred_cooldown_audit_rejects_fabricated_measurements(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker
    from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector
    from holoclip.source_ytdlp_pacing import LocalYtDlpPacer

    monkeypatch.setattr(
        worker, "collect_public_source_patterns",
        lambda *_args, **_kwargs: pytest.fail("defer must skip collection"),
    )
    path = tmp_path / "pacing.db"
    gate = LocalYtDlpPacer(path)
    gate.impose_cooldown("HTTP_RATE_LIMIT")

    class Provider:
        pacer = gate

    _run(tmp_path, provider=Provider())
    saved_path = tmp_path / "last-run.json"
    audit = json.loads(saved_path.read_text(encoding="utf-8"))
    audit["result"]["new_unique_video_samples_this_run"] = 99
    saved_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="Deferred"):
        diagnose_source_pattern_collector(
            state_path=tmp_path / "state.json", report_path=saved_path,
            launcher_path=tmp_path / "launcher.json", check_tools=False,
        )
    audit["result"]["new_unique_video_samples_this_run"] = None
    audit["result"]["editor_cut_precision"] = 0.99
    saved_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="Deferred"):
        diagnose_source_pattern_collector(
            state_path=tmp_path / "state.json", report_path=saved_path,
            launcher_path=tmp_path / "launcher.json", check_tools=False,
        )
    audit["result"]["editor_cut_precision"] = None
    audit["result"]["fabricated_finished_video_evidence"] = True
    saved_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="Deferred"):
        diagnose_source_pattern_collector(
            state_path=tmp_path / "state.json", report_path=saved_path,
            launcher_path=tmp_path / "launcher.json", check_tools=False,
        )


def test_corrupt_gate_returns_nonzero_cli_without_network(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from holoclip import cli
    from holoclip import source_pattern_worker as worker
    from holoclip.source_ytdlp_pacing import LocalYtDlpPacer

    path = tmp_path / "corrupt.db"
    path.write_text("unsafe sqlite file", encoding="utf-8")
    gate = LocalYtDlpPacer(path)

    class Provider:
        pacer = gate

    async def forbidden(*_args, **_kwargs):
        pytest.fail("CLI cannot enter real collection with corrupt access state")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **_kw: Provider())
    monkeypatch.setattr(cli, "load_policy_rules", lambda *_args: [])

    response = CliRunner().invoke(cli.app, [
        "examples-collect-source-patterns",
        "--store", str(tmp_path / "examples.jsonl"),
        "--state", str(tmp_path / "state.json"),
        "--output", str(tmp_path / "video"),
        "--report-path", str(tmp_path / "last-run.json"),
    ])
    assert response.exit_code == 6, response.output
    result = json.loads(response.stdout)
    assert result["status"] == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
    assert result["collection_started"] is False
    assert result["ready"] is False
    assert not (tmp_path / "state.json").exists()


def test_current_failed_launcher_preserves_same_run_pacing_blocker(
    tmp_path, monkeypatch,
):
    from datetime import datetime, timedelta

    from holoclip import source_pattern_worker as worker
    from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector
    from holoclip.source_ytdlp_pacing import LocalYtDlpPacer

    async def forbidden(*_args, **_kwargs):
        pytest.fail("broken pacing database must stop before network access")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    path = tmp_path / "unsafe.db"
    path.write_bytes(b"not a SQLite gate")

    class Provider:
        pacer = LocalYtDlpPacer(path)

    _run(tmp_path, provider=Provider())
    audit = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(audit["started_at_utc"]) - timedelta(seconds=3)
    end = datetime.fromisoformat(audit["finished_at_utc"]) + timedelta(seconds=3)
    (tmp_path / "launcher.json").write_text(json.dumps({
        "version": "source-pattern-windows-launcher-v1",
        "status": "FAILED_COLLECTOR_PROCESS", "exit_code": 6,
        "error_class": None, "ran_collector": True,
        "started_at_utc": start.isoformat(),
        "finished_at_utc": end.isoformat(),
        **{key: NO_GOLD[key] for key in (
            "source_identity_verified", "reviewed_gold_available",
            "qualification_allowed", "ready", "production_ready",
        )},
    }), encoding="utf-8")
    diagnosis = diagnose_source_pattern_collector(
        state_path=tmp_path / "state.json", report_path=tmp_path / "last-run.json",
        launcher_path=tmp_path / "launcher.json", check_tools=False,
    )
    assert diagnosis["status"] == "BLOCKED_LOCAL_YTDLP_PACING_STATE"
    assert diagnosis["local_collection_health"]["primary_blocker"] == (
        "LOCAL_YTDLP_PACING_STORAGE_BLOCKED"
    )
    # Do not use a past corrupt state audit for a DIFFERENT launcher failure.
    latest_launcher = json.loads(
        (tmp_path / "launcher.json").read_text(encoding="utf-8")
    )
    latest_launcher["started_at_utc"] = (
        end + timedelta(seconds=1)
    ).isoformat()
    latest_launcher["finished_at_utc"] = (
        end + timedelta(seconds=2)
    ).isoformat()
    (tmp_path / "launcher.json").write_text(json.dumps(latest_launcher), encoding="utf-8")
    stale = diagnose_source_pattern_collector(
        state_path=tmp_path / "state.json", report_path=tmp_path / "last-run.json",
        launcher_path=tmp_path / "launcher.json", check_tools=False,
    )
    assert stale["status"] == "BLOCKED_LATEST_WINDOWS_LAUNCH"
