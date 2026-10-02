from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioChunk:
    path: Path
    offset_seconds: float


def require_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg was not found on PATH")
    return executable


def require_ffplay() -> str:
    executable = shutil.which("ffplay")
    if executable is None:
        raise RuntimeError("ffplay was not found on PATH")
    return executable


def play_media_segment(
    media_path: Path,
    *,
    start: float,
    end: float,
    padding_seconds: float = 10.0,
) -> None:
    if not media_path.exists():
        raise FileNotFoundError(media_path)
    if end <= start:
        raise ValueError("preview end must be greater than start")

    ffplay = require_ffplay()
    preview_start = max(0.0, start - padding_seconds)
    preview_end = end + padding_seconds
    duration = preview_end - preview_start
    command = [
        ffplay,
        "-hide_banner",
        "-loglevel",
        "error",
        "-autoexit",
        "-ss",
        f"{preview_start:.3f}",
        "-t",
        f"{duration:.3f}",
        str(media_path),
    ]
    subprocess.run(command, check=False)


def split_media_audio(
    media_path: Path,
    output_dir: Path,
    *,
    chunk_seconds: int = 900,
) -> list[AudioChunk]:
    if chunk_seconds < 60:
        raise ValueError("chunk_seconds must be >= 60")
    if not media_path.exists():
        raise FileNotFoundError(media_path)

    ffmpeg = require_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "chunk-%04d.mp3"

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "32k",
        "-map_metadata",
        "-1",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    subprocess.run(command, check=True)

    paths = sorted(output_dir.glob("chunk-*.mp3"))
    if not paths:
        raise RuntimeError("ffmpeg completed without producing audio chunks")

    return [
        AudioChunk(path=path, offset_seconds=float(index * chunk_seconds))
        for index, path in enumerate(paths)
    ]
