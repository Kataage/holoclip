from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from holoclip.alignment import align_clip_to_source
from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.config import GroqTranscriptionSettings, NimSettings
from holoclip.examples import (
    HumanClipExample,
    compute_human_clip_stats,
    load_human_clip_manifest,
    load_human_clip_store,
    make_human_clip_example_id,
    upsert_human_clip_examples,
)
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
from holoclip.learning import enrich_human_clip_example
from holoclip.media import play_media_segment
from holoclip.models import ReviewDecision
from holoclip.providers.llm.nvidia_nim import (
    NvidiaNimAuthorizationError,
    NvidiaNimClient,
    NvidiaNimTimeoutError,
)
from holoclip.transcript import chunk_segments, load_transcript, save_transcript
from holoclip.transcription import transcribe_media


app = typer.Typer(no_args_is_help=True)


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


async def _detect_candidates(
    segments: list,
    *,
    settings: NimSettings,
    top_k: int,
    window_minutes: float,
    overlap_minutes: float,
):
    chunks = chunk_segments(
        segments,
        window_seconds=window_minutes * 60.0,
        overlap_seconds=overlap_minutes * 60.0,
    )
    typer.echo(
        f"Transcript chunks: {len(chunks)} "
        f"(fast={settings.fast_model}, final={settings.model})"
    )
    async with NvidiaNimClient(settings, event_callback=typer.echo) as client:
        detector = CandidateDetector(
            client,
            fast_model=settings.fast_model,
            strong_model=settings.model,
            fast_reasoning_effort=settings.fast_reasoning_effort,
            strong_reasoning_effort=settings.strong_reasoning_effort,
            progress=typer.echo,
        )
        return await detector.run(chunks, top_k=top_k)


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

    async def _run() -> list[dict[str, object]]:
        candidates = await _detect_candidates(
            segments,
            settings=settings,
            top_k=top_k,
            window_minutes=window_minutes,
            overlap_minutes=overlap_minutes,
        )

        if save_run_path is not None:
            run = build_run(
                stream_id=stream_id or transcript.stem,
                transcript_path=str(transcript),
                candidates=candidates,
            )
            save_run(run, save_run_path)

        return [candidate.model_dump(mode="json") for candidate in candidates]

    try:
        result = asyncio.run(_run())
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        typer.echo(
            "The API key was loaded, but hosted inference access is not authorized.",
            err=True,
        )
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        typer.echo(
            "The transcript is already saved. Rerun the same analyze command to retry.",
            err=True,
        )
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"candidates": result}, ensure_ascii=False, indent=2))


