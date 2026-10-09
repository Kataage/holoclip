from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from holoclip.providers.source.base import AcquisitionProfile, SourceMetadata
from holoclip.providers.source import yt_dlp


def test_source_candidates_prefer_explicit_original_link() -> None:
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp",
        js_runtime="node",
        manage_updates=False,
    )
    metadata = SourceMetadata(
        provider="yt-dlp",
        url="https://www.youtube.com/watch?v=clip123",
        webpage_url="https://www.youtube.com/watch?v=clip123",
        external_id="clip123",
        title="clip",
        description=(
            "元配信はこちら https://www.youtube.com/watch?v=source123\n"
            "関連動画 https://youtu.be/related456"
        ),
    )

    candidates = provider.source_candidates(metadata)

    assert len(candidates) == 2
    assert candidates[0].url == "https://www.youtube.com/watch?v=source123"
    assert candidates[0].score == pytest.approx(0.90)
    assert candidates[0].reason == "description_explicit_source_label"
    assert candidates[1].score == pytest.approx(0.45)
    assert yt_dlp.choose_source_candidate(candidates) == candidates[0]



def test_source_candidates_use_adjacent_lines_for_source_context() -> None:
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp",
        js_runtime="node",
        manage_updates=False,
    )
    metadata = SourceMetadata(
        provider="yt-dlp",
        url="https://www.youtube.com/watch?v=tPmHMqtjLIw",
        webpage_url="https://www.youtube.com/watch?v=tPmHMqtjLIw",
        external_id="tPmHMqtjLIw",
        title="Leon or Chris, Risu?",
        description=(
            "This clip is taken from this stream:\n"
            "#MMMalas : Akhirnya Monday Malam Malaszszszs\n"
            "https://www.youtube.com/watch?v=yqjeW_LSMOI\n\n"
            "Ayunda Risu:\n"
            "https://twitter.com/ayunda_risu"
        ),
    )

    candidates = provider.source_candidates(metadata)

    assert len(candidates) == 1
    assert candidates[0].url == "https://www.youtube.com/watch?v=yqjeW_LSMOI"
    assert candidates[0].score == pytest.approx(0.90)
    assert candidates[0].reason == "description_explicit_source_label"
    assert "This clip is taken from this stream" in (candidates[0].context or "")
    assert yt_dlp.choose_source_candidate(candidates) == candidates[0]


def test_single_low_score_youtube_link_is_not_auto_selected() -> None:
    candidate = yt_dlp.SourceCandidate(
        url="https://youtu.be/related",
        score=0.45,
        reason="description_youtube_link",
    )

    assert yt_dlp.choose_source_candidate([candidate]) is None


def test_equal_explicit_source_candidates_are_not_auto_selected() -> None:
    candidates = [
        yt_dlp.SourceCandidate(
            url="https://youtu.be/a",
            score=0.90,
            reason="description_explicit_source_label",
        ),
        yt_dlp.SourceCandidate(
            url="https://youtu.be/b",
            score=0.90,
            reason="description_explicit_source_label",
        ),
    ]

    assert yt_dlp.choose_source_candidate(candidates) is None


def test_training_acquisition_uses_audio_profile_and_reuses_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, label: str = "command", timeout_seconds: int = 240) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="2026.10.01\n",
                stderr="",
            )
        if "--dump-single-json" in command:
            payload = {
                "id": "source123",
                "title": "source",
                "webpage_url": "https://www.youtube.com/watch?v=source123",
                "channel": "channel",
                "description": "",
                "duration": 123.0,
                "extractor_key": "Youtube",
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(payload),
                stderr="",
            )

        directory = Path(command[command.index("--paths") + 1])
        directory.mkdir(parents=True, exist_ok=True)
        media = directory / "media.webm"
        media.write_bytes(b"audio")
        (directory / "media.info.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=str(media) + "\n",
            stderr="",
        )

    monkeypatch.setattr(yt_dlp, "_run_process", fake_run)
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp",
        js_runtime="node",
        manage_updates=False,
    )

    first = provider.acquire(
        "https://www.youtube.com/watch?v=source123",
        tmp_path,
        profile=AcquisitionProfile.TRAINING_AUDIO,
    )
    second = provider.acquire(
        "https://www.youtube.com/watch?v=source123",
        tmp_path,
        profile=AcquisitionProfile.TRAINING_AUDIO,
    )

    download_call = next(call for call in calls if "--format" in call)
    assert download_call[download_call.index("--format") + 1] == "bestaudio/best"
    assert "--merge-output-format" not in download_call
    assert first.media_path.exists()
    assert first.reused is False
    assert second.media_path == first.media_path
    assert second.reused is True



