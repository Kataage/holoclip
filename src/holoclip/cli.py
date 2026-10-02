from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.config import NimSettings
from holoclip.evaluation import (
    FeedbackRecord,
    append_feedback,
    build_run,
    compute_metrics,
    latest_feedback_index,
    load_feedback,
    load_known_clips,
    load_run,
    load_runs,
    save_run,
)
from holoclip.models import ReviewDecision
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
    stream_id: str | None = typer.Option(None),
    save_run_path: Path | None = typer.Option(
        None,
        "--save-run",
        help="Save an evaluation run JSON in addition to printing candidates.",
    ),
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

            if save_run_path is not None:
                run = build_run(
                    stream_id=stream_id or transcript.stem,
                    transcript_path=str(transcript),
                    candidates=candidates,
                )
                save_run(run, save_run_path)

            return [candidate.model_dump(mode="json") for candidate in candidates]

    result = asyncio.run(_run())
    typer.echo(json.dumps({"candidates": result}, ensure_ascii=False, indent=2))


@app.command()
def label(
    stream_id: str = typer.Argument(...),
    candidate_id: str = typer.Argument(...),
    decision: ReviewDecision = typer.Argument(...),
    reason: str | None = typer.Option(None),
    feedback_file: Path = typer.Option(
        Path("data/evaluation/feedback.jsonl"),
        "--feedback-file",
    ),
) -> None:
    """Append one accept/maybe/reject judgment to the evaluation log."""
    append_feedback(
        FeedbackRecord(
            stream_id=stream_id,
            candidate_id=candidate_id,
            decision=decision,
            reason=reason,
        ),
        feedback_file,
    )
    typer.echo(f"saved {decision.value}: {stream_id}/{candidate_id}")


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@app.command()
def review(
    run_file: Path = typer.Argument(..., exists=True, readable=True),
    feedback_file: Path = typer.Option(
        Path("data/evaluation/feedback.jsonl"),
        "--feedback-file",
    ),
    include_labeled: bool = typer.Option(
        False,
        "--include-labeled",
        help="Show candidates that already have feedback.",
    ),
) -> None:
    """Review one saved run with single-key-style choices."""
    run = load_run(run_file)
    latest = latest_feedback_index(load_feedback(feedback_file))

    decision_map = {
        "a": ReviewDecision.ACCEPT,
        "m": ReviewDecision.MAYBE,
        "r": ReviewDecision.REJECT,
    }

    pending = 0
    for item in run.candidates:
        existing = latest.get((run.stream_id, item.id))
        if existing is not None and not include_labeled:
            continue

        pending += 1
        candidate = item.candidate
        typer.echo("")
        typer.echo(f"#{item.rank}  {item.id}")
        typer.echo(f"{_clock(candidate.start)} - {_clock(candidate.end)}")
        typer.echo(candidate.working_title)
        typer.echo(f"理由: {candidate.why_interesting}")
        typer.echo(f"前振り: {candidate.setup}")
        typer.echo(f"展開: {candidate.development}")
        typer.echo(f"オチ: {candidate.payoff}")
        if candidate.warnings:
            typer.echo("注意: " + ", ".join(value.value for value in candidate.warnings))
        if existing is not None:
            typer.echo(f"現在の評価: {existing.decision.value}")

        while True:
            choice = typer.prompt("[a]採用 [m]微妙 [r]ボツ [s]スキップ [q]終了").strip().lower()
            if choice == "q":
                return
            if choice == "s":
                break
            decision = decision_map.get(choice)
            if decision is None:
                typer.echo("a / m / r / s / q のいずれかを入力してください。")
                continue

            append_feedback(
                FeedbackRecord(
                    stream_id=run.stream_id,
                    candidate_id=item.id,
                    decision=decision,
                ),
                feedback_file,
            )
            typer.echo(f"saved: {decision.value}")
            break

    if pending == 0:
        typer.echo("未評価の候補はありません。")


@app.command()
def metrics(
    runs_dir: Path = typer.Option(
        Path("data/evaluation/runs"),
        "--runs-dir",
    ),
    feedback_file: Path = typer.Option(
        Path("data/evaluation/feedback.jsonl"),
        "--feedback-file",
    ),
    known_clips_file: Path | None = typer.Option(
        None,
        "--known-clips",
    ),
) -> None:
    """Compute Top-K acceptance and known-clip recall metrics."""
    result = compute_metrics(
        load_runs(runs_dir),
        load_feedback(feedback_file),
        load_known_clips(known_clips_file),
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
