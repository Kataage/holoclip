"""OS-process-shared conservative yt-dlp access pacing; completely offline."""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred, LocalYtDlpPacer


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
    assert len(calls) == 1
