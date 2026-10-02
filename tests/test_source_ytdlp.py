from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from holoclip.providers.source.base import AcquisitionProfile, SourceMetadata
from holoclip.providers.source import yt_dlp


def test_source_candidates_prefer_explicit_original_link() -> None:
    provider = yt_dlp.YtDlpSourceProvider(executable="yt-dlp")
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

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
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

    monkeypatch.setattr(yt_dlp, "_run", fake_run)
    provider = yt_dlp.YtDlpSourceProvider(executable="yt-dlp")

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