@app.command()
def transcribe(
    media: Path = typer.Argument(..., exists=True, readable=True),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Create a timed Japanese transcript from local media with Groq Whisper."""
    _load_env_file()
    settings = GroqTranscriptionSettings.from_env()
    target = output or Path("data/transcripts") / f"{media.stem}.json"

    segments = asyncio.run(transcribe_media(media, settings))
    save_transcript(segments, target)
    typer.echo(f"saved transcript: {target}")
    typer.echo(f"segments: {len(segments)}")


@app.command()
def discover(
    media: Path = typer.Argument(..., exists=True, readable=True),
    stream_id: str | None = typer.Option(None),
    top_k: int = typer.Option(5, min=1, max=20),
    window_minutes: float = typer.Option(10.0, min=2.0, max=30.0),
    overlap_minutes: float = typer.Option(2.0, min=0.0, max=10.0),
    transcript_output: Path | None = typer.Option(None, "--transcript-output"),
    run_output: Path | None = typer.Option(None, "--run-output"),
    force_transcribe: bool = typer.Option(
        False,
        "--force-transcribe",
        help="Ignore an existing transcript and transcribe the media again.",
    ),
) -> None:
    """Transcribe local media, discover clip candidates, and save an evaluation run."""
    _load_env_file()
    nim_settings = NimSettings.from_env()
    groq_settings = GroqTranscriptionSettings.from_env()
    resolved_stream_id = stream_id or media.stem
    transcript_path = (
        transcript_output
        or Path("data/transcripts") / f"{resolved_stream_id}.json"
    )
    run_path = (
        run_output
        or Path("data/evaluation/runs") / f"{resolved_stream_id}.json"
    )

    async def _run() -> list[dict[str, object]]:
        if transcript_path.exists() and not force_transcribe:
            typer.echo(f"reusing transcript: {transcript_path}")
            segments = load_transcript(transcript_path)
        else:
            typer.echo(f"transcribing media: {media}")
            segments = await transcribe_media(media, groq_settings)
            save_transcript(segments, transcript_path)
            typer.echo(f"saved transcript: {transcript_path}")

        candidates = await _detect_candidates(
            segments,
            settings=nim_settings,
            top_k=top_k,
            window_minutes=window_minutes,
            overlap_minutes=overlap_minutes,
        )
        save_run(
            build_run(
                stream_id=resolved_stream_id,
                transcript_path=str(transcript_path),
                candidates=candidates,
                media_path=str(media.resolve()),
            ),
            run_path,
        )
        return [candidate.model_dump(mode="json") for candidate in candidates]

    try:
        result = asyncio.run(_run())
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        typer.echo(f"Transcript is safe at: {transcript_path}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        typer.echo(f"Transcript is safe at: {transcript_path}", err=True)
        typer.echo(
            "Rerun the same discover command; the saved transcript will be reused automatically.",
            err=True,
        )
        raise typer.Exit(3) from exc

    typer.echo(f"saved evaluation run: {run_path}")
    typer.echo(json.dumps({"candidates": result}, ensure_ascii=False, indent=2))


@app.command()
def doctor() -> None:
    """Check NVIDIA NIM model listing and per-model inference access."""
    _load_env_file()
    settings = NimSettings.from_env()

    async def _run() -> tuple[int, set[str], list[tuple[str, str, str | None]]]:
        async with NvidiaNimClient(settings) as client:
            models = await client.list_models()
            data = models.get("data", [])
            model_ids = {
                str(item.get("id"))
                for item in data
                if isinstance(item, dict) and item.get("id")
            }

            checks: list[tuple[str, str, str | None]] = []
            for label, model in (
                ("strong", settings.model),
                ("fast", settings.fast_model),
            ):
                try:
                    await client.chat(
                        [{"role": "user", "content": "Reply with OK only."}],
                        model=model,
                        temperature=0.0,
                        max_tokens=8,
                        reasoning_effort="low",
                    )
                except NvidiaNimAuthorizationError as exc:
                    checks.append((label, model, str(exc)))
                else:
                    checks.append((label, model, None))

            return len(model_ids), model_ids, checks

    try:
        model_count, model_ids, checks = asyncio.run(_run())
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM models: FAIL ({exc})")
        typer.echo(
            "The request failed before per-model inference checks. "
            "Verify that holoclip is loading the same NVIDIA_API_KEY as the working client."
        )
        raise typer.Exit(2) from exc

    typer.echo(f"NVIDIA NIM models: OK ({model_count} listed)")
    failed = False
    for label, model, error in checks:
        listed = "listed" if model in model_ids else "not listed"
        if error is None:
            typer.echo(f"{label} model {model}: OK ({listed})")
        else:
            failed = True
            typer.echo(f"{label} model {model}: FAIL ({listed}) - {error}")

    if failed:
        typer.echo(
            "If one configured model works and the other fails, use the working model for both "
            "NVIDIA_NIM_MODEL and NVIDIA_NIM_FAST_MODEL until the endpoint issue is resolved."
        )
        raise typer.Exit(2)

    typer.echo("NVIDIA NIM inference: OK")


@app.command()
def examples_align(
    clip_media: Path = typer.Argument(..., exists=True, readable=True),
    source_transcript: Path = typer.Argument(..., exists=True, readable=True),
    clip_url: str | None = typer.Option(None, "--clip-url"),
    clip_title: str | None = typer.Option(None, "--clip-title"),
    clip_channel: str | None = typer.Option(None, "--clip-channel"),
    source_url: str | None = typer.Option(None, "--source-url"),
    source_title: str | None = typer.Option(None, "--source-title"),
    stream_id: str | None = typer.Option(None, "--stream-id"),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    min_confidence: float = typer.Option(
        0.55,
        "--min-confidence",
        min=0.0,
        max=1.0,
    ),
    save: bool = typer.Option(
        False,
        "--save",
        help="Upsert the result into the human-clip example store.",
    ),
    force_transcribe: bool = typer.Option(
        False,
        "--force-transcribe",
    ),
) -> None:
    """Transcribe a human-made clip and align it to an original source transcript."""
    _load_env_file()
    groq_settings = GroqTranscriptionSettings.from_env()

    resolved_clip_url = clip_url or clip_media.resolve().as_uri()
    example_id = make_human_clip_example_id(resolved_clip_url)
    clip_transcript_path = Path("data/transcripts/examples") / f"{example_id}.json"

    if clip_transcript_path.exists() and not force_transcribe:
        typer.echo(f"reusing clip transcript: {clip_transcript_path}")
        clip_segments = load_transcript(clip_transcript_path)
    else:
        typer.echo(f"transcribing clip: {clip_media}")
        clip_segments = asyncio.run(transcribe_media(clip_media, groq_settings))
        save_transcript(clip_segments, clip_transcript_path)
        typer.echo(f"saved clip transcript: {clip_transcript_path}")

    source_segments = load_transcript(source_transcript)
    alignment = align_clip_to_source(clip_segments, source_segments)
    accepted = alignment.confidence >= min_confidence

    typer.echo(
        f"alignment: {_clock(alignment.source_start)} - {_clock(alignment.source_end)} "
        f"confidence={alignment.confidence:.3f} "
        f"anchors={alignment.matched_anchors}/{alignment.total_anchors} "
        f"segments={len(alignment.segments)}"
    )
    for index, segment in enumerate(alignment.segments, start=1):
        typer.echo(
            f"  segment {index}: clip {_clock(segment.clip_start)}-{_clock(segment.clip_end)} "
            f"-> source {_clock(segment.source_start)}-{_clock(segment.source_end)} "
            f"confidence={segment.confidence:.3f} anchors={segment.matched_anchors}"
        )

    for anchor in alignment.anchors[:5]:
        typer.echo(
            f"  clip {_clock(anchor.clip_start)} -> source {_clock(anchor.source_start)} "
            f"score={anchor.similarity:.3f}"
        )

    if not save:
        typer.echo("not saved (use --save after the alignment looks correct)")
        return

    if clip_url is None:
        typer.echo("--save requires --clip-url so training provenance is preserved.", err=True)
        raise typer.Exit(2)

    existing = next(
        (
            item
            for item in load_human_clip_store(store)
            if item.id == example_id
        ),
        None,
    )
    base = (
        existing.model_dump(mode="python")
        if existing is not None
        else {
            "id": example_id,
            "clip_url": clip_url,
            "clip_title": clip_title or clip_media.stem,
        }
    )
    excerpt = " ".join(segment.text for segment in clip_segments)[:4000]
    base.update(
        {
            "clip_url": clip_url,
            "clip_title": clip_title or base.get("clip_title") or clip_media.stem,
            "clip_channel": clip_channel or base.get("clip_channel"),
            "source_url": source_url or base.get("source_url"),
            "source_title": source_title or base.get("source_title"),
            "stream_id": stream_id or base.get("stream_id") or source_transcript.stem,
            "source_start": alignment.source_start if accepted else None,
            "source_end": alignment.source_end if accepted else None,
            "alignment_confidence": alignment.confidence,
            "alignment_method": "transcript-anchor-v2",
            "alignment_matched_anchors": alignment.matched_anchors,
            "alignment_segments": (
                [
                    {
                        "clip_start": segment.clip_start,
                        "clip_end": segment.clip_end,
                        "source_start": segment.source_start,
                        "source_end": segment.source_end,
                        "confidence": segment.confidence,
                        "matched_anchors": segment.matched_anchors,
                    }
                    for segment in alignment.segments
                ]
                if accepted
                else []
            ),
            "clip_transcript_path": str(clip_transcript_path),
            "source_transcript_path": str(source_transcript),
            "transcript_excerpt": excerpt,
        }
    )

    if not accepted:
        typer.echo(
            f"confidence is below {min_confidence:.2f}; storing as unaligned for later review."
        )

    example = HumanClipExample.model_validate(base)
    added, updated, total = upsert_human_clip_examples(store, [example])
    typer.echo(f"stored example: +{added} new, {updated} updated, {total} total")


@app.command()
def examples_enrich(
    example_id: str = typer.Argument(...),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    weak_negative_window: float = typer.Option(
        90.0,
        "--weak-negative-window",
        min=30.0,
        max=300.0,
    ),
) -> None:
    """Extract editorial structure and adjacent weak negatives for one aligned example."""
    _load_env_file()
    settings = NimSettings.from_env()
    examples = load_human_clip_store(store)
    example = next((item for item in examples if item.id == example_id), None)
    if example is None:
        typer.echo(f"example not found: {example_id}", err=True)
        raise typer.Exit(2)
    if example.source_start is None or example.source_end is None:
        typer.echo("example is not source-aligned yet", err=True)
        raise typer.Exit(2)
    if not example.source_transcript_path:
        typer.echo("example has no source_transcript_path", err=True)
        raise typer.Exit(2)

    source_transcript_path = Path(example.source_transcript_path)
    if not source_transcript_path.exists():
        typer.echo(f"source transcript not found: {source_transcript_path}", err=True)
        raise typer.Exit(2)
    source_segments = load_transcript(source_transcript_path)

    async def _run() -> HumanClipExample:
        async with NvidiaNimClient(settings, event_callback=typer.echo) as client:
            return await enrich_human_clip_example(
                client,
                example,
                source_segments,
                model=settings.model,
                reasoning_effort=settings.strong_reasoning_effort,
                weak_negative_window_seconds=weak_negative_window,
            )

    try:
        enriched = asyncio.run(_run())
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        raise typer.Exit(3) from exc

    _added, _updated, total = upsert_human_clip_examples(store, [enriched])
    typer.echo(f"enriched: {enriched.id} ({total} total)")
    typer.echo(f"setup: {enriched.setup}")
    typer.echo(f"development: {enriched.development}")
    typer.echo(f"payoff: {enriched.payoff}")
    typer.echo(f"why_selected: {enriched.why_selected}")
    typer.echo(f"traits: {', '.join(enriched.traits) if enriched.traits else '(none)'}")
    typer.echo(f"weak_negatives: {len(enriched.weak_negatives)}")


@app.command()
def examples_import(
    manifest: Path = typer.Argument(..., exists=True, readable=True),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
        help="JSONL store for structured human-made clip examples.",
    ),
) -> None:
    """Validate and upsert human-made clip examples into the local learning store."""
    examples = load_human_clip_manifest(manifest)
    added, updated, total = upsert_human_clip_examples(store, examples)
    typer.echo(f"human clip examples: +{added} new, {updated} updated, {total} total")
    typer.echo(f"store: {store}")


@app.command()
def examples_stats(
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
) -> None:
    """Summarize the current human-made clip weak-supervision store."""
    stats = compute_human_clip_stats(load_human_clip_store(store))
    typer.echo(json.dumps(stats.model_dump(mode="json"), ensure_ascii=False, indent=2))


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
    media: Path | None = typer.Option(
        None,
        "--media",
        exists=True,
        readable=True,
        help="Override the source media path used for candidate preview.",
    ),
    preview_padding: float = typer.Option(
        10.0,
        "--preview-padding",
        min=0.0,
        max=120.0,
        help="Seconds of context before and after a preview.",
    ),
    context_padding: float = typer.Option(
        20.0,
        "--context-padding",
        min=0.0,
        max=300.0,
        help="Seconds of transcript context before and after a candidate.",
    ),
) -> None:
    """Review one saved run with single-key-style choices."""
    run = load_run(run_file)
    latest = latest_feedback_index(load_feedback(feedback_file))
    transcript_path = Path(run.transcript_path)
    transcript_segments = (
        load_transcript(transcript_path) if transcript_path.exists() else []
    )
    preview_media = media or (Path(run.media_path) if run.media_path else None)

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
            choice = typer.prompt(
                "[p]再生 [c]前後字幕 [a]採用 [m]微妙 [r]ボツ [s]スキップ [q]終了"
            ).strip().lower()
            if choice == "p":
                if preview_media is None:
                    typer.echo(
                        "元動画パスがありません。--media path\\to\\video で指定してください。"
                    )
                    continue
                try:
                    play_media_segment(
                        preview_media,
                        start=candidate.start,
                        end=candidate.end,
                        padding_seconds=preview_padding,
                    )
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    typer.echo(f"プレビュー再生に失敗しました: {exc}")
                continue
            if choice == "c":
                if not transcript_segments:
                    typer.echo(f"字幕を読み込めません: {transcript_path}")
                    continue
                context_start = max(0.0, candidate.start - context_padding)
                context_end = candidate.end + context_padding
                typer.echo("")
                typer.echo(
                    f"--- 前後字幕 {_clock(context_start)} - {_clock(context_end)} ---"
                )
                shown = 0
                for segment in transcript_segments:
                    if segment.end <= context_start or segment.start >= context_end:
                        continue
                    typer.echo(f"[{_clock(segment.start)}] {segment.text}")
                    shown += 1
                if shown == 0:
                    typer.echo("(この範囲の字幕はありません)")
                typer.echo("---")
                continue
            if choice == "q":
                return
            if choice == "s":
                break
            decision = decision_map.get(choice)
            if decision is None:
                typer.echo("p / c / a / m / r / s / q のいずれかを入力してください。")
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
