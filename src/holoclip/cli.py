from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.config import NimSettings
from holoclip.providers.llm.nvidia_nim import NvidiaNimClient
from holoclip.transcript import chunk_segments, load_transcript


app = typer.Typer(no_args_is_help=True)


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@app.command()
def analyze(
    transcript: Path = typer.Argument(..., exists=True, readable=True),
    top_k: int = typer.Option(5, min=1, max=20),
    window_minutes: float = typer.Option(10.0, min=2.0, max=30.0),
    overlap_minutes: float = typer.Option(2.0, min=0.0, max=10.0),
) -> None:
    """Analyze a timed transcript JSON file and print clip candidates."""
    _load_env_file()
    settings = NimSettings.from_env()
    segments = load_transcript(transcript)
    chunks = chunk_segments(
        segments,
        window_seconds=window_minutes * 60.0,
        overlap_seconds=overlap_minutes * 60.0,
    )

    async def _run() -> list[dict[str, object]]:
        async with NvidiaNimClient(settings) as client:
            detector = CandidateDetector(
                client,
                fast_model=settings.fast_model,
                strong_model=settings.model,
            )
            candidates = await detector.run(chunks, top_k=top_k)
            return [candidate.model_dump(mode="json") for candidate in candidates]

    result = asyncio.run(_run())
    typer.echo(json.dumps({"candidates": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