def test_managed_runtime_refreshes_latest_nightly_once(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_which(name: str):
        if name in {"uvx", "uvx.exe"}:
            return "uvx"
        return None

    def fake_run(command: list[str], *, label: str = "command", timeout_seconds: int = 240):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="2026.10.02.010203\n",
            stderr="",
        )

    monkeypatch.setattr(yt_dlp.shutil, "which", fake_which)
    monkeypatch.setattr(yt_dlp, "_run_process", fake_run)

    provider = yt_dlp.YtDlpSourceProvider(
        channel="nightly",
        js_runtime="node",
    )
    info = provider.runtime_info
    command = provider.runtime.command(["--version"])

    assert info.version == "2026.10.02.010203"
    assert info.channel == "nightly"
    assert info.managed_by == "uvx"
    assert len(calls) == 1
    assert "--refresh-package" in calls[0]
    assert "yt-dlp[default]" in calls[0]
    assert "--prerelease" in calls[0]
    assert "allow" in calls[0]
    assert "--offline" in command
    assert "yt-dlp[default]" in command
    assert all("==" not in item for item in command)
    assert command[-3:] == ["--js-runtimes", "node", "--version"]


def test_download_failure_refreshes_runtime_and_retries_once(
    monkeypatch,
) -> None:
    attempts = {"probe": 0}
    refreshes = {"count": 0}

    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp",
        js_runtime="node",
        manage_updates=False,
    )

    monkeypatch.setattr(
        provider.runtime,
        "command",
        lambda args: ["yt-dlp", *args],
    )
    provider.runtime._command_prefix = ["yt-dlp"]
    provider.runtime._runtime_info = yt_dlp.YtDlpRuntimeInfo(
        version="2026.10.02",
        channel="nightly",
        js_runtime="node",
        managed_by="test",
    )

    def fake_refresh() -> None:
        refreshes["count"] += 1

    provider.runtime.manage_updates = True
    monkeypatch.setattr(provider.runtime, "refresh", fake_refresh)

    def fake_run(command: list[str], *, label: str = "command", timeout_seconds: int = 240):
        attempts["probe"] += 1
        if attempts["probe"] == 1:
            raise yt_dlp.YtDlpError("extractor broke")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "id": "video",
                    "title": "video",
                    "webpage_url": "https://www.youtube.com/watch?v=video",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(yt_dlp, "_run_process", fake_run)

    metadata = provider.probe("https://www.youtube.com/watch?v=video")

    assert metadata.external_id == "video"
    assert attempts["probe"] == 2
    assert refreshes["count"] == 1


def test_native_process_deadline_is_enforced_without_url_in_error():
    import sys

    # No network or external yt-dlp binary; real Python subprocess times out.
    with pytest.raises(yt_dlp.YtDlpTimeout) as exc:
        yt_dlp._run_process(
            [sys.executable, "-c", "import time; time.sleep(30)",
             "https://example.invalid/private-id"],
            label="bounded original video",
            timeout_seconds=1,
        )
    assert "bounded original video" in str(exc.value)
    assert "1s" in str(exc.value)
    assert "https://" not in str(exc.value)
    assert "cleanup=TREE_TERMINATION_REQUESTED" in str(exc.value)


