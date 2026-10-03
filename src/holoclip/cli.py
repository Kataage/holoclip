from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from holoclip.alignment import ALIGNMENT_METHOD, align_clip_to_source
from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.clip_policy import (
    ClipPolicyRule,
    PolicyDecision,
    PolicyOperation,
    append_policy_rule,
    evaluate_source_metadata,
    load_policy_rules,
)
from holoclip.config import GroqTranscriptionSettings, NimEmbeddingSettings, NimSettings
from holoclip.example_batch import (
    BatchStatus,
    load_batch_manifest,
    load_batch_state,
    save_batch_state,
    should_skip_succeeded,
    summarize_batch_state,
    update_batch_item_state,
)
from holoclip.example_audit import (
    build_human_example_audit,
    find_human_example,
    format_human_example_audit,
)
from holoclip.example_quality import evaluate_human_example_learning_quality
from holoclip.example_revalidation import realign_human_clip_example
from holoclip.examples import (
    HumanClipExample,
    compute_human_clip_stats,
    load_human_clip_manifest,
    load_human_clip_store,
    make_human_clip_example_id,
    upsert_human_clip_examples,
)
from holoclip.execution import StageArtifactCache
from holoclip.example_ingest import (
    ClipPolicyGateError,
    SourceResolutionError,
    ingest_human_example_from_urls,
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
from holoclip.learning import (
    enrich_human_clip_example,
    refresh_human_clip_weak_negatives,
)
from holoclip.media import play_media_segment
from holoclip.migration import migrate_legacy_data, report_as_json
from holoclip.models import ReviewDecision
from holoclip.providers.embedding.nvidia_nim import (
    NvidiaEmbeddingAuthorizationError,
    NvidiaEmbeddingRateLimitDeferred,
    NvidiaNimEmbeddingClient,
)
from holoclip.providers.source.yt_dlp import YtDlpError, YtDlpSourceProvider
from holoclip.providers.llm.nvidia_nim import (
    NvidiaNimAuthorizationError,
    NvidiaNimClient,
    NvidiaNimTimeoutError,
)
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import index_human_examples, retrieve_structural_examples
from holoclip.transcript import chunk_segments, load_transcript, save_transcript
from holoclip.structure_validation import validate_editorial_structure
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
    stage_cache: StageArtifactCache | None = None,
    retrieval_repository: SQLiteCanonicalRepository | None = None,
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
        if retrieval_repository is None:
            detector = CandidateDetector(
                client,
                fast_model=settings.fast_model,
                strong_model=settings.model,
                fast_reasoning_effort=settings.fast_reasoning_effort,
                strong_reasoning_effort=settings.strong_reasoning_effort,
                progress=typer.echo,
                stage_cache=stage_cache,
                cache_provider="nvidia_nim",
            )
            return await detector.run(chunks, top_k=top_k)

        embedding_settings = NimEmbeddingSettings.from_env()
        async with NvidiaNimEmbeddingClient(embedding_settings) as embedding_client:
            detector = CandidateDetector(
                client,
                fast_model=settings.fast_model,
                strong_model=settings.model,
                fast_reasoning_effort=settings.fast_reasoning_effort,
                strong_reasoning_effort=settings.strong_reasoning_effort,
                progress=typer.echo,
                stage_cache=stage_cache,
                cache_provider="nvidia_nim",
                embedding_provider=embedding_client,
                retrieval_repository=retrieval_repository,
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

    async def _run(
        stage_cache: StageArtifactCache,
        repository: SQLiteCanonicalRepository,
    ) -> list[dict[str, object]]:
        candidates = await _detect_candidates(
            segments,
            settings=settings,
            top_k=top_k,
            window_minutes=window_minutes,
            overlap_minutes=overlap_minutes,
            stage_cache=stage_cache,
            retrieval_repository=repository,
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
        with SQLiteCanonicalRepository(Path("data/holoclip.db")) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            result = asyncio.run(_run(stage_cache, repository))
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

    with SQLiteCanonicalRepository(Path("data/holoclip.db")) as repository:
        stage_cache = StageArtifactCache(
            repository,
            Path("data/artifacts/stages"),
        )
        segments = asyncio.run(
            transcribe_media(
                media,
                settings,
                stage_cache=stage_cache,
                progress=typer.echo,
            )
        )
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

    async def _run(
        stage_cache: StageArtifactCache,
        repository: SQLiteCanonicalRepository,
    ) -> list[dict[str, object]]:
        if transcript_path.exists() and not force_transcribe:
            typer.echo(f"reusing transcript: {transcript_path}")
            segments = load_transcript(transcript_path)
        else:
            typer.echo(f"transcribing media: {media}")
            segments = await transcribe_media(
                media,
                groq_settings,
                stage_cache=stage_cache,
                progress=typer.echo,
            )
            save_transcript(segments, transcript_path)
            typer.echo(f"saved transcript: {transcript_path}")

        candidates = await _detect_candidates(
            segments,
            settings=nim_settings,
            top_k=top_k,
            window_minutes=window_minutes,
            overlap_minutes=overlap_minutes,
            stage_cache=stage_cache,
            retrieval_repository=repository,
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
        with SQLiteCanonicalRepository(Path("data/holoclip.db")) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            result = asyncio.run(_run(stage_cache, repository))
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


def _refresh_example_index(
    examples: list[HumanClipExample],
    *,
    database: Path = Path("data/holoclip.db"),
) -> None:
    if not examples:
        return
    settings = NimEmbeddingSettings.from_env()

    async def _run():
        with SQLiteCanonicalRepository(database) as repository:
            async with NvidiaNimEmbeddingClient(settings) as provider:
                return await index_human_examples(provider, repository, examples)

    try:
        stats = asyncio.run(_run())
    except (
        NvidiaEmbeddingAuthorizationError,
        NvidiaEmbeddingRateLimitDeferred,
        ValueError,
    ) as exc:
        typer.echo(
            f"embedding index update skipped/failed: {exc}; "
            "the enriched example was already saved",
            err=True,
        )
        return

    typer.echo(
        "retrieval index: "
        f"{stats.embedded_documents} embedded, "
        f"{stats.reused_documents} reused, "
        f"{stats.removed_documents} removed"
    )


@app.command()
def policy_check(
    source_url: str = typer.Argument(...),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"),
        "--policy-file",
    ),
) -> None:
    """Check source-level clip restrictions before acquisition or discovery."""
    _load_env_file()
    try:
        rules = load_policy_rules(policy_file)
        provider = YtDlpSourceProvider(
            channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"),
        )
        metadata = provider.probe(source_url)
        evaluation = evaluate_source_metadata(
            metadata,
            rules=rules,
            operation=PolicyOperation.PUBLISH,
        )
    except (RuntimeError, ValueError, YtDlpError) as exc:
        typer.echo(f"policy check failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(
        json.dumps(
            {
                "source_id": metadata.external_id,
                "title": metadata.title,
                "channel": metadata.channel,
                "channel_id": metadata.channel_id,
                "availability": metadata.availability,
                "live_status": metadata.live_status,
                "policy": evaluation.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if evaluation.decision is PolicyDecision.BLOCK:
        raise typer.Exit(5)
    if evaluation.decision is PolicyDecision.REVIEW:
        raise typer.Exit(4)


@app.command()
def policy_add(
    rule_id: str = typer.Option(..., "--id"),
    decision: PolicyDecision = typer.Option(..., "--decision"),
    reason: str = typer.Option(..., "--reason"),
    source_id: str | None = typer.Option(None, "--source-id"),
    channel_id: str | None = typer.Option(None, "--channel-id"),
    channel_name: str | None = typer.Option(None, "--channel-name"),
    start: float | None = typer.Option(None, "--start", min=0.0),
    end: float | None = typer.Option(None, "--end", min=0.001),
    evidence_url: str | None = typer.Option(None, "--evidence-url"),
    evidence_text: str | None = typer.Option(None, "--evidence-text"),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"),
        "--policy-file",
    ),
) -> None:
    """Append a sourced source/channel/range restriction to the local policy registry."""
    try:
        rule = ClipPolicyRule(
            id=rule_id,
            decision=decision,
            reason=reason,
            source_id=source_id,
            channel_id=channel_id,
            channel_name=channel_name,
            start=start,
            end=end,
            evidence_url=evidence_url,
            evidence_text=evidence_text,
        )
        append_policy_rule(policy_file, rule)
    except ValueError as exc:
        typer.echo(f"invalid policy rule: {exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(f"saved clip policy rule: {rule.id}")
    typer.echo(f"policy file: {policy_file}")


@app.command()
def source_doctor() -> None:
    """Check the managed yt-dlp runtime and JavaScript runtime."""
    _load_env_file()
    try:
        provider = YtDlpSourceProvider(
            channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"),
        )
        info = provider.runtime_info
    except (RuntimeError, YtDlpError) as exc:
        typer.echo(f"source runtime: FAIL ({exc})", err=True)
        raise typer.Exit(2) from exc

    typer.echo("source runtime: OK")
    typer.echo(f"yt-dlp: {info.version}")
    typer.echo(f"channel: {info.channel}")
    typer.echo(f"managed_by: {info.managed_by}")
    typer.echo(f"js_runtime: {info.js_runtime}")


@app.command()
def examples_ingest_url(
    clip_url: str = typer.Argument(...),
    source_url: str | None = typer.Option(
        None,
        "--source-url",
        help=(
            "Original source stream URL. When omitted, Holoclip tries to resolve "
            "one unambiguous source link from the clip description."
        ),
    ),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    download_root: Path = typer.Option(
        Path("data/sources/examples"),
        "--download-root",
    ),
    transcript_root: Path = typer.Option(
        Path("data/transcripts"),
        "--transcript-root",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    min_confidence: float = typer.Option(
        0.55,
        "--min-confidence",
        min=0.0,
        max=1.0,
    ),
    weak_negative_window: float = typer.Option(
        90.0,
        "--weak-negative-window",
        min=30.0,
        max=300.0,
    ),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"),
        "--policy-file",
    ),
    allow_policy_review: bool = typer.Option(
        False,
        "--allow-policy-review",
        help=(
            "Proceed through REVIEW findings after manual inspection. "
            "BLOCK findings can never be overridden."
        ),
    ),
) -> None:
    """Download, align, enrich and index one human-made clip from URLs."""
    _load_env_file()

    groq_settings = GroqTranscriptionSettings.from_env()
    nim_settings = NimSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()
    try:
        policy_rules = load_policy_rules(policy_file)
    except ValueError as exc:
        typer.echo(f"clip policy error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        source_provider = YtDlpSourceProvider(
            channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"),
        )
    except RuntimeError as exc:
        typer.echo(f"source provider error: {exc}", err=True)
        raise typer.Exit(2) from exc

    async def _run():
        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            async with (
                NvidiaNimClient(
                    nim_settings,
                    event_callback=typer.echo,
                ) as llm,
                NvidiaNimEmbeddingClient(embedding_settings) as embedding,
            ):
                return await ingest_human_example_from_urls(
                    source_provider,
                    clip_url=clip_url,
                    source_url=source_url,
                    download_root=download_root,
                    transcript_root=transcript_root,
                    examples_store=store,
                    groq_settings=groq_settings,
                    llm=llm,
                    llm_model=nim_settings.model,
                    llm_reasoning_effort=nim_settings.strong_reasoning_effort,
                    embedding_provider=embedding,
                    repository=repository,
                    stage_cache=stage_cache,
                    min_confidence=min_confidence,
                    weak_negative_window_seconds=weak_negative_window,
                    policy_rules=policy_rules,
                    allow_policy_review=allow_policy_review,
                    progress=typer.echo,
                )

    try:
        result = asyncio.run(_run())
    except ClipPolicyGateError as exc:
        typer.echo(
            f"clip policy: {exc.evaluation.decision.value} ({exc.stage})",
            err=True,
        )
        for finding in exc.evaluation.findings:
            scope = ""
            if finding.start is not None and finding.end is not None:
                scope = f" [{_clock(finding.start)}-{_clock(finding.end)}]"
            typer.echo(
                f"  - {finding.source}{scope}: {finding.reason}",
                err=True,
            )
            if finding.evidence_url:
                typer.echo(f"    evidence: {finding.evidence_url}", err=True)
        if exc.evaluation.decision is PolicyDecision.REVIEW:
            typer.echo(
                "Review the evidence/range manually. Re-run with "
                "--allow-policy-review only if it is confirmed safe.",
                err=True,
            )
        raise typer.Exit(5) from exc
    except SourceResolutionError as exc:
        typer.echo(
            "元配信を一意に決められませんでした。"
            "候補を確認して --source-url を指定してください。",
            err=True,
        )
        if not exc.candidates:
            typer.echo("(概要欄から元配信候補を検出できませんでした)", err=True)
        for index, candidate in enumerate(exc.candidates, start=1):
            typer.echo(
                f"  {index}. score={candidate.score:.2f} "
                f"{candidate.url} [{candidate.reason}]",
                err=True,
            )
            if candidate.context:
                typer.echo(f"     {candidate.context}", err=True)
        raise typer.Exit(4) from exc
    except YtDlpError as exc:
        typer.echo(f"yt-dlp error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        typer.echo(
            "Rerun the same command. Media/transcripts are reusable, and a failed "
            "enrichment attempt will not overwrite the last good human example.",
            err=True,
        )
        raise typer.Exit(3) from exc
    except NvidiaEmbeddingAuthorizationError as exc:
        typer.echo(f"NVIDIA embedding authorization error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaEmbeddingRateLimitDeferred as exc:
        typer.echo(f"NVIDIA embedding quota/rate limit: {exc}", err=True)
        raise typer.Exit(3) from exc

    typer.echo("")
    typer.echo("human clip ingestion complete")
    typer.echo(f"example_id: {result.example_id}")
    typer.echo(f"clip: {result.clip_url}")
    typer.echo(f"source: {result.source_url}")
    typer.echo(
        "source resolution: "
        + ("auto" if result.source_auto_detected else "explicit")
    )
    typer.echo(
        f"alignment: confidence={result.alignment_confidence:.3f} "
        f"segments={result.alignment_segments} "
        f"accepted={'yes' if result.aligned else 'no'}"
    )
    typer.echo(f"enriched: {'yes' if result.enriched else 'no'}")
    typer.echo(f"clip media: {result.clip_media_path}")
    typer.echo(f"source media: {result.source_media_path}")
    typer.echo(f"clip transcript: {result.clip_transcript_path}")
    typer.echo(f"source transcript: {result.source_transcript_path}")
    if result.acquisition_provider_version:
        typer.echo(
            "acquisition runtime: "
            f"{result.acquisition_provider} "
            f"{result.acquisition_provider_version} "
            f"channel={result.acquisition_provider_channel or 'unknown'} "
            f"js={result.acquisition_js_runtime or 'unknown'}"
        )


@app.command()
def examples_ingest_batch(
    manifest: Path = typer.Argument(..., exists=True, readable=True),
    state_path: Path | None = typer.Option(None, "--state"),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    download_root: Path = typer.Option(
        Path("data/sources/examples"),
        "--download-root",
    ),
    transcript_root: Path = typer.Option(
        Path("data/transcripts"),
        "--transcript-root",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"),
        "--policy-file",
    ),
    min_confidence: float = typer.Option(
        0.55,
        "--min-confidence",
        min=0.0,
        max=1.0,
    ),
    weak_negative_window: float = typer.Option(
        90.0,
        "--weak-negative-window",
        min=30.0,
        max=300.0,
    ),
    allow_policy_review: bool = typer.Option(
        False,
        "--allow-policy-review",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reprocess items that already succeeded with the same inputs.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero when the batch ends with unresolved items.",
    ),
) -> None:
    """Sequentially ingest a resumable JSONL batch of human-made clips."""
    _load_env_file()
    resolved_state_path = (
        state_path
        if state_path is not None
        else manifest.with_suffix(manifest.suffix + ".state.json")
    )

    try:
        items = load_batch_manifest(manifest)
        batch_state = load_batch_state(
            resolved_state_path,
            manifest_path=manifest,
        )
        policy_rules = load_policy_rules(policy_file)
    except ValueError as exc:
        typer.echo(f"batch configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if not items:
        typer.echo("batch manifest has no enabled/input rows")
        return

    groq_settings = GroqTranscriptionSettings.from_env()
    nim_settings = NimSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()

    try:
        source_provider = YtDlpSourceProvider(
            channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"),
        )
    except RuntimeError as exc:
        typer.echo(f"source provider error: {exc}", err=True)
        raise typer.Exit(2) from exc

    async def _run_batch() -> tuple[int, int]:
        processed = 0
        skipped_existing_success = 0

        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            async with (
                NvidiaNimClient(
                    nim_settings,
                    event_callback=typer.echo,
                ) as llm,
                NvidiaNimEmbeddingClient(embedding_settings) as embedding,
            ):
                for index, item in enumerate(items, start=1):
                    prefix = f"[{index}/{len(items)}]"

                    if not item.enabled:
                        typer.echo(f"{prefix} disabled: {item.clip_url}")
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=BatchStatus.SKIPPED,
                            details={"reason": "disabled"},
                        )
                        save_batch_state(resolved_state_path, batch_state)
                        continue

                    if should_skip_succeeded(
                        item,
                        batch_state,
                        force=force,
                    ):
                        skipped_existing_success += 1
                        typer.echo(f"{prefix} already succeeded; skipped")
                        continue

                    processed += 1
                    typer.echo("")
                    typer.echo(f"{prefix} ingesting {item.clip_url}")
                    if item.source_url:
                        typer.echo(f"{prefix} source {item.source_url}")

                    try:
                        result = await ingest_human_example_from_urls(
                            source_provider,
                            clip_url=item.clip_url,
                            source_url=item.source_url,
                            download_root=download_root,
                            transcript_root=transcript_root,
                            examples_store=store,
                            groq_settings=groq_settings,
                            llm=llm,
                            llm_model=nim_settings.model,
                            llm_reasoning_effort=nim_settings.strong_reasoning_effort,
                            embedding_provider=embedding,
                            repository=repository,
                            stage_cache=stage_cache,
                            min_confidence=min_confidence,
                            weak_negative_window_seconds=weak_negative_window,
                            policy_rules=policy_rules,
                            allow_policy_review=allow_policy_review,
                            progress=lambda message, p=prefix: typer.echo(
                                f"{p} {message}"
                            ),
                        )
                    except SourceResolutionError as exc:
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=BatchStatus.NEEDS_SOURCE,
                            last_error=str(exc),
                            details={
                                "candidates": [
                                    candidate.model_dump(mode="json")
                                    for candidate in exc.candidates
                                ]
                            },
                        )
                        typer.echo(
                            f"{prefix} NEEDS_SOURCE: {len(exc.candidates)} candidate(s)"
                        )
                    except ClipPolicyGateError as exc:
                        status = (
                            BatchStatus.POLICY_BLOCKED
                            if exc.evaluation.decision is PolicyDecision.BLOCK
                            else BatchStatus.POLICY_REVIEW
                        )
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=status,
                            last_error=str(exc),
                            details={
                                "stage": exc.stage,
                                "policy": exc.evaluation.model_dump(mode="json"),
                            },
                        )
                        typer.echo(
                            f"{prefix} {status.value.upper()}: "
                            f"{exc.evaluation.decision.value} at {exc.stage}"
                        )
                    except (
                        NvidiaNimTimeoutError,
                        NvidiaEmbeddingRateLimitDeferred,
                        YtDlpError,
                    ) as exc:
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=BatchStatus.RETRYABLE,
                            last_error=str(exc),
                            details={"error_class": type(exc).__name__},
                        )
                        typer.echo(
                            f"{prefix} RETRYABLE: {type(exc).__name__}: {exc}"
                        )
                    except (
                        NvidiaNimAuthorizationError,
                        NvidiaEmbeddingAuthorizationError,
                    ) as exc:
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=BatchStatus.FAILED,
                            last_error=str(exc),
                            details={"error_class": type(exc).__name__},
                        )
                        save_batch_state(resolved_state_path, batch_state)
                        typer.echo(
                            f"{prefix} FAILED: authorization/configuration error: {exc}"
                        )
                        break
                    except Exception as exc:
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=BatchStatus.FAILED,
                            last_error=str(exc),
                            details={"error_class": type(exc).__name__},
                        )
                        typer.echo(
                            f"{prefix} FAILED: {type(exc).__name__}: {exc}"
                        )
                    else:
                        status = (
                            BatchStatus.SUCCEEDED
                            if result.aligned and result.enriched
                            else BatchStatus.ALIGNMENT_REJECTED
                        )
                        update_batch_item_state(
                            batch_state,
                            item,
                            status=status,
                            example_id=result.example_id,
                            details={
                                "alignment_confidence": result.alignment_confidence,
                                "alignment_segments": result.alignment_segments,
                                "aligned": result.aligned,
                                "enriched": result.enriched,
                            },
                        )
                        typer.echo(
                            f"{prefix} {status.value.upper()}: "
                            f"confidence={result.alignment_confidence:.3f} "
                            f"segments={result.alignment_segments}"
                        )

                    save_batch_state(resolved_state_path, batch_state)

        return processed, skipped_existing_success

    processed, skipped_existing_success = asyncio.run(_run_batch())
    summary = summarize_batch_state(
        items,
        batch_state,
        processed=processed,
        skipped_existing_success=skipped_existing_success,
        state_path=resolved_state_path,
    )
    typer.echo("")
    typer.echo(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))

    unresolved = sum(
        count
        for status, count in summary.counts.items()
        if status not in {
            BatchStatus.SUCCEEDED.value,
            BatchStatus.SKIPPED.value,
        }
    )
    if strict and unresolved:
        raise typer.Exit(6)


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
        with SQLiteCanonicalRepository(Path("data/holoclip.db")) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            clip_segments = asyncio.run(
                transcribe_media(
                    clip_media,
                    groq_settings,
                    stage_cache=stage_cache,
                    progress=typer.echo,
                )
            )
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
            "alignment_method": ALIGNMENT_METHOD,
            "alignment_matched_anchors": alignment.matched_anchors,
            "alignment_total_anchors": alignment.total_anchors,
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
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
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
        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            async with NvidiaNimClient(
                settings,
                event_callback=typer.echo,
            ) as client:
                return await enrich_human_clip_example(
                    client,
                    example,
                    source_segments,
                    model=settings.model,
                    reasoning_effort=settings.strong_reasoning_effort,
                    weak_negative_window_seconds=weak_negative_window,
                    stage_cache=stage_cache,
                    cache_provider="nvidia_nim",
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
    _refresh_example_index(
        load_human_clip_store(store),
        database=database,
    )
    typer.echo(f"setup: {enriched.setup}")
    typer.echo(f"development: {enriched.development}")
    typer.echo(f"payoff: {enriched.payoff}")
    typer.echo(f"why_selected: {enriched.why_selected}")
    typer.echo(f"traits: {', '.join(enriched.traits) if enriched.traits else '(none)'}")
    typer.echo(f"weak_negatives: {len(enriched.weak_negatives)}")


@app.command()
def examples_revalidate(
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    min_confidence: float = typer.Option(
        0.55,
        "--min-confidence",
        min=0.0,
        max=1.0,
    ),
    weak_negative_window: float = typer.Option(
        90.0,
        "--weak-negative-window",
        min=30.0,
        max=300.0,
    ),
) -> None:
    """Re-align, repair and re-index the human-example corpus from cached transcripts."""
    _load_env_file()
    examples = load_human_clip_store(store)
    if not examples:
        typer.echo(f"no human clip examples found: {store}")
        return

    nim_settings = NimSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()

    async def _run() -> dict[str, object]:
        updated = 0
        alignment_changed = 0
        alignment_rejected = 0
        llm_reenriched = 0
        weak_negative_refreshed = 0
        retryable_failures = 0
        missing_transcripts = 0

        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            async with NvidiaNimClient(
                nim_settings,
                event_callback=typer.echo,
            ) as llm:
                for index, example in enumerate(examples, start=1):
                    prefix = f"[{index}/{len(examples)}]"
                    typer.echo("")
                    typer.echo(f"{prefix} revalidating {example.id}")

                    if not example.clip_transcript_path or not example.source_transcript_path:
                        missing_transcripts += 1
                        typer.echo(f"{prefix} skipped: transcript path missing")
                        continue

                    clip_path = Path(example.clip_transcript_path)
                    source_path = Path(example.source_transcript_path)
                    if not clip_path.exists() or not source_path.exists():
                        missing_transcripts += 1
                        typer.echo(
                            f"{prefix} skipped: cached transcript file missing"
                        )
                        continue

                    try:
                        clip_segments = load_transcript(clip_path)
                        source_segments = load_transcript(source_path)
                        realigned = realign_human_clip_example(
                            example,
                            clip_segments,
                            source_segments,
                            min_confidence=min_confidence,
                        )
                    except (OSError, TypeError, ValueError) as exc:
                        retryable_failures += 1
                        typer.echo(
                            f"{prefix} failed to load/re-align cached transcripts: {exc}"
                        )
                        continue

                    candidate = realigned.example
                    if realigned.alignment_changed:
                        alignment_changed += 1

                    if not realigned.accepted:
                        alignment_rejected += 1
                        upsert_human_clip_examples(store, [candidate])
                        updated += 1
                        typer.echo(
                            f"{prefix} rejected: "
                            f"confidence={realigned.alignment_confidence:.3f}"
                        )
                        continue

                    structure_warnings = validate_editorial_structure(
                        candidate
                    ).warnings
                    needs_llm = (
                        realigned.alignment_changed
                        or bool(structure_warnings)
                        or not candidate.structure_method
                    )

                    if needs_llm:
                        try:
                            candidate = await enrich_human_clip_example(
                                llm,
                                candidate,
                                source_segments,
                                model=nim_settings.model,
                                reasoning_effort=nim_settings.strong_reasoning_effort,
                                weak_negative_window_seconds=weak_negative_window,
                                stage_cache=stage_cache,
                                cache_provider="nvidia_nim",
                            )
                        except NvidiaNimTimeoutError as exc:
                            retryable_failures += 1
                            typer.echo(
                                f"{prefix} retryable NIM timeout; "
                                f"last good example preserved: {exc}"
                            )
                            continue
                        llm_reenriched += 1
                    else:
                        candidate = refresh_human_clip_weak_negatives(
                            candidate,
                            source_segments,
                            weak_negative_window_seconds=weak_negative_window,
                        )
                        weak_negative_refreshed += 1

                    upsert_human_clip_examples(store, [candidate])
                    updated += 1
                    quality = evaluate_human_example_learning_quality(candidate)
                    reason_text = (
                        ",".join(reason.value for reason in quality.reasons)
                        if quality.reasons
                        else "-"
                    )
                    typer.echo(
                        f"{prefix} {quality.decision.value}: "
                        f"confidence={realigned.alignment_confidence:.3f} "
                        f"segments={realigned.alignment_segments} "
                        f"reasons={reason_text}"
                    )

            final_examples = load_human_clip_store(store)
            async with NvidiaNimEmbeddingClient(
                embedding_settings
            ) as embedding:
                index_stats = await index_human_examples(
                    embedding,
                    repository,
                    final_examples,
                )

        final_stats = compute_human_clip_stats(load_human_clip_store(store))
        return {
            "total": len(examples),
            "updated": updated,
            "alignment_changed": alignment_changed,
            "alignment_rejected": alignment_rejected,
            "llm_reenriched": llm_reenriched,
            "weak_negative_refreshed": weak_negative_refreshed,
            "retryable_failures": retryable_failures,
            "missing_transcripts": missing_transcripts,
            "stats": final_stats.model_dump(mode="json"),
            "retrieval_index": index_stats.model_dump(mode="json"),
        }

    try:
        summary = asyncio.run(_run())
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaEmbeddingAuthorizationError as exc:
        typer.echo(f"NVIDIA embedding authorization error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaEmbeddingRateLimitDeferred as exc:
        typer.echo(f"NVIDIA embedding quota/rate limit: {exc}", err=True)
        raise typer.Exit(3) from exc

    typer.echo("")
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))


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
def examples_index(
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
) -> None:
    """Build or refresh structural embeddings for human clip examples."""
    _load_env_file()
    examples = load_human_clip_store(store)
    if not examples:
        typer.echo(f"no human clip examples found: {store}")
        return

    settings = NimEmbeddingSettings.from_env()

    async def _run():
        with SQLiteCanonicalRepository(database) as repository:
            async with NvidiaNimEmbeddingClient(settings) as provider:
                return await index_human_examples(provider, repository, examples)

    try:
        stats = asyncio.run(_run())
    except NvidiaEmbeddingAuthorizationError as exc:
        typer.echo(f"NVIDIA embedding authorization error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaEmbeddingRateLimitDeferred as exc:
        typer.echo(f"NVIDIA embedding quota/rate limit: {exc}", err=True)
        raise typer.Exit(3) from exc

    typer.echo(json.dumps(stats.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command()
def examples_retrieve(
    query: str = typer.Argument(...),
    positive_k: int = typer.Option(3, "--positive-k", min=0, max=20),
    negative_k: int = typer.Option(3, "--negative-k", min=0, max=20),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
) -> None:
    """Inspect structurally similar positive and weak-negative examples."""
    _load_env_file()
    settings = NimEmbeddingSettings.from_env()

    async def _run():
        with SQLiteCanonicalRepository(database) as repository:
            async with NvidiaNimEmbeddingClient(settings) as provider:
                return await retrieve_structural_examples(
                    provider,
                    repository,
                    query,
                    positive_k=positive_k,
                    negative_k=negative_k,
                )

    try:
        bundle = asyncio.run(_run())
    except NvidiaEmbeddingAuthorizationError as exc:
        typer.echo(f"NVIDIA embedding authorization error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaEmbeddingRateLimitDeferred as exc:
        typer.echo(f"NVIDIA embedding quota/rate limit: {exc}", err=True)
        raise typer.Exit(3) from exc

    typer.echo(json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command()
def examples_show(
    example_id: str = typer.Argument(...),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the complete audit report as JSON.",
    ),
) -> None:
    """Inspect one human-made clip example, alignment, structure and retrieval state."""
    try:
        example = find_human_example(store, example_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    with SQLiteCanonicalRepository(database) as repository:
        retrieval_documents = repository.list_retrieval_documents_for_example(
            example.id
        )

    report = build_human_example_audit(
        example,
        retrieval_documents=retrieval_documents,
    )
    if as_json:
        typer.echo(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    typer.echo(format_human_example_audit(report))


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
def migrate_legacy(
    runs_dir: Path = typer.Option(
        Path("data/evaluation/runs"),
        "--runs-dir",
    ),
    examples_store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--examples-store",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero when migration diagnostics include errors.",
    ),
) -> None:
    """Import existing evaluation/example data into the canonical SQLite store."""
    with SQLiteCanonicalRepository(database) as repository:
        report = migrate_legacy_data(
            repository,
            runs_dir=runs_dir,
            examples_store=examples_store,
        )

    typer.echo(report_as_json(report))
    typer.echo(
        "legacy JSON/JSONL files were left unchanged; current review/metrics "
        "commands continue to use them during the migration period"
    )
    if strict and report.errors:
        raise typer.Exit(2)


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
