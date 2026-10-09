"""OS-process-shared conservative yt-dlp access pacing; completely offline."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from holoclip import cli

from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_pacing import (
    LocalYtDlpDeferred, LocalYtDlpPacer,
    inspect_local_ytdlp_pacing,
)


def test_cross_provider_access_is_serialized_without_exposing_video_ids(tmp_path, monkeypatch):
    clock = [10000.0]
    pauses = []
    import holoclip.source_ytdlp_pacing as pacing

    monkeypatch.setattr(pacing.time, "time", lambda: clock[0])
    def sleep(seconds):
        pauses.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(pacing.time, "sleep", sleep)
    path = tmp_path / "shared-access.sqlite3"
    first = LocalYtDlpPacer(path)
    second = LocalYtDlpPacer(path)
    with first.reserve(timeout_seconds=240):
        with pytest.raises(LocalYtDlpDeferred, match="OTHER_PROCESS_ACTIVE"):
            with second.reserve(timeout_seconds=240):
                pytest.fail("other process started while the first was in flight")
    with second.reserve(timeout_seconds=240):
        pass
    assert pauses == [pytest.approx(20)]
    assert path.exists()
    with sqlite3.connect(path) as db:
        next_at, active, cooldown = db.execute(
            "SELECT next_at, active_until, cooldown_until FROM access WHERE id=1"
        ).fetchone()
    assert next_at == pytest.approx(clock[0] + 20)
    assert active == 0
    assert cooldown == 0


def test_rate_limit_imposes_durable_cooldown_across_instances(tmp_path, monkeypatch):
    import holoclip.source_ytdlp_pacing as pacing
    clock = [2000.0]
    monkeypatch.setattr(pacing.time, "time", lambda: clock[0])
    gate = LocalYtDlpPacer(tmp_path / "pacing.sqlite3")
    with gate.reserve(timeout_seconds=240):
        gate.impose_cooldown()
    next_process = LocalYtDlpPacer(gate.path)
    clock[0] += 7200
    with pytest.raises(LocalYtDlpDeferred, match="COOLDOWN_ACTIVE"):
        with next_process.reserve(timeout_seconds=240):
            pytest.fail("cooldown was bypassed")
    # After the full cooldown, the process still respects the normal gap.
    clock[0] += 14400
    with next_process.reserve(timeout_seconds=240):
        pass


def test_invalid_gate_config_and_corrupt_state_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        LocalYtDlpPacer(tmp_path / "gate.db", gap_seconds=0)
    with pytest.raises(ValueError):
        LocalYtDlpPacer(tmp_path / "gate.db", cooldown_seconds=300)
    path = tmp_path / "gate.db"
    path.write_text("not a SQLite database", encoding="utf-8")
    with pytest.raises(LocalYtDlpDeferred, match="STORAGE_FAILURE"):
        with LocalYtDlpPacer(path).reserve(timeout_seconds=240):
            pytest.fail("must not send network traffic using corrupt shared state")


def test_bot_or_429_skips_refresh_and_defers_next_native_probe(tmp_path, monkeypatch):
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
        pacing_path=tmp_path / "access.sqlite3",
    )
    provider.runtime.manage_updates = True
    monkeypatch.setattr(provider.runtime, "command", lambda args: ["yt-dlp", *args])
    refreshed = []
    monkeypatch.setattr(provider.runtime, "refresh", lambda: refreshed.append(True))
    seen = []

    def reject(command, *, label, timeout_seconds):
        seen.append(command)
        raise yt_dlp.YtDlpError("HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(yt_dlp, "_run_process", reject)
    with pytest.raises(yt_dlp.YtDlpError, match="429"):
        provider.probe("https://youtu.be/AAAAAAAAAAA")
    assert refreshed == []
    assert len(seen) == 1
    cooldown_view = inspect_local_ytdlp_pacing(provider.pacer.path)
    assert cooldown_view["cooldown_reason"] == "HTTP_RATE_LIMIT"
    assert "--sleep-requests" in seen[0]
    assert seen[0][seen[0].index("--sleep-requests") + 1] == "2"
    assert "--sleep-interval" in seen[0]
    assert "--max-sleep-interval" in seen[0]
    assert seen[0][seen[0].index("--concurrent-fragments") + 1] == "1"
    with pytest.raises(yt_dlp.YtDlpError, match="LOCAL_YTDLP_COOLDOWN_ACTIVE"):
        provider.probe("https://youtu.be/BBBBBBBBBBB")
    assert len(seen) == 1


@pytest.mark.parametrize("message", [
    "Sign in to confirm you're not a bot",
    "This content isn't available, try again later",
    "HTTP Error 429: Too Many Requests",
])
def test_site_challenge_errors_never_refresh_yt_dlp(tmp_path, monkeypatch, message):
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
        pacing_path=tmp_path / "shared.db",
    )
    provider.runtime.manage_updates = True
    monkeypatch.setattr(provider.runtime, "command", lambda args: ["yt-dlp", *args])
    monkeypatch.setattr(
        provider.runtime, "refresh",
        lambda: pytest.fail("site rate limits must not trigger immediate retry"),
    )
    monkeypatch.setattr(
        yt_dlp, "_run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(yt_dlp.YtDlpError(message)),
    )
    with pytest.raises(yt_dlp.YtDlpError, match="(?i:bot|429|available)"):
        provider.probe("https://youtu.be/AAAAAAAAAAA")


def test_unrestricted_native_metadata_still_works_with_staggered_process(tmp_path, monkeypatch):
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
        pacing_path=tmp_path / "access.db",
    )
    monkeypatch.setattr(provider.runtime, "command", lambda args: ["yt-dlp", *args])
    calls = []
    def fake_process(command, *, label, timeout_seconds):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{"id":"AAAAAAAAAAA","title":"ok"}', stderr="")
    monkeypatch.setattr(yt_dlp, "_run_process", fake_process)
    clip = provider.probe("https://youtu.be/AAAAAAAAAAA")
    assert clip.external_id == "AAAAAAAAAAA"
    # Metadata probe is the only external-video operation. The provider
    # separately calls yt-dlp --version to record local runtime provenance;
    # this does not contact YouTube and must not be counted as a probe.
    metadata_requests = [cmd for cmd in calls if "--dump-single-json" in cmd]
    assert len(metadata_requests) == 1
    assert all(cmd[-1:] == ["--version"] or
               "--dump-single-json" in cmd for cmd in calls)


def test_pacing_reader_is_read_only_and_missing_state_is_not_fake_success(tmp_path):
    path = tmp_path / "subfolder" / "native state.db"
    before = inspect_local_ytdlp_pacing(path, now_seconds=10000)
    assert before["pacing_status"] == "UNKNOWN_NO_LOCAL_PACING_RECORD"
    assert before["next_attempt_not_before_utc"] is None
    assert before["site_access_guaranteed"] is False
    assert before["fresh_network_probes_attempted"] is False
    assert before["filesystem_mutated"] is False
    assert not path.exists()
    assert not path.parent.exists()


def test_reader_displays_sanitized_reason_and_resume_timestamp(tmp_path, monkeypatch):
    import holoclip.source_ytdlp_pacing as pacing

    path = tmp_path / "pacing.sqlite3"
    clock = [10000.0]
    monkeypatch.setattr(pacing.time, "time", lambda: clock[0])
    gate = LocalYtDlpPacer(path)
    with gate.reserve(timeout_seconds=30):
        gate.impose_cooldown("HTTP_RATE_LIMIT")
        active = inspect_local_ytdlp_pacing(path, now_seconds=clock[0])
        assert active["pacing_status"] == "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
        assert active["active_lease_until_utc"] is not None
    saved = inspect_local_ytdlp_pacing(path, now_seconds=10001)
    assert saved["pacing_status"] == "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
    assert saved["cooldown_reason"] == "HTTP_RATE_LIMIT"
    assert saved["seconds_until_not_before"] == 21600 - 1
    assert saved["cooldown_until_utc"] == datetime.fromtimestamp(
        10000 + 21600, timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    assert saved["next_attempt_not_before_utc"] == saved["cooldown_until_utc"]
    assert saved["ready"] is False
    assert saved["production_ready"] is False
    assert "http" not in json.dumps(saved).lower() or (
        "HTTP_RATE_LIMIT" in json.dumps(saved)
    )
    assert inspect_local_ytdlp_pacing(path, now_seconds=31600)["pacing_status"] == (
        "LOCAL_PACING_GATE_AVAILABLE"
    )


def test_reader_distinguishes_in_flight_and_normal_gap_without_mutation(
    tmp_path, monkeypatch,
):
    import holoclip.source_ytdlp_pacing as pacing

    clock = [12000.0]
    monkeypatch.setattr(pacing.time, "time", lambda: clock[0])
    path = tmp_path / "gate.db"
    gate = LocalYtDlpPacer(path)
    with gate.reserve(timeout_seconds=240):
        record = inspect_local_ytdlp_pacing(path, now_seconds=clock[0])
        assert record["pacing_status"] == "ACTIVE_OTHER_LOCAL_YTDLP_PROCESS"
        assert record["seconds_until_not_before"] >= 240
        assert record["active_lease_until_utc"] is not None
    waiting = inspect_local_ytdlp_pacing(path, now_seconds=12001)
    assert waiting["pacing_status"] == "WAITING_MINIMUM_LOCAL_ACCESS_GAP"
    assert waiting["seconds_until_not_before"] == 19
    assert waiting["active_lease_until_utc"] is None
    assert inspect_local_ytdlp_pacing(path, now_seconds=12020)["pacing_status"] == (
        "LOCAL_PACING_GATE_AVAILABLE"
    )


def test_reader_supports_preexisting_v1_cooldowns_and_writer_migrates_once(tmp_path, monkeypatch):
    import holoclip.source_ytdlp_pacing as pacing

    monkeypatch.setattr(pacing.time, "time", lambda: 10000.0)
    path = tmp_path / "pacing legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE access (id INTEGER PRIMARY KEY CHECK(id=1),"
            "next_at REAL NOT NULL, active_until REAL NOT NULL,"
            "cooldown_until REAL NOT NULL)"
        )
        db.execute("INSERT INTO access VALUES(1, 5, 0, 30000)")
    before = path.read_bytes()
    status = inspect_local_ytdlp_pacing(path, now_seconds=10000)
    assert path.read_bytes() == before
    assert status["pacing_status"] == "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
    assert status["cooldown_reason"] == "UNKNOWN_HISTORICAL_COOLDOWN_REASON"
    gate = LocalYtDlpPacer(path)
    # A writer opening the legacy gate upgrades schema transactionally,
    # but may NOT erase a previously saved pause.
    with pytest.raises(LocalYtDlpDeferred, match="COOLDOWN_ACTIVE"):
        with gate.reserve(timeout_seconds=30):
            pytest.fail("legacy active host cooldown erased")
    with sqlite3.connect(path) as db:
        fields = [x[1] for x in db.execute("PRAGMA table_info(access)")]
        assert "cooldown_reason" in fields
        assert db.execute("SELECT cooldown_until FROM access WHERE id=1").fetchone()[0] == 30000


def test_competing_local_reservations_have_a_total_bounded_wait(
    tmp_path, monkeypatch,
):
    import holoclip.source_ytdlp_pacing as pacing

    clock = [5000.0]
    monkeypatch.setattr(pacing.time, "time", lambda: clock[0])
    gate = LocalYtDlpPacer(tmp_path / "shared.sqlite3")
    with gate.reserve(timeout_seconds=30):
        pass

    sleeps = []

    def another_process_wins_after_sleep(duration):
        clock[0] += duration
        sleeps.append(duration)
        with sqlite3.connect(gate.path) as db:
            db.execute(
                "UPDATE access SET next_at=? WHERE id=1",
                (clock[0] + 20,),
            )

    monkeypatch.setattr(pacing.time, "sleep", another_process_wins_after_sleep)
    with pytest.raises(LocalYtDlpDeferred, match="PACING_WAIT_TOO_LONG"):
        with gate.reserve(timeout_seconds=30):
            pytest.fail("competing processes may not drive recursive waiting")
    assert sleeps == [20, 20, 20]
    assert sum(sleeps) == 60


def test_reader_rejects_corrupt_symlinked_or_forged_pacing_state(tmp_path):
    path = tmp_path / "pacing.db"
    path.write_bytes(b"not sqlite")
    assert inspect_local_ytdlp_pacing(path)["pacing_status"] == (
        "BLOCKED_UNREADABLE_LOCAL_PACING_STATE"
    )
    path.unlink()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE access (id INTEGER PRIMARY KEY,"
            "next_at REAL, active_until REAL, cooldown_until REAL,"
            "cooldown_reason TEXT)"
        )
        db.execute(
            "INSERT INTO access VALUES(1, 0, 0, 30000, 'https://invalid.example')"
        )
    assert inspect_local_ytdlp_pacing(path, now_seconds=10000)["pacing_status"] == (
        "BLOCKED_UNREADABLE_LOCAL_PACING_STATE"
    )
    with pytest.raises(LocalYtDlpDeferred, match="UNSAFE_STATE"):
        with LocalYtDlpPacer(path).reserve(timeout_seconds=30):
            pytest.fail("a forged cooldown cause cannot authorize access")
    target = tmp_path / "target.db"
    path.replace(target)
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert inspect_local_ytdlp_pacing(path)["pacing_status"] == (
        "BLOCKED_UNREADABLE_LOCAL_PACING_STATE"
    )


def test_pacing_reason_must_be_from_fixed_enum(tmp_path):
    gate = LocalYtDlpPacer(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="safe yt-dlp cooldown reason"):
        gate.impose_cooldown("https://youtube.com/private?secret=true")
    assert not gate.path.exists()


def test_access_status_command_is_offline_and_respects_configured_path(
    tmp_path, monkeypatch,
):
    path = tmp_path / "folder with spaces" / "pacing.sqlite3"
    path.parent.mkdir()
    gate = LocalYtDlpPacer(path)
    # Use current clock: gate persists a real future timestamp locally.
    gate.impose_cooldown("YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE")
    contents_before = path.read_bytes()
    result = CliRunner().invoke(
        cli.app, ["examples-ytdlp-access-status", "--pacing-path", str(path)],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["pacing_status"] == "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
    assert parsed["cooldown_reason"] == "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE"
    assert parsed["seconds_until_not_before"] >= 21590
    assert path.read_bytes() == contents_before
    assert "folder with spaces" not in result.stdout
    assert parsed["fresh_network_probes_attempted"] is False
    assert parsed["ready"] is False


def test_compact_health_includes_ytdlp_gate_without_touching_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gate = LocalYtDlpPacer(tmp_path / "data/runtime/yt-dlp-access.sqlite3")
    gate.impose_cooldown("HTTP_RATE_LIMIT")
    data_before = gate.path.read_bytes()
    result = CliRunner().invoke(
        cli.app, ["examples-collector-health", "--no-check-tools",
                  "--no-audit-media-cache"],
    )
    assert result.exit_code == 0, result.output
    health = json.loads(result.stdout)
    status = health["yt_dlp_access"]
    assert status["pacing_status"] == "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
    assert status["cooldown_reason"] == "HTTP_RATE_LIMIT"
    assert status["ready"] is False
    assert gate.path.read_bytes() == data_before