def test_timeout_terminates_descendant_process_before_it_can_write(tmp_path):
    import sys
    import time

    marker = tmp_path / "orphan_wrote_after_timeout.txt"
    # Direct child launches a long-running grandchild with the same inherited
    # standard streams, precisely the case subprocess.run(timeout) misses.
    grandchild = (
        "import pathlib,sys,time;time.sleep(2.2);"
        "pathlib.Path(sys.argv[1]).write_text('orphan',encoding='utf-8');"
        "time.sleep(20)"
    )
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r},sys.argv[1]]);"
        "print('grandchild spawned',flush=True);"
        "time.sleep(20)"
    )
    with pytest.raises(yt_dlp.YtDlpTimeout) as exc:
        yt_dlp._run_process(
            [sys.executable, "-c", parent, str(marker)],
            label="bounded source child-tree", timeout_seconds=1,
        )
    assert "cleanup=TREE_TERMINATION_REQUESTED" in str(exc.value)
    # Wait beyond the grandchild write point. This is a real OS-process test
    # on both Ubuntu and Windows; it never downloads external media.
    time.sleep(2.0)
    assert not marker.exists(), "descendant survived the yt-dlp timeout"


def test_native_process_nonzero_exit_preserves_bounded_diagnostic():
    import sys

    with pytest.raises(yt_dlp.YtDlpError) as exc:
        yt_dlp._run_process([
            sys.executable, "-c",
            "import sys;sys.stderr.write('synthetic failure');sys.exit(7)",
        ], label="yt-dlp metadata probe", timeout_seconds=3)
    assert "exit code 7" in str(exc.value)
    assert "synthetic failure" in str(exc.value)


def test_runtime_timeout_does_not_retry_or_refresh(monkeypatch):
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
    )
    monkeypatch.setattr(provider.runtime, "command",
                        lambda args: ["yt-dlp", *args])
    provider.runtime.manage_updates = True
    called = []

    def would_refresh():
        called.append("refresh")
        raise AssertionError("refresh may not extend timed-out process")

    def timeout(_command, *, label="command", timeout_seconds=240):
        called.append(("run", label, timeout_seconds))
        raise yt_dlp.YtDlpTimeout("metadata extraction exceeded deadline")

    monkeypatch.setattr(provider.runtime, "refresh", would_refresh)
    monkeypatch.setattr(yt_dlp, "_run_process", timeout)
    with pytest.raises(yt_dlp.YtDlpTimeout):
        provider.probe("https://www.youtube.com/watch?v=AAAAAAAAAAA")
    assert called == [("run", "yt-dlp metadata probe", yt_dlp.METADATA_TIMEOUT_SECONDS)]


def test_bounded_video_and_bulk_audio_use_separate_deadlines(tmp_path, monkeypatch):
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
    )
    from holoclip.providers.source.base import SourceMetadata
    meta = SourceMetadata(
        provider="yt-dlp", url="https://www.youtube.com/watch?v=AAAAAAAAAAA",
        external_id="AAAAAAAAAAA", title="source", duration=999,
        availability="public", live_status="was_live",
    )
    monkeypatch.setattr(provider, "probe", lambda _url: meta)
    calls = []

    def timeout(_args, *, label, timeout_seconds=240, **kwargs):
        calls.append((label, timeout_seconds))
        raise yt_dlp.YtDlpTimeout("bounded test deadline")

    monkeypatch.setattr(provider, "_run_yt_dlp", timeout)
    with pytest.raises(yt_dlp.YtDlpTimeout):
        provider.acquire_video_range(meta.url, tmp_path / "range", start=10, end=22)
    with pytest.raises(yt_dlp.YtDlpTimeout):
        provider.acquire(meta.url, tmp_path / "bulk")
    assert calls == [
        ("yt-dlp bounded source video", yt_dlp.BOUNDED_VIDEO_TIMEOUT_SECONDS),
        ("yt-dlp acquisition", yt_dlp.MEDIA_ACQUISITION_TIMEOUT_SECONDS),
    ]


@pytest.mark.parametrize("limit", [0, -1, 3601, True, 2.5])
def test_native_deadline_budget_validation_fails_without_spawning(monkeypatch, limit):
    def forbidden(*args, **kwargs):
        raise AssertionError("bad timeout may not launch a process")

    monkeypatch.setattr(yt_dlp.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="deadline"):
        yt_dlp._run_process(["uvx"], timeout_seconds=limit)
