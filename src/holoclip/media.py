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
