from __future__ import annotations

import asyncio
import json
import sqlite3
import os
from pathlib import Path

import typer
from holoclip.provider_benchmark import app as provider_benchmark_app

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
from holoclip.config import GroqTranscriptionSettings, NimEmbeddingSettings
from holoclip.corpus_growth import grow_corpus
from holoclip.source_candidate_visual import verify_pending_source_visual
from holoclip.source_visual_control_report import report_saved_source_controls
from holoclip.source_pattern_worker import (
    _state_lock, run_locked_source_pattern_collection,
)
from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector
from holoclip.source_pattern_local_health import resolve_local_diagnostic_paths
from holoclip.source_pattern_local_history import read_launcher_history
from holoclip.source_ytdlp_lan_onboarding import diagnose_lan_client, import_lan_secret
from holoclip.source_ytdlp_lan_pair_evidence import export_lan_pair_evidence, compare_lan_pair_evidence, observe_lan_pair_evidence
from holoclip.source_ytdlp_lan_pair_collect import collect_lan_pair_over_ssh
from holoclip.source_ytdlp_lan_onboard_check import inspect_lan_onboard_node, check_lan_onboarding
from holoclip.source_ytdlp_lan_deployment_diagnosis import diagnose_lan_deployment
from holoclip.source_ytdlp_lan_pacing_trial import trial_lan_pacing, witness_lan_pacing
from holoclip.source_ytdlp_lan_evidence_history import (
    DEFAULT_EVIDENCE_DB, capture_lan_evidence, read_lan_evidence_history,
)
from holoclip.source_ytdlp_lan_monitor import (
    DEFAULT_MONITOR_ALERT, run_lan_monitor_once, prepare_lan_monitor_task,
    read_lan_monitor_alert,
)
from holoclip.source_ytdlp_lan_operator_profile import (
    DEFAULT_OPERATOR_PROFILE, prepare_operator_profile, operator_status,
)
from holoclip.source_ytdlp_lan_first_run_check import inspect_lan_first_run
from holoclip.source_ytdlp_lan_local_bootstrap import bootstrap_local_lan
from holoclip.source_ytdlp_lan_task_prepare import inspect_lan_task_registration
from holoclip.source_ytdlp_lan_firewall_prepare import prepare_lan_firewall
from holoclip.source_ytdlp_lan_pc_a_prepare import prepare_pc_a_lan
from holoclip.source_ytdlp_lan_pc_b_prepare import prepare_pc_b_lan
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred
from holoclip.source_ytdlp_lan_setup_doctor import inspect_lan_setup
from holoclip.source_ytdlp_lan_ssh_transfer import pull_lan_secret_sftp, _secure_windows_path
from holoclip.source_ytdlp_pacing import DEFAULT_PACING_PATH, inspect_local_ytdlp_pacing
from holoclip.source_ytdlp_lan_coordinator import (
    DEFAULT_COORDINATOR_STATE, DEFAULT_SECRET_PATH, DEFAULT_CLIENT_CONFIG,
    configure_lan_client, create_coordinator_secret,
    serve_lan_ytdlp_coordinator,
)
from holoclip.holdout_reservations import export_holdout_inventory
from holoclip.holdout_preparation import prepare_reserved_holdouts
from holoclip.holdout_audio_pipeline import align_reserved_holdouts
from holoclip.bounded_source_evidence import measure_reserved_source_pairs
from holoclip.holdout_visual_pipeline import inspect_reserved_visual_edits
from holoclip.holdout_cut_pipeline import refine_reserved_cut_candidates
from holoclip.holdout_source_clock_pipeline import calibrate_reserved_section_clocks
from holoclip.holdout_frame_links_pipeline import correlate_reserved_source_frames
from holoclip.holdout_cut_side_pipeline import inspect_reserved_cut_sides
from holoclip.holdout_partial_time_map_pipeline import build_reserved_partial_maps
from holoclip.holdout_diagnosis import diagnose_holdout_e2e
from holoclip.scripted_cut_metrics import score_scripted_cut_files
from holoclip.holdout_e2e_trial import (
    run_all_reserved_holdout_batches, run_reserved_holdout_e2e,
)
from holoclip.editorial_planning import plan_candidate_boundaries
from holoclip.editorial_evaluation import run_editorial_ab
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
    build_human_example_audit_batch,
    find_human_example,
    format_human_example_audit,
    format_human_example_audit_batch,
)
from holoclip.example_quality import (
    LearningQuality,
    evaluate_human_example_learning_quality,
)
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
    RetrievalABManifest,
    append_feedback,
    build_blind_ab_run,
    build_guided_only_run,
    build_retrieval_ab_diff,
    build_run,
    candidate_pool_fingerprint,
    compare_evaluation_metrics,
    compute_guided_discovery_metrics,
    compute_metrics,
    latest_feedback_index,
    load_feedback,
    load_known_clips,
    load_run,
    load_runs,
    save_retrieval_ab_manifest,
    save_run,
)
from holoclip.editorial_profile import (
    EditorialProfile,
    ReadinessThresholds,
    induce_editorial_profile,
    load_editorial_profile,
    save_editorial_profile,
    build_editorial_learning_cards,
    card_fingerprint,
)
from holoclip.evaluation_leak import build_evaluation_leak_report
from holoclip.learning import (
    enrich_human_clip_example,
    refresh_human_clip_weak_negatives,
)
from holoclip.media import play_media_segment
from holoclip.domain import ClipTimeline
from holoclip.render_plan import LocalRenderSource, compile_local_render_plan, render_local_preview
from holoclip.render_source_resolver import resolve_cached_render_sources
from holoclip.render_source_acquisition import acquire_one_missing_render_source
from holoclip.render_source_queue import (
    DEFAULT_QUEUE, enqueue_render_timeline, inspect_render_queue,
    advance_render_queue, reconcile_render_queue,
)
from holoclip.render_source_queue_task import prepare_render_queue_task
from holoclip.render_ready_queue import (
    DEFAULT_PREVIEW_ROOT, DEFAULT_TRANSCRIPT_ROOT, advance_completed_source_renders,
)
from holoclip.render_review_calibration import (
    DEFAULT_EXAMPLE_STORE, DEFAULT_PACK, export_review_calibration_pack,
)
from holoclip.render_human_edit_context import (
    DEFAULT_GROWTH_STATE, DEFAULT_OUTPUT as DEFAULT_HUMAN_CONTEXT_OUTPUT,
    export_human_edit_context,
)
from holoclip.render_same_source_geometry import (
    DEFAULT_OUTPUT as DEFAULT_SAME_SOURCE_OUTPUT,
    export_same_source_edit_geometry, compare_same_source_edit_geometry,
)
from holoclip.render_same_source_semantic_review import (
    DEFAULT_OUTPUT as DEFAULT_SEMANTIC_OUTPUT,
    prepare_semantic_evidence, infer_semantic_hypotheses, save_semantic_review,
    prepared_fingerprint,
)
from holoclip.render_candidate_autoqueue import (
    DEFAULT_AUTOQUEUE_CONFIG, setup_candidate_autoqueue, enroll_candidate_run,
)
from holoclip.migration import migrate_legacy_data, report_as_json
from holoclip.models import ReviewDecision
from holoclip.providers.embedding.base import EmbeddingProviderError
from holoclip.providers.embedding.nvidia_nim import (
    NvidiaEmbeddingAuthorizationError,
    NvidiaEmbeddingRateLimitDeferred,
)
from holoclip.providers.source.yt_dlp import YtDlpError, YtDlpSourceProvider
from holoclip.providers.llm.nvidia_nim import (
    NvidiaNimAuthorizationError,
    NvidiaNimTimeoutError,
)
from holoclip.providers.runtime import MeshLLMClient, MeshEmbeddingClient, RoutingSettings, ProviderRuntime
from holoclip.providers.mesh import Capability, ExecutionMode, MeshDeferred
from holoclip.providers.cost import CostPolicyError
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import (
    RETRIEVAL_VERSION,
    index_human_examples,
    retrieval_corpus_fingerprint,
    retrieve_structural_examples,
)
from holoclip.transcript import chunk_segments, load_transcript, save_transcript
from holoclip.structure_validation import validate_editorial_structure
from holoclip.transcription import transcribe_media


class ProviderCommandGroup(typer.core.TyperGroup):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CostPolicyError as exc:
            typer.echo(json.dumps({"status": "BLOCKED_QUOTA" if getattr(exc, "quota", False)
                                  else "BLOCKED_DEPENDENCY", "reason": str(exc),
                                  "retry_after": getattr(exc, "retry_after", None),
                                  "cost_policy": "ZERO_COST_ONLY", "paid_fallback_allowed": False}), err=True)
            ctx.exit(3)


app = typer.Typer(no_args_is_help=True, cls=ProviderCommandGroup)
app.add_typer(provider_benchmark_app, name="providers")


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
    settings: RoutingSettings,
    top_k: int,
    window_minutes: float,
    overlap_minutes: float,
    stage_cache: StageArtifactCache | None = None,
    retrieval_repository: SQLiteCanonicalRepository | None = None,
    editorial_profile: EditorialProfile | None = None,
    editorial_profile_path: Path | None = None,
    evaluation_transcript: Path | None = None,
    source_stream_id: str | None = None,
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
    async with MeshLLMClient(settings, event_callback=typer.echo) as client:
        if editorial_profile_path is not None:
            try:
                editorial_profile = await _refresh_editorial_profile(
                    client, settings=settings, stage_cache=stage_cache,
                    path=editorial_profile_path,
                )
            except (NvidiaNimTimeoutError, NvidiaNimAuthorizationError, ValueError) as exc:
                if getattr(client, "strict_output", False):
                    raise MeshDeferred([f"Editorial profile refresh needs repair: {type(exc).__name__}"]) from exc
                typer.echo(
                    "Automatic editorial profile refresh deferred; continuing "
                    f"without learned profile: {exc}"
                )
                editorial_profile = None
        if editorial_profile and editorial_profile.readiness.ready and evaluation_transcript is not None:
            try:
                report = await run_editorial_ab(client, transcript=evaluation_transcript,
                    stream_id=source_stream_id or evaluation_transcript.stem,
                    examples=load_human_clip_store(Path(editorial_profile.induction_config.get(
                        "corpus_store", str(editorial_profile_path.parent / "human_clips.jsonl")
                        if editorial_profile_path else "data/examples/human_clips.jsonl"))),
                    profile=editorial_profile, fast_model=settings.fast_model, strong_model=settings.model,
                    fast_reasoning_effort=settings.fast_reasoning_effort,
                    strong_reasoning_effort=settings.strong_reasoning_effort, stage_cache=stage_cache,
                    top_k=top_k)
                report_id = make_human_clip_example_id(str(evaluation_transcript.resolve()))
                report_path = Path("data/evaluation/editorial-ab") / editorial_profile.fingerprint[:16] / f"{report_id}.json"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                typer.echo(f"Automatic unseen-stream ON/OFF diagnostic: {report_path}")
            except ValueError as exc:
                typer.echo(f"Automatic ON/OFF deferred: {exc}")
        async def discover_and_plan(detector):
            broad = await detector.discover_broad(chunks)
            if editorial_profile and editorial_profile.readiness.ready:
                # Strong preliminary selection bounds planning cost, then final selection
                # sees the actual retained spans and compression of each planned candidate.
                shortlist = await detector.final_select(broad, top_k=min(20, max(top_k * 2, 10)))
                planned = await plan_candidate_boundaries(client, shortlist, segments,
                    profile=editorial_profile, model=settings.model,
                    reasoning_effort=settings.strong_reasoning_effort, stage_cache=stage_cache)
                return await detector.final_select(planned, top_k=top_k)
            return await detector.final_select(broad, top_k=top_k)
        if retrieval_repository is None:
            detector = CandidateDetector(
                client,
                fast_model=settings.fast_model,
                strong_model=settings.model,
                fast_reasoning_effort=settings.fast_reasoning_effort,
                strong_reasoning_effort=settings.strong_reasoning_effort,
                progress=typer.echo,
                stage_cache=stage_cache,
                cache_provider="capability_mesh",
                editorial_profile=editorial_profile,
            )
            return await discover_and_plan(detector)

        embedding_settings = NimEmbeddingSettings.from_env()
        async with MeshEmbeddingClient(embedding_settings) as embedding_client:
            detector = CandidateDetector(
                client,
                fast_model=settings.fast_model,
                strong_model=settings.model,
                fast_reasoning_effort=settings.fast_reasoning_effort,
                strong_reasoning_effort=settings.strong_reasoning_effort,
                progress=typer.echo,
                stage_cache=stage_cache,
                cache_provider="capability_mesh",
                embedding_provider=embedding_client,
                retrieval_repository=retrieval_repository,
                editorial_profile=editorial_profile,
            )
            return await discover_and_plan(detector)


def _load_ready_editorial_profile(
    path: Path | None,
) -> EditorialProfile | None:
    if path is None or not path.exists():
        return None
    profile = load_editorial_profile(path)
    store = Path(profile.induction_config.get("corpus_store", str(path.parent / "human_clips.jsonl")))
    if not store.exists() or card_fingerprint(build_editorial_learning_cards(load_human_clip_store(store))) != profile.corpus_fingerprint:
        return None
    if not profile.readiness.ready:
        return None
    return profile


async def _refresh_editorial_profile(client, *, settings, stage_cache, path: Path, store: Path | None = None):
    existing = load_editorial_profile(path) if path.exists() else None
    store = store or Path(existing.induction_config.get("corpus_store", str(path.parent / "human_clips.jsonl")) if existing else path.parent / "human_clips.jsonl")
    examples = load_human_clip_store(store)
    cards = build_editorial_learning_cards(examples)
    if not cards:
        return None
    thresholds = existing.thresholds if existing is not None else ReadinessThresholds()
    trusted_streams = len({card.stream_id for card in cards if card.stream_id})
    if (
        len(cards) < thresholds.trusted_examples
        or trusted_streams < thresholds.unique_streams
    ):
        typer.echo(
            "Editorial profile auto-refresh deferred: corpus count floors are not met "
            f"({len(cards)}/{thresholds.trusted_examples} trusted, "
            f"{trusted_streams}/{thresholds.unique_streams} streams)."
        )
        return None
    if (existing is None or existing.corpus_fingerprint != card_fingerprint(cards)
            or existing.model != settings.model or existing.observation_model != settings.fast_model
            or existing.induction_config.get("reasoning_effort") != settings.strong_reasoning_effort
            or existing.induction_config.get("observation_reasoning_effort") != settings.fast_reasoning_effort
            or existing.version != "editorial-profile-v2"):
        typer.echo("Trusted corpus changed: refreshing editorial profile with cached stages")
        existing = await induce_editorial_profile(
            client, examples, model=settings.model, reasoning_effort=settings.strong_reasoning_effort,
            observation_model=settings.fast_model, observation_reasoning_effort=settings.fast_reasoning_effort,
            thresholds=existing.thresholds if existing else None,
            stage_cache=stage_cache, cache_provider="capability_mesh",
            plan_path=path.with_suffix(".batches.json"), cards_directory=path.parent / "learning_cards",
            progress=typer.echo,
        )
        existing.induction_config["corpus_store"] = str(store)
        save_editorial_profile(existing, path)
    typer.echo(f"Editorial readiness={existing.readiness.ready}: "
               f"{existing.trusted_examples} trusted / {existing.unique_streams} streams")
    return existing if existing.readiness.ready else None


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
    editorial_profile: Path | None = typer.Option(
        Path("data/examples/editorial_profile.json"),
        "--editorial-profile",
        help=(
            "Use the learned editorial profile only after its readiness gate passes."
        ),
    ),
) -> None:
    """Analyze a timed transcript JSON file and print clip candidates."""
    _load_env_file()
    settings = RoutingSettings.from_env()
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
            editorial_profile=_load_ready_editorial_profile(editorial_profile),
            editorial_profile_path=editorial_profile,
            evaluation_transcript=transcript,
            source_stream_id=stream_id or transcript.stem,
        )

        run = build_run(
            stream_id=stream_id or transcript.stem,
            transcript_path=str(transcript),
            candidates=candidates,
        )
        if save_run_path is not None:
            save_run(run, save_run_path)
        _autoqueue_generated_candidates(run)

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
    editorial_profile: Path | None = typer.Option(
        Path("data/examples/editorial_profile.json"),
        "--editorial-profile",
        help=(
            "Use the learned editorial profile only after its readiness gate passes."
        ),
    ),
) -> None:
    """Transcribe local media, discover clip candidates, and save an evaluation run."""
    _load_env_file()
    nim_settings = RoutingSettings.from_env()
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
            editorial_profile=_load_ready_editorial_profile(editorial_profile),
            editorial_profile_path=editorial_profile,
            evaluation_transcript=transcript_path,
            source_stream_id=resolved_stream_id,
        )
        run = build_run(
            stream_id=resolved_stream_id,
            transcript_path=str(transcript_path),
            candidates=candidates,
            media_path=str(media.resolve()),
        )
        save_run(run, run_path)
        _autoqueue_generated_candidates(run)
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
def examples_learn_profile(
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    output: Path = typer.Option(
        Path("data/examples/editorial_profile.json"),
        "--output",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    batch_size: int = typer.Option(
        8,
        "--batch-size",
        min=2,
        max=12,
    ),
    diagnostic_fast: bool = typer.Option(False, "--diagnostic-fast",
        help="Generate a non-production diagnostic profile with the fast model if strong NIM is unavailable."),
) -> None:
    """Learn a corpus-level editorial model from trusted human-made clips."""
    _load_env_file()
    settings = RoutingSettings.from_env()
    examples = load_human_clip_store(store)

    async def _run(stage_cache: StageArtifactCache) -> EditorialProfile:
        async with MeshLLMClient(
            settings,
            event_callback=typer.echo,
        ) as llm:
            return await induce_editorial_profile(
                llm,
                examples,
                model=settings.fast_model if diagnostic_fast else settings.model,
                reasoning_effort=settings.fast_reasoning_effort if diagnostic_fast else settings.strong_reasoning_effort,
                batch_size=batch_size,
                stage_cache=stage_cache,
                cache_provider="capability_mesh",
                observation_model=settings.fast_model,
                observation_reasoning_effort=settings.fast_reasoning_effort,
                plan_path=output.with_suffix(".batches.json"),
                cards_directory=output.parent / "learning_cards",
                progress=typer.echo,
                production_eligible=not diagnostic_fast,
            )

    try:
        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            profile = asyncio.run(_run(stage_cache))
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        raise typer.Exit(3) from exc
    except ValueError as exc:
        typer.echo(f"editorial learning error: {exc}", err=True)
        raise typer.Exit(4) from exc

    profile.induction_config["corpus_store"] = str(store)
    save_editorial_profile(profile, output)
    typer.echo(
        json.dumps(
            {
                "output": str(output),
                "version": profile.version,
                "corpus_fingerprint": profile.corpus_fingerprint,
                "trusted_examples": profile.trusted_examples,
                "unique_streams": profile.unique_streams,
                "patterns": len(profile.patterns),
                "core_patterns": profile.readiness.core_patterns,
                "core_coverage_ratio": profile.readiness.core_coverage_ratio,
                "ready": profile.readiness.ready,
                "readiness_reasons": profile.readiness.reasons,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def examples_learning_status(
    profile_path: Path = typer.Option(
        Path("data/examples/editorial_profile.json"),
        "--profile",
    ),
) -> None:
    """Show whether corpus-level editorial learning is ready for validation."""
    if not profile_path.exists():
        typer.echo(
            json.dumps(
                {
                    "ready": False,
                    "profile": str(profile_path),
                    "reasons": ["editorial profile has not been learned yet"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    profile = load_editorial_profile(profile_path)
    typer.echo(
        json.dumps(
            {
                "ready": profile.readiness.ready,
                "profile": str(profile_path),
                "version": profile.version,
                "model": profile.model,
                "trusted_examples": profile.trusted_examples,
                "unique_streams": profile.unique_streams,
                "core_patterns": profile.readiness.core_patterns,
                "core_coverage_ratio": profile.readiness.core_coverage_ratio,
                "reasons": profile.readiness.reasons,
                "patterns": [
                    {
                        "id": pattern.id,
                        "name": pattern.name,
                        "maturity": pattern.maturity.value,
                        "support_examples": len(pattern.support_example_ids),
                        "support_streams": len(pattern.support_stream_ids),
                        "confidence": pattern.confidence,
                    }
                    for pattern in profile.patterns
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def evaluation_leak_check(
    transcript: Path = typer.Argument(..., exists=True, readable=True),
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
) -> None:
    """Check an evaluation transcript for overlap with human-example sources."""
    examples = load_human_clip_store(store)
    report = build_evaluation_leak_report(transcript, examples)
    typer.echo(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    if report.suspected_overlap:
        raise typer.Exit(5)


@app.command("examples-score-scripted-cuts")
def examples_score_scripted_cuts(
    reference: Path = typer.Option(
        ..., exists=True, readable=True,
        help="Known synthetic FFmpeg construction boundaries JSON",
    ),
    cut_report: Path = typer.Option(
        Path("data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json"),
        exists=True, readable=True,
        help="Measured unreviewed cut-brackets JSON from the nine-stage trial",
    ),
    output: Path = typer.Option(
        Path("data/provider-benchmark/holdout/scripted-cut-scores.json"),
        help="New isolated synthetic cut metrics; never overwrites original audit",
    ),
    tolerance_seconds: float = typer.Option(0., min=0., max=1.),
    max_bracket_seconds: float = typer.Option(8., min=.01, max=12.),
) -> None:
    """Score scripted artificial cuts only, never human-gold editor accuracy."""
    try:
        scored = score_scripted_cut_files(
            reference_path=reference, report_path=cut_report, output_path=output,
            tolerance_seconds=tolerance_seconds,
            max_bracket_seconds=max_bracket_seconds,
        )
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        # Public errors are fixed, avoiding contents from arbitrary supplied
        # files and any measured-source/provider exception body.
        typer.echo(json.dumps({
            "status": "BLOCKED_INVALID_SCRIPTED_CUT_EVIDENCE",
            "error_class": type(exc).__name__, "ready": False,
        }), err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(json.dumps(scored, ensure_ascii=False, indent=2))


@app.command("examples-diagnose-holdout-e2e")
def examples_diagnose_holdout_e2e(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    output: Path | None = typer.Option(
        None, help="Offline diagnostic output JSON; defaults inside --root",
    ),
) -> None:
    """Diagnose saved holdout stages offline, without acquiring any new media."""
    report = diagnose_holdout_e2e(
        store=store, state_path=state, root=root, output=output,
        policy_rules=load_policy_rules(policy_file),
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] not in {"UNREVIEWED_TRIAL_RECORDED"}:
        raise typer.Exit(code=3)


@app.command("examples-validate-holdout-e2e")
def examples_validate_holdout_e2e(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
    preflight_only: bool = typer.Option(False, help="Check reserved clips without downloading media"),
    all_batches: bool = typer.Option(False, help="Process all reserved clips once in bounded batches"),
    max_batches: int = typer.Option(8, min=1, max=12),
    continue_on_insufficient_audio: bool = typer.Option(
        False,
        help="Isolate each clip; defer only uniquely insufficient original audio evidence",
    ),
    resume_checkpoint: bool = typer.Option(
        False,
        help="Revalidate saved complete batches before resuming; never trust cached success alone",
    ),
) -> None:
    """Run local-source video evidence with fail-closed checkpoints."""
    if all_batches and preflight_only:
        raise typer.BadParameter("Use --preflight-only without --all-batches")
    if continue_on_insufficient_audio and not all_batches:
        raise typer.BadParameter("--continue-on-insufficient-audio requires --all-batches")
    if resume_checkpoint and not all_batches:
        raise typer.BadParameter("--resume-checkpoint requires --all-batches")
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    rules = load_policy_rules(policy_file)
    if all_batches:
        report = run_all_reserved_holdout_batches(
            provider, store=store, state_path=state, root=root,
            policy_rules=rules, max_clips=max_clips, max_batches=max_batches,
            progress=typer.echo,
            continue_on_insufficient_audio=continue_on_insufficient_audio,
            resume_checkpoint=resume_checkpoint,
        )
        allowed = {"UNREVIEWED_ALL_BATCHES_COMPLETE"}
    else:
        report = run_reserved_holdout_e2e(
            provider, store=store, state_path=state, root=root,
            policy_rules=rules, max_clips=max_clips, preflight_only=preflight_only,
            progress=typer.echo,
        )
        allowed = {
            "UNREVIEWED_MEASUREMENTS_COMPLETE",
            "PREFLIGHT_ONLY_NO_MEDIA_EXECUTED",
        }
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if all_batches:
        # Media-heavy work has already completed or failed. A second,
        # inexpensive, offline-only triage makes the first blocked stage
        # actionable without requiring a separate user command.
        diagnosis = diagnose_holdout_e2e(
            store=store, state_path=state, root=root, policy_rules=rules,
        )
        typer.echo(json.dumps({
            "offline_diagnosis": diagnosis["status"],
            "diagnostic_report_path": diagnosis["report_path"],
            "next_action": diagnosis["next_action"],
            "first_blocker": diagnosis["first_blocker"],
            "cut_signal_triage": diagnosis["cut_signal_triage"]["status"],
            "cut_signal_counts": diagnosis["cut_signal_triage"].get(
                "signal_counts", {},
            ),
            "unattempted_clips": len(diagnosis["unattempted_clip_ids"]),
            "deferred_insufficient_audio_clips": report.get("deferred_audio_clips", 0),
        }, ensure_ascii=False, indent=2))
    if report["status"] not in allowed:
        raise typer.Exit(code=3)


@app.command("examples-build-partial-time-map")
def examples_build_partial_time_map(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    clocks_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_clocks")),
    frame_links_root: Path = typer.Option(Path("data/provider-benchmark/holdout/frame_links")),
    cuts_root: Path = typer.Option(Path("data/provider-benchmark/holdout/cut_brackets")),
    cut_sides_root: Path = typer.Option(Path("data/provider-benchmark/holdout/cut_sides")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/partial_time_maps")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
) -> None:
    """Build bounded, unreviewed source/clip time-map candidates only."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = build_reserved_partial_maps(
        provider, store=store, state_path=state,
        prepared_root=prepared_root, audio_root=audio_root,
        pairs_root=pairs_root, clocks_root=clocks_root,
        frame_links_root=frame_links_root, cuts_root=cuts_root,
        cut_sides_root=cut_sides_root, output=output,
        policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-compare-cut-sides")
def examples_compare_cut_sides(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    clocks_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_clocks")),
    frame_links_root: Path = typer.Option(Path("data/provider-benchmark/holdout/frame_links")),
    cuts_root: Path = typer.Option(Path("data/provider-benchmark/holdout/cut_brackets")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/cut_sides")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
) -> None:
    """Inspect corresponding original/finished frames near candidate human edits."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = inspect_reserved_cut_sides(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        audio_root=audio_root, pairs_root=pairs_root, clocks_root=clocks_root,
        frame_links_root=frame_links_root, cuts_root=cuts_root,
        output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-correlate-source-frames")
def examples_correlate_source_frames(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    clocks_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_clocks")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/frame_links")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
    max_frames: int = typer.Option(64, min=1, max=64),
) -> None:
    """Compare measured original/finished frames conditioned on acoustic offsets."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = correlate_reserved_source_frames(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        audio_root=audio_root, pairs_root=pairs_root, clocks_root=clocks_root,
        output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, max_frames=max_frames, progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-calibrate-source-clocks")
def examples_calibrate_source_clocks(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/source_clocks")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
) -> None:
    """Measure bounded source-section audio origins and native local frame PTS."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = calibrate_reserved_section_clocks(
        provider, store=store, state_path=state,
        prepared_root=prepared_root, audio_root=audio_root,
        pairs_root=pairs_root, output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-refine-cut-candidates")
def examples_refine_cut_candidates(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    visual_root: Path = typer.Option(Path("data/provider-benchmark/holdout/visual_edits")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/cut_brackets")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
    max_points: int = typer.Option(480, min=10, max=1000),
) -> None:
    """Refine acoustic transition time brackets, never assert exact edit-frame cuts."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = refine_reserved_cut_candidates(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        audio_root=audio_root, pairs_root=pairs_root, visual_root=visual_root,
        output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, max_points=max_points, progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-inspect-visual-edits")
def examples_inspect_visual_edits(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    pairs_root: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/visual_edits")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
    max_frames: int = typer.Option(64, min=1, max=64),
    time_tolerance: float = typer.Option(4.0, min=.25, max=10),
) -> None:
    """Measure sparse source/finished visual geometry; never claim gold or exact cuts."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = inspect_reserved_visual_edits(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        audio_root=audio_root, pairs_root=pairs_root, output=output,
        policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, max_frames=max_frames, time_tolerance=time_tolerance,
        progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-measure-source-pairs")
def examples_measure_source_pairs(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    audio_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout/source_pairs")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
    max_ranges: int = typer.Option(3, min=1, max=5),
    padding_seconds: float = typer.Option(3.0, min=2, max=10),
) -> None:
    """Measure bounded original/finished video pairs; does not verify gold or exact cuts."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    result = measure_reserved_source_pairs(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        audio_root=audio_root, output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, max_ranges=max_ranges, padding_seconds=padding_seconds,
        progress=typer.echo,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("examples-align-holdout")
def examples_align_holdout(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    prepared_root: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(3, min=1, max=20),
) -> None:
    """Find conservative audio anchors in original source; never certify exact cuts."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    report = align_reserved_holdouts(
        provider, store=store, state_path=state, prepared_root=prepared_root,
        output=output, policy_rules=load_policy_rules(policy_file),
        max_clips=max_clips, progress=typer.echo,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("examples-prepare-holdout")
def examples_prepare_holdout(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout")),
    max_clips: int = typer.Option(3, min=1, max=20),
    window_seconds: float = typer.Option(20, min=5, max=120),
) -> None:
    """Fetch and measure reserved public clips; never certify gold or production routes."""
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    report = prepare_reserved_holdouts(
        provider, store=store, state_path=state, output=output,
        policy_rules=load_policy_rules(policy_file), max_clips=max_clips,
        window_seconds=window_seconds, progress=typer.echo,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("examples-holdout-status")
def examples_holdout_status(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    output: Path = typer.Option(Path("data/provider-benchmark/holdout-reservations.json")),
) -> None:
    """Export automatic source-level holdout reservations (not reviewed gold)."""
    report = export_holdout_inventory(store, state, output)
    typer.echo(json.dumps({**report, "output": str(output)}, ensure_ascii=False, indent=2))


def _blocked_corpus_state_writer(state_path: Path) -> None:
    """Never let a skipped writer masquerade as a successful run."""
    typer.echo(json.dumps({
        "status": "BLOCKED_CONCURRENT_CORPUS_STATE_WRITER",
        "state_path": str(state_path),
        "ready": False,
        "production_ready": False,
        "qualification_allowed": False,
        "reviewed_gold_available": False,
    }, ensure_ascii=False))
    raise typer.Exit(6)


@app.command("examples-collector-doctor")
def examples_collector_doctor(
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    report_path: Path = typer.Option(Path("data/examples/source-pattern-last-run.json")),
    launcher_path: Path = typer.Option(Path("data/examples/source-pattern-launcher-last-run.json")),
    check_tools: bool = typer.Option(True, "--check-tools/--no-check-tools"),
    media_output: Path = typer.Option(Path("data/sources/source_visual_scout")),
    audit_media_cache: bool = typer.Option(
        True, "--audit-media-cache/--no-audit-media-cache",
    ),
) -> None:
    """Offline runtime and disk-byte evidence audit; no public video requests."""
    report = diagnose_source_pattern_collector(
        state_path=state, report_path=report_path,
        launcher_path=launcher_path, check_tools=check_tools,
        media_output=media_output, audit_media_cache=audit_media_cache,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("examples-collector-health")
def examples_collector_health(
    config_path: Path | None = typer.Option(None, "--config-path"),
    check_tools: bool = typer.Option(True, "--check-tools/--no-check-tools"),
    audit_media_cache: bool = typer.Option(
        True, "--audit-media-cache/--no-audit-media-cache",
    ),
) -> None:
    """Compact local-only collection diagnosis using Windows JSON settings."""
    paths, loaded = resolve_local_diagnostic_paths(
        Path.cwd(), config_path=config_path,
    )
    doctor = diagnose_source_pattern_collector(
        state_path=paths["state"], report_path=paths["report_path"],
        launcher_path=paths["launcher_audit_path"],
        check_tools=check_tools, media_output=paths["output"],
        audit_media_cache=audit_media_cache,
    )
    history_path = paths["launcher_audit_path"].with_name(
        "source-pattern-launcher-history.json"
    )
    compact = {
        **doctor["local_collection_health"],
        "local_settings_loaded": loaded,
        "native_launcher_history": read_launcher_history(history_path),
        "yt_dlp_access": inspect_local_ytdlp_pacing(),
    }
    typer.echo(json.dumps(compact, ensure_ascii=False, indent=2))


@app.command("examples-ytdlp-access-status")
def examples_ytdlp_access_status(
    pacing_path: Path = typer.Option(DEFAULT_PACING_PATH, "--pacing-path"),
) -> None:
    """Read local yt-dlp access gate; no network and no storage writes."""
    typer.echo(json.dumps(
        inspect_local_ytdlp_pacing(pacing_path),
        ensure_ascii=False, indent=2,
    ))


@app.command("examples-ytdlp-lan-init")
def examples_ytdlp_lan_init(
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
) -> None:
    """Create an owner-held secret once; never print/overwrite its contents."""
    create_coordinator_secret(secret_file)
    typer.echo(json.dumps({
        "status": "LOCAL_LAN_COORDINATOR_SECRET_CREATED",
        "secret_printed": False, "ready": False,
    }))


@app.command("examples-ytdlp-lan-import-secret")
def examples_ytdlp_lan_import_secret(
    source_file: Path = typer.Option(..., "--source-file",
        help="Existing trusted local copy (USB or independently secured transfer)."),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file",
        help="Local credential destination; never overwritten."),
) -> None:
    """Import a validated LAN credential without printing or transmitting it."""
    try:
        result = import_lan_secret(source_file=source_file, secret_file=secret_file)
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_LAN_SECRET_IMPORT_REJECTED",
            "reason": type(exc).__name__, "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps({
        "status": result, "secret_printed": False,
        "network_access": False, "windows_acl_verified": False,
        "ready": False,
    }))


@app.command("examples-ytdlp-lan-pull-secret")
def examples_ytdlp_lan_pull_secret(
    host: str = typer.Option(..., "--host", help="PC-A trusted private IPv4."),
    user: str = typer.Option(..., "--user", help="SSH username on PC-A."),
    remote_path: str = typer.Option(..., "--remote-path",
        help="Absolute PC-A SFTP path, e.g. D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Preverified known_hosts file. Unknown host keys are never accepted."),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
) -> None:
    """Pull private credential via verified-key SFTP; no YouTube access."""
    try:
        report = pull_lan_secret_sftp(
            host=host, user=user, remote_path=remote_path,
            known_hosts=known_hosts, secret_file=secret_file, port=port,
        )
    except (OSError, ValueError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_SECRET_SFTP_REJECTED",
            "reason": str(exc) if str(exc).startswith("LAN_") else type(exc).__name__,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report))


@app.command("examples-ytdlp-lan-secret-acl")
def examples_ytdlp_lan_secret_acl(
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    apply: bool = typer.Option(False, "--apply",
        help="Explicitly restrict the current user's Windows NTFS DACL."),
) -> None:
    """Audit or restrict local LAN credential ACL without printing it."""
    try:
        verified = _secure_windows_path(secret_file.absolute(), apply=apply)
    except (OSError, ValueError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_WINDOWS_ACL_NOT_SECURE",
            "reason": type(exc).__name__, "ready": False,
        }))
        raise typer.Exit(6) from exc
    if not verified:
        typer.echo(json.dumps({"status": "WINDOWS_REQUIRED", "ready": False}))
        raise typer.Exit(6)
    typer.echo(json.dumps({
        "status": "ACL_SECURE", "acl_verified": True, "ready": False,
    }))


@app.command("examples-ytdlp-lan-onboard-node")
def examples_ytdlp_lan_onboard_node() -> None:
    """PC-B read-only local setup and signed status for pinned SSH collection."""
    _load_env_file()
    try:
        report = inspect_lan_onboard_node(role="pc-b")
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_ONBOARD_NODE_REJECTED",
            "reason": (str(exc) if str(exc).startswith(("LAN_ONBOARD_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    # Always emit a bounded JSON report even when not configured. It is a
    # negative diagnosis, never a readiness assertion.
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")))


@app.command("examples-ytdlp-lan-pacing-witness")
def examples_ytdlp_lan_pacing_witness(
    phase: str = typer.Option(..., "--phase", help="Fixed PC-B trial phase: held or gap."),
) -> None:
    """PC-B signed status and denied lease witness; no media operation."""
    _load_env_file()
    try:
        report = witness_lan_pacing(phase=phase)
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "phase": "invalid",
            "status": "LAN_PACING_WITNESS_REJECTED",
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report["status"] not in {
        "LAN_PACING_WITNESS_HELD_DENIED", "LAN_PACING_WITNESS_GAP_DENIED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-pacing-trial")
def examples_ytdlp_lan_pacing_trial(
    host: str = typer.Option(..., "--host", help="PC-B private LAN IPv4."),
    user: str = typer.Option(..., "--user", help="PC-B SSH public-key account."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Independently verified PC-B SSH host key file."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="Absolute Windows Holoclip repository directory on PC-B."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
    apply: bool = typer.Option(False, "--apply",
        help="Opt in to a real coordinator lease and 20s gap, but NO YouTube."),
) -> None:
    """Preview first; test cross-PC lease/gap without launching yt-dlp."""
    _load_env_file()
    try:
        report = trial_lan_pacing(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
            coordinator_port=coordinator_port, task_name=task_name,
            apply=apply,
        )
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_PACING_TRIAL_REJECTED",
            "reason": "LAN_PACING_INPUT_OR_TRUST_REJECTED",
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report["status"] not in {
        "LAN_PACING_PREVIEW_ONLY", "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED",
    }:
        raise typer.Exit(6)


@app.command("render-candidate-autoqueue-setup")
def render_candidate_autoqueue_setup_command(
    config: Path = typer.Option(DEFAULT_AUTOQUEUE_CONFIG, "--config"),
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    database: Path = typer.Option(Path("data/holoclip.db"), "--database"),
    cache_root: Path = typer.Option(Path("data/sources"), "--cache-root"),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"), "--policy-file"),
    min_confidence: float = typer.Option(
        0.85, "--min-confidence", min=0.75, max=1),
    max_candidates_per_run: int = typer.Option(
        1, "--max-candidates-per-run", min=1, max=3),
    apply: bool = typer.Option(False, "--apply"),
    approve_local_preview: bool = typer.Option(
        False, "--approve-local-preview",
        help="One-time permission for qualifying candidate local-source queue enrollment."),
) -> None:
    """Configure once, then analyze/discover auto-enroll qualifying candidates."""
    try:
        report = setup_candidate_autoqueue(
            config=config, queue=queue, database=database,
            cache_root=cache_root, policy_file=policy_file,
            min_confidence=min_confidence,
            max_candidates_per_run=max_candidates_per_run,
            apply=apply, approve_local_preview=approve_local_preview,
        )
    except (ValueError, OSError, UnicodeError, TypeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_AUTOQUEUE_SETUP_REJECTED",
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))


def _autoqueue_generated_candidates(run) -> None:
    """Non-fatal acquisition queue hook: no network, no raw source/prose logs."""
    try:
        report = enroll_candidate_run(run)
        if report["status"] != "LOCAL_RENDER_AUTOQUEUE_DISABLED":
            typer.echo(
                "Local candidate autoqueue: "
                f"{report['status']}, enrolled={report['enrolled_count']}, "
                f"already_enrolled={report['already_enrolled_count']}"
            )
    except (ValueError, OSError, UnicodeError, TypeError, sqlite3.Error):
        typer.echo("Local candidate autoqueue: LOCAL_RENDER_AUTOQUEUE_BLOCKED")


@app.command("render-source-queue-task-prepare")
def render_source_queue_task_prepare_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    task_name: str = typer.Option("Holoclip render source queue", "--task-name"),
    interval_minutes: int = typer.Option(60, "--interval-minutes", min=30, max=1440),
    apply: bool = typer.Option(False, "--apply",
        help="Explicitly register a current-user recurring worker; never start task."),
) -> None:
    """Preview-first scheduled unattended queue worker installation on Windows."""
    try:
        report = prepare_render_queue_task(
            queue=queue, task_name=task_name,
            interval_minutes=interval_minutes, apply=apply,
        )
    except (ValueError, OSError, TypeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_QUEUE_TASK_SETUP_REJECTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LOCAL_RENDER_QUEUE_TASK_PREVIEW_ELIGIBLE",
        "LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED",
    }:
        raise typer.Exit(6)


@app.command("render-ready-preview")
def render_ready_preview_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    preview_root: Path = typer.Option(DEFAULT_PREVIEW_ROOT, "--preview-root"),
    transcript_root: Path = typer.Option(DEFAULT_TRANSCRIPT_ROOT, "--transcript-root",
        help="Existing local ASR transcripts for optional cut-aligned SRT sidecar."),
    execute: bool = typer.Option(False, "--execute",
        help="Render at most one complete-source timeline to local MP4, never publish."),
) -> None:
    """Offline source-complete queue -> one technical MP4 + persistent QA outcome."""
    try:
        result = advance_completed_source_renders(
            queue=queue, preview_root=preview_root,
            transcript_root=transcript_root, execute=execute,
        )
    except (ValueError, OSError, UnicodeError, TypeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_AUTO_QA_WORKER_BLOCKED",
            "ready": False, "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result["status"] in {
        "LOCAL_RENDER_AUTO_QA_BLOCKED_PREVIOUS",
        "LOCAL_RENDER_AUTO_QA_PAUSED_NEEDS_REVIEW",
    }:
        raise typer.Exit(6)


@app.command("render-review-calibration-pack")
def render_review_calibration_pack_command(
    preview_root: Path = typer.Option(DEFAULT_PREVIEW_ROOT, "--preview-root"),
    example_store: Path = typer.Option(DEFAULT_EXAMPLE_STORE, "--example-store"),
    output: Path = typer.Option(DEFAULT_PACK, "--output"),
    after_job_id: str | None = typer.Option(None, "--after-job-id",
        help="Continue after the previous page's next_cursor."),
    max_jobs: int = typer.Option(40, "--max-jobs", min=1, max=40),
    max_candidates: int = typer.Option(30, "--max-candidates", min=1, max=30),
    execute: bool = typer.Option(False, "--execute",
        help="Explicit create-only offline unlabeled review sampling pack."),
) -> None:
    """Inspect existing review evidence and alignment context; never infer gold."""
    try:
        report = export_review_calibration_pack(
            preview_root=preview_root, example_store=example_store,
            output=output, execute=execute,
            after_job_id=after_job_id, max_jobs=max_jobs,
            max_candidates=max_candidates,
        )
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_EDIT_PACK_BLOCKED", "ready": False,
            "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))


@app.command("render-ab-human-context")
def render_ab_human_context_command(
    example_store: Path = typer.Option(DEFAULT_EXAMPLE_STORE, "--example-store"),
    growth_state: Path = typer.Option(DEFAULT_GROWTH_STATE, "--growth-state"),
    preview_root: Path = typer.Option(DEFAULT_PREVIEW_ROOT, "--preview-root"),
    output: Path = typer.Option(DEFAULT_HUMAN_CONTEXT_OUTPUT, "--output"),
    after_job_id: str | None = typer.Option(
        None, "--after-job-id", help="Advance only the A/B metric inventory cursor."),
    max_jobs: int = typer.Option(40, "--max-jobs", min=1, max=40),
    execute: bool = typer.Option(
        False, "--execute", help="Create-only offline, unqualified context report."),
) -> None:
    """Join trusted human structural metadata with unscored A/B technical signals."""
    try:
        report = export_human_edit_context(
            example_store=example_store, growth_state=growth_state,
            preview_root=preview_root, output=output,
            after_job_id=after_job_id, max_jobs=max_jobs,
            execute=execute,
        )
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_HUMAN_EDIT_CONTEXT_BLOCKED",
            "ready": False, "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))


@app.command("render-same-source-geometry")
def render_same_source_geometry_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    example_store: Path = typer.Option(DEFAULT_EXAMPLE_STORE, "--example-store"),
    growth_state: Path = typer.Option(DEFAULT_GROWTH_STATE, "--growth-state"),
    preview_root: Path = typer.Option(DEFAULT_PREVIEW_ROOT, "--preview-root"),
    transcript_root: Path = typer.Option(DEFAULT_TRANSCRIPT_ROOT, "--transcript-root"),
    output: Path = typer.Option(DEFAULT_SAME_SOURCE_OUTPUT, "--output"),
    after_job_id: str | None = typer.Option(
        None, "--after-job-id", help="Continue after a prior page's next_cursor."),
    max_jobs: int = typer.Option(40, "--max-jobs", min=1, max=40),
    execute: bool = typer.Option(
        False, "--execute", help="Save only an immutable offline comparison report."),
) -> None:
    """Compare same original-source edit selections without human quality claims."""
    try:
        report = export_same_source_edit_geometry(
            queue=queue, example_store=example_store,
            growth_state=growth_state, preview_root=preview_root,
            transcript_root=transcript_root,
            output=output, after_job_id=after_job_id, max_jobs=max_jobs,
            execute=execute,
        )
    except (OSError, ValueError, UnicodeError, TypeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_SAME_SOURCE_GEOMETRY_BLOCKED",
            "ready": False, "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))




@app.command("render-same-source-semantic-review")
def render_same_source_semantic_review_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    example_store: Path = typer.Option(DEFAULT_EXAMPLE_STORE, "--example-store"),
    growth_state: Path = typer.Option(DEFAULT_GROWTH_STATE, "--growth-state"),
    preview_root: Path = typer.Option(DEFAULT_PREVIEW_ROOT, "--preview-root"),
    transcript_root: Path = typer.Option(DEFAULT_TRANSCRIPT_ROOT, "--transcript-root"),
    after_job_id: str | None = typer.Option(None, "--after-job-id"),
    max_jobs: int = typer.Option(40, "--max-jobs", min=1, max=40),
    output: Path = typer.Option(DEFAULT_SEMANTIC_OUTPUT, "--output"),
    execute: bool = typer.Option(
        False, "--execute",
        help="Explicitly send public local ASR/story excerpts to a qualified AI route and save a create-only report."),
) -> None:
    """Inspect source-cited story hypotheses; dry-run never calls a provider."""
    try:
        base = compare_same_source_edit_geometry(
            queue=queue, example_store=example_store,
            growth_state=growth_state, preview_root=preview_root,
            transcript_root=transcript_root, after_job_id=after_job_id,
            max_jobs=max_jobs,
        )
        prepared = prepare_semantic_evidence(base)
        if not execute:
            prepared["evidence_sha256"] = prepared_fingerprint(prepared)
            typer.echo(json.dumps(prepared, ensure_ascii=False, sort_keys=True))
            return
        if os.getenv("HOLOCLIP_DATA_CLASS") != "public":
            raise ValueError("LOCAL_SEMANTIC_EXPLICIT_PUBLIC_DATA_REQUIRED")
        if prepared["status"] != "LOCAL_SEMANTIC_EVIDENCE_PREPARED":
            raise ValueError("LOCAL_SEMANTIC_NO_QUALIFIED_EVIDENCE")
        async def _infer():
            async with MeshLLMClient() as client:
                return await infer_semantic_hypotheses(prepared, client)
        reviewed = asyncio.run(_infer())
        reviewed["evidence_sha256"] = prepared_fingerprint(prepared)
        save_semantic_review(reviewed, output)
    except Exception as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_SEMANTIC_REVIEW_BLOCKED",
            "ready": False, "production_ready": False,
            "publication_performed": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(reviewed, ensure_ascii=False, sort_keys=True))


@app.command("render-source-queue-enqueue")
def render_source_queue_enqueue_command(
    timeline: Path = typer.Option(..., "--timeline"),
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    database: Path = typer.Option(Path("data/holoclip.db"), "--database"),
    cache_root: Path = typer.Option(Path("data/sources"), "--cache-root"),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl"), "--policy-file"),
    max_duration_seconds: int = typer.Option(14400, "--max-duration-seconds", min=60, max=14400),
    max_filesize_mb: int = typer.Option(512, "--max-filesize-mb", min=64, max=1024),
    apply: bool = typer.Option(False, "--apply",
        help="Store only the approved local timeline and paths in the durable queue."),
    approve_local_preview: bool = typer.Option(False, "--approve-local-preview",
        help="Operator authorizes unattended local preview acquisitions, not publishing."),
) -> None:
    """Preview missing sources; opt in to persist this measured timeline once."""
    try:
        if not timeline.is_file() or timeline.stat().st_size > 1_048_576:
            raise ValueError("LOCAL_RENDER_QUEUE_TIMELINE_UNAVAILABLE")
        parsed = ClipTimeline.model_validate_json(timeline.read_text(encoding="utf-8"))
        result = enqueue_render_timeline(
            parsed, queue=queue, database=database, cache_root=cache_root,
            policy_file=policy_file, max_duration_seconds=max_duration_seconds,
            max_filesize_mb=max_filesize_mb, apply=apply,
            approve_local_preview=approve_local_preview,
        )
    except (ValueError, OSError, UnicodeError, TypeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_QUEUE_ENROLLMENT_BLOCKED",
            "reason": (str(exc) if str(exc).startswith("LOCAL_RENDER_")
                       else "LOCAL_RENDER_QUEUE_INPUT_UNTRUSTED"),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@app.command("render-source-queue-status")
def render_source_queue_status_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
) -> None:
    """Read-only local queue counts and next state; never contacts YouTube."""
    try:
        result = inspect_render_queue(queue=queue)
    except (ValueError, OSError, TypeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_QUEUE_STATUS_UNTRUSTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("render-source-queue-run")
def render_source_queue_run_command(
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    execute: bool = typer.Option(False, "--execute",
        help="Attempt at most one approved missing video via existing LAN/local yt-dlp pacing."),
) -> None:
    """Single bounded durable worker iteration; safe for scheduled invocation."""
    if execute:
        _load_env_file()
    try:
        result = advance_render_queue(queue=queue, execute=execute)
    except (ValueError, OSError, TypeError, UnicodeError, sqlite3.Error,
            YtDlpError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_QUEUE_WORKER_UNTRUSTED",
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, sort_keys=True))
    if result["status"] in {
        "LOCAL_RENDER_QUEUE_STOPPED_UNCERTAIN_OR_PAUSED",
        "LOCAL_RENDER_QUEUE_PAUSED_REMOTE_OR_UNCERTAIN",
        "LOCAL_RENDER_QUEUE_PAUSED_UNKNOWN_RESULT",
        "LOCAL_RENDER_QUEUE_PAUSED_VALIDATION",
    }:
        raise typer.Exit(6)


@app.command("render-source-queue-reconcile")
def render_source_queue_reconcile_command(
    job_id: str = typer.Option(..., "--job-id"),
    queue: Path = typer.Option(DEFAULT_QUEUE, "--queue"),
    apply: bool = typer.Option(False, "--apply"),
    resume: bool = typer.Option(False, "--resume",
        help="On a previously PAUSED job only, explicitly re-arm after investigation."),
) -> None:
    """Offline crash recovery; never contacts media or automatically retries."""
    try:
        result = reconcile_render_queue(
            queue=queue, job_id=job_id, apply=apply, resume=resume,
        )
    except (ValueError, OSError, TypeError, UnicodeError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_QUEUE_RECOVERY_BLOCKED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("render-local-source-acquire")
def render_local_source_acquire_command(
    timeline: Path = typer.Option(..., "--timeline",
        help="Existing measured ClipTimeline JSON, no remote URLs."),
    database: Path = typer.Option(Path("data/holoclip.db"), "--database",
        help="Existing canonical SQLite asset repository, read only."),
    cache_root: Path = typer.Option(Path("data/sources"), "--cache-root",
        help="Existing local video cache root; no downloads in preview."),
    policy_file: Path = typer.Option(
        Path("data/policy/clip_rules.jsonl"), "--policy-file"),
    max_duration_seconds: int = typer.Option(
        14400, "--max-duration-seconds", min=60, max=14400),
    max_filesize_mb: int = typer.Option(
        512, "--max-filesize-mb", min=64, max=1024),
    execute: bool = typer.Option(False, "--execute",
        help="Attempt one bounded source download through shared LAN/local pacing."),
    approve_local_preview: bool = typer.Option(
        False, "--approve-local-preview",
        help="Explicit permission assertion for local preview only; not publishing."),
) -> None:
    """Show missing full source videos offline, or acquire at most one if approved."""
    try:
        if not timeline.is_file() or timeline.stat().st_size > 1_048_576:
            raise ValueError("LOCAL_RENDER_TIMELINE_UNAVAILABLE")
        measured = ClipTimeline.model_validate_json(
            timeline.read_text(encoding="utf-8")
        )
        report = acquire_one_missing_render_source(
            measured, database=database, cache_root=cache_root,
            policy_file=policy_file,
            max_duration_seconds=max_duration_seconds,
            max_filesize_mb=max_filesize_mb, execute=execute,
            approve_local_preview=approve_local_preview,
        )
    except (ValueError, OSError, UnicodeError, TypeError,
            YtDlpError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_SOURCE_ACQUISITION_BLOCKED",
            "reason": (str(exc) if str(exc).startswith("LOCAL_RENDER_")
                       else "LOCAL_RENDER_SOURCE_ACQUISITION_FAILED"),
            "ready": False, "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))


@app.command("render-local-preview")
def render_local_preview_command(
    timeline: Path = typer.Option(..., "--timeline",
        help="Local validated ClipTimeline JSON; no URLs."),
    sources: Path | None = typer.Option(None, "--sources",
        help="Existing explicit JSON source mappings (default resolver alternative)."),
    output: Path = typer.Option(..., "--output",
        help="New local .mp4; existing files are never overwritten."),
    auto_sources: bool = typer.Option(False, "--auto-sources",
        help="Resolve full yt-dlp editing-video caches from the canonical repository."),
    database: Path = typer.Option(Path("data/holoclip.db"), "--database",
        help="Existing canonical read-only SQLite source asset repository."),
    cache_root: Path = typer.Option(Path("data/sources"), "--cache-root",
        help="Root of prior original editing_video source caches; never downloads."),
    approve_local_preview: bool = typer.Option(False, "--approve-local-preview",
        help="Explicit permission assertion for cached source local preview only."),
    execute: bool = typer.Option(False, "--execute",
        help="Explicitly run local FFmpeg after plan validation and input probes."),
) -> None:
    """Prepare or execute deterministic video+audio multi-source clip assembly."""
    try:
        if (auto_sources and sources is not None) or (not auto_sources and sources is None):
            raise ValueError("LOCAL_RENDER_SELECT_EXACTLY_ONE_SOURCE_MODE")
        if not timeline.is_file():
            raise ValueError("LOCAL_RENDER_MANIFEST_MISSING")
        if timeline.stat().st_size > 1_048_576:
            raise ValueError("LOCAL_RENDER_MANIFEST_OVERSIZE")
        measured_timeline = ClipTimeline.model_validate_json(
            timeline.read_text(encoding="utf-8")
        )
        if auto_sources:
            resolved = resolve_cached_render_sources(
                measured_timeline, database=database, cache_root=cache_root,
                approve_local_preview=approve_local_preview,
            )
        else:
            if not sources.is_file():
                raise ValueError("LOCAL_RENDER_MANIFEST_MISSING")
            if sources.stat().st_size > 131_072:
                raise ValueError("LOCAL_RENDER_MANIFEST_OVERSIZE")
            mapped = json.loads(sources.read_text(encoding="utf-8"))
            if not isinstance(mapped, list) or not 1 <= len(mapped) <= 32:
                raise ValueError("LOCAL_RENDER_MANIFEST_INVALID")
            resolved = [
                LocalRenderSource.model_validate(row) for row in mapped
            ]
        plan = compile_local_render_plan(measured_timeline, resolved, output)
        if execute:
            report = render_local_preview(plan)
        else:
            report = {
                "status": "LOCAL_RENDER_PLAN_VALIDATED_NOT_EXECUTED",
                "plan_id": plan.id,
                "source_count": len(plan.sources),
                "segment_count": len(plan.timeline.segments),
                "expected_duration": plan.expected_duration,
                "video_width": plan.video_width,
                "video_height": plan.video_height,
                "audio_required": True,
                "output_created": False,
                "youtube_contacted": False,
                "publication_performed": False,
                "ready": False,
                "production_ready": False,
            }
    except (ValueError, OSError, UnicodeError, TypeError, KeyError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_RENDER_BLOCKED",
            "reason": (str(exc) if str(exc).startswith("LOCAL_RENDER_")
                       else "LOCAL_RENDER_INVALID_SOURCE_OR_TIMELINE"),
            "ready": False, "production_ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))


@app.command("examples-ytdlp-lan-first-run-check")
def examples_ytdlp_lan_first_run_check(
    profile_file: Path = typer.Option(DEFAULT_OPERATOR_PROFILE, "--profile-file",
        help="Previously saved PC-A operator profile; no repeated IP or SSH arguments."),
    check_remote: bool = typer.Option(False, "--check-remote",
        help="Also check PC-B using already pinned SSH and signed status; no media."),
) -> None:
    """Read-only first-run audit: exact task, last run and fresh signed history."""
    _load_env_file()
    try:
        report = inspect_lan_first_run(
            profile_file=profile_file, check_remote=check_remote,
        )
    except (ValueError, OSError, UnicodeError, TypeError, sqlite3.Error,
            LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_FIRST_RUN_CHECK_REJECTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))
    if report["status"] not in {
        "LAN_FIRST_RUN_LOCAL_MONITOR_OBSERVED",
        "LAN_FIRST_RUN_LOCAL_AND_PEER_OBSERVED",
    } or (check_remote and report["signed_peer"] != "LAN_FIRST_RUN_SIGNED_PEER_OBSERVED"):
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-operator-setup")
def examples_ytdlp_lan_operator_setup(
    host: str = typer.Option(..., "--host", help="PC-B private IPv4."),
    user: str = typer.Option(..., "--user", help="Existing PC-B SSH account."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Independently pinned PC-B known_hosts file."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="Absolute PC-B Windows Holoclip repository directory."),
    profile_file: Path = typer.Option(DEFAULT_OPERATOR_PROFILE, "--profile-file"),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    interval_minutes: int = typer.Option(60, "--interval-minutes", min=15, max=1440),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Monitor", "--task-name"),
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db"),
    alert_file: Path = typer.Option(DEFAULT_MONITOR_ALERT, "--alert-file"),
    apply: bool = typer.Option(False, "--apply",
        help="Save a no-secret profile on PC-A, never altering SSH trust."),
    update: bool = typer.Option(False, "--update",
        help="With --apply, explicitly replace profile only if both task names absent."),
    register_monitor: bool = typer.Option(False, "--register-monitor",
        help="With --apply, also register (never start) the safe Windows monitor task."),
) -> None:
    """One-time parameter entry, preview-first reusable Windows operator profile."""
    _load_env_file()
    try:
        report = prepare_operator_profile(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, profile_file=profile_file,
            ssh_port=ssh_port, coordinator_port=coordinator_port,
            interval_minutes=interval_minutes, task_name=task_name,
            evidence_db=evidence_db, alert_file=alert_file,
            apply=apply, update=update, register_monitor=register_monitor,
        )
    except (ValueError, OSError, UnicodeError, TypeError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_OPERATOR_SETUP_REJECTED",
            "reason": (str(exc) if str(exc).startswith("LAN_OPERATOR_")
                       else "LAN_OPERATOR_PREFLIGHT_UNVERIFIED"),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_OPERATOR_PREVIEW_ELIGIBLE", "LAN_OPERATOR_PREPARED_NOT_STARTED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-operator-status")
def examples_ytdlp_lan_operator_status(
    profile_file: Path = typer.Option(DEFAULT_OPERATOR_PROFILE, "--profile-file"),
    diagnose: bool = typer.Option(False, "--diagnose",
        help="Opt in to pinned SSH and signed two-PC diagnosis."),
    register_monitor: bool = typer.Option(False, "--register-monitor",
        help="Opt in to registering the previously validated monitor task."),
) -> None:
    """Default offline local status; optional peer diagnosis or task install."""
    _load_env_file()
    try:
        report = operator_status(
            profile_file=profile_file, diagnose=diagnose,
            register_monitor=register_monitor,
        )
    except (ValueError, OSError, UnicodeError, TypeError, sqlite3.Error,
            LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_OPERATOR_STATUS_REJECTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_OPERATOR_LOCAL_STATUS", "LAN_OPERATOR_DIAGNOSIS_OBSERVED",
        "LAN_OPERATOR_MONITOR_REGISTERED_NOT_STARTED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-monitor-run")
def examples_ytdlp_lan_monitor_run(
    host: str = typer.Option(..., "--host"),
    user: str = typer.Option(..., "--user"),
    known_hosts: Path = typer.Option(..., "--known-hosts"),
    remote_repo: str = typer.Option(..., "--remote-repo"),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db"),
    alert_file: Path = typer.Option(DEFAULT_MONITOR_ALERT, "--alert-file"),
) -> None:
    """Record one diagnosis and optionally notify persistent repeated issues."""
    _load_env_file()
    try:
        report = run_lan_monitor_once(
            host=host, user=user, known_hosts=known_hosts, remote_repo=remote_repo,
            ssh_port=ssh_port, coordinator_port=coordinator_port,
            task_name=task_name, evidence_db=evidence_db, alert_file=alert_file,
        )
    except (ValueError, OSError, UnicodeError, sqlite3.Error,
            LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_MONITOR_RUN_REJECTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    # Even a negative observation was successfully recorded. The scheduler
    # must not retry an incomplete setup as a failed *monitor process*.


@app.command("examples-ytdlp-lan-monitor-alert")
def examples_ytdlp_lan_monitor_alert(
    alert_file: Path = typer.Option(DEFAULT_MONITOR_ALERT, "--alert-file"),
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db"),
) -> None:
    """Read one durable safe-code Windows LAN monitor alert completely offline."""
    try:
        report = read_lan_monitor_alert(alert_file=alert_file, evidence_db=evidence_db)
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(json.dumps({"status": "LAN_MONITOR_ALERT_REJECTED", "ready": False}))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))


@app.command("examples-ytdlp-lan-monitor-task-prepare")
def examples_ytdlp_lan_monitor_task_prepare(
    host: str = typer.Option(..., "--host", help="PC-B private LAN IPv4."),
    user: str = typer.Option(..., "--user", help="PC-B existing SSH account."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Independently verified PC-B known_hosts file."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="Absolute Windows PC-B Holoclip repository path."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    interval_minutes: int = typer.Option(60, "--interval-minutes", min=15, max=1440),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Monitor", "--task-name"),
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db"),
    alert_file: Path = typer.Option(DEFAULT_MONITOR_ALERT, "--alert-file"),
    apply: bool = typer.Option(False, "--apply",
        help="Register a non-elevated Windows Task Scheduler job; never auto-start."),
) -> None:
    """Preview-first, non-overwriting, no-media Windows monitor task registration."""
    _load_env_file()
    try:
        report = prepare_lan_monitor_task(
            host=host, user=user, known_hosts=known_hosts, remote_repo=remote_repo,
            ssh_port=ssh_port, coordinator_port=coordinator_port,
            interval_minutes=interval_minutes, task_name=task_name,
            evidence_db=evidence_db, alert_file=alert_file, apply=apply,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_MONITOR_TASK_REJECTED", "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_MONITOR_TASK_PREVIEW_ELIGIBLE",
        "LAN_MONITOR_TASK_REGISTERED_NOT_STARTED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-evidence-capture")
def examples_ytdlp_lan_evidence_capture(
    host: str = typer.Option(..., "--host", help="PC-B private IPv4."),
    user: str = typer.Option(..., "--user", help="PC-B SSH key-only login."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Independently pinned PC-B known_hosts file."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="Absolute PC-B Windows Holoclip repository directory."),
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db",
        help="Local, Git-ignored, code-only SQLite evidence history."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
    record: bool = typer.Option(False, "--record",
        help="Opt in to writing one sanitized local observation."),
    include_pacing_trial: bool = typer.Option(False, "--include-pacing-trial",
        help="With --record only: opt in to a live lease/gap trial; no media."),
) -> None:
    """Capture safe LAN evidence; default has no persistence or media lease."""
    _load_env_file()
    try:
        result = capture_lan_evidence(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, evidence_db=evidence_db,
            ssh_port=ssh_port, coordinator_port=coordinator_port,
            task_name=task_name, record=record,
            include_pacing_trial=include_pacing_trial,
        )
    except (ValueError, OSError, UnicodeError, sqlite3.Error,
            LocalYtDlpDeferred) as exc:
        code = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_EVIDENCE_CAPTURE_REJECTED",
            "reason": code if code.startswith("LAN_EVIDENCE_") else type(exc).__name__,
            "recording_unverified": bool(record),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["diagnosis"] != "LAN_DEPLOYMENT_OBSERVATIONS_MATCH" or (
        include_pacing_trial
        and result["pacing_trial"] != "LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED"
    ):
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-evidence-history")
def examples_ytdlp_lan_evidence_history(
    evidence_db: Path = typer.Option(DEFAULT_EVIDENCE_DB, "--evidence-db"),
) -> None:
    """Offline code-only LAN evidence history; no SSH or coordinator calls."""
    try:
        report = read_lan_evidence_history(evidence_db)
    except (ValueError, OSError, sqlite3.Error) as exc:
        typer.echo(json.dumps({
            "status": "LAN_EVIDENCE_HISTORY_REJECTED",
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))


@app.command("examples-ytdlp-lan-deployment-diagnose")
def examples_ytdlp_lan_deployment_diagnose(
    host: str = typer.Option(..., "--host", help="PC-B private IPv4."),
    user: str = typer.Option(..., "--user", help="PC-B SSH public-key account."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Dedicated PC-B SSH key file verified out of band."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="Absolute PC-B Holoclip Windows repository directory."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
) -> None:
    """Read-only prioritized two-PC diagnosis; retain PC-A evidence on SSH errors."""
    _load_env_file()
    try:
        report = diagnose_lan_deployment(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
            coordinator_port=coordinator_port, task_name=task_name,
        )
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        code = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_DEPLOYMENT_DIAGNOSIS_REJECTED",
            "reason": (code if code.startswith("LAN_DEPLOYMENT_")
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, ensure_ascii=False))
    if report["status"] != "LAN_DEPLOYMENT_OBSERVATIONS_MATCH":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-onboard-check")
def examples_ytdlp_lan_onboard_check(
    host: str = typer.Option(..., "--host", help="PC-B private IPv4, not localhost."),
    user: str = typer.Option(..., "--user", help="PC-B SSH public-key login."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="PC-B SSH host key file verified independently out of band."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="PC-B absolute Holoclip Windows repository path."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
) -> None:
    """Check PC-A/B setup and signed coordinator consistency without media."""
    _load_env_file()
    try:
        report = check_lan_onboarding(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
            coordinator_port=coordinator_port, task_name=task_name,
        )
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        code = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_ONBOARD_CHECK_REJECTED",
            "reason": (code if code.startswith(("LAN_ONBOARD_", "LAN_PAIR_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "LAN_ONBOARD_OBSERVATIONS_COMPLETE":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-pair-observe")
def examples_ytdlp_lan_pair_observe(
    role: str = typer.Option(..., "--role", help="PC label: pc-a or pc-b."),
) -> None:
    """Emit fresh signed status to stdout; never create a report file."""
    _load_env_file()
    try:
        report = observe_lan_pair_evidence(role=role)
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_PAIR_OBSERVE_REJECTED",
            "reason": (str(exc) if str(exc).startswith(("LAN_PAIR_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))


@app.command("examples-ytdlp-lan-pair-collect")
def examples_ytdlp_lan_pair_collect(
    host: str = typer.Option(..., "--host", help="PC-B private IPv4."),
    user: str = typer.Option(..., "--user", help="PC-B SSH username."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="Independently preverified known_hosts with PC-B's pinned SSH host key."),
    remote_repo: str = typer.Option(..., "--remote-repo",
        help="PC-B Holoclip Windows directory, e.g. D:\\AI\\holoclip."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
) -> None:
    """One-command two-PC status collect over pinned SSH, no credential files."""
    _load_env_file()
    try:
        result = collect_lan_pair_over_ssh(
            host=host, user=user, known_hosts=known_hosts,
            remote_repo=remote_repo, ssh_port=ssh_port,
        )
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_PAIR_COLLECTION_REJECTED",
            "reason": (str(exc) if str(exc).startswith(("LAN_PAIR_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "LAN_PAIR_OBSERVATIONS_MATCH":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-pair-export")
def examples_ytdlp_lan_pair_export(
    role: str = typer.Option(..., "--role", help="PC label: pc-a or pc-b."),
    output: Path = typer.Option(..., "--output",
        help="Local signed report destination. Existing reports are never overwritten."),
) -> None:
    """Export signed per-PC coordinator observation without yt-dlp or lease."""
    _load_env_file()
    try:
        report = export_lan_pair_evidence(role=role, output=output)
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_PAIR_EXPORT_REJECTED",
            "reason": (str(exc) if str(exc).startswith(("LAN_PAIR_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report))


@app.command("examples-ytdlp-lan-pair-verify")
def examples_ytdlp_lan_pair_verify(
    pc_a: Path = typer.Option(..., "--pc-a", help="Signed PC-A report file."),
    pc_b: Path = typer.Option(..., "--pc-b", help="Signed PC-B report file."),
) -> None:
    """Compare signed observations offline; not physical host attestation."""
    _load_env_file()
    try:
        report = compare_lan_pair_evidence(pc_a=pc_a, pc_b=pc_b)
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        typer.echo(json.dumps({
            "status": "LAN_PAIR_VERIFY_REJECTED",
            "reason": (str(exc) if str(exc).startswith(("LAN_PAIR_", "LOCAL_YTDLP_"))
                       else type(exc).__name__),
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report))
    if report["status"] != "LAN_PAIR_OBSERVATIONS_MATCH":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-pc-b-prepare")
def examples_ytdlp_lan_pc_b_prepare(
    coordinator_ip: str = typer.Option(..., "--coordinator-ip",
        help="PC-A coordinator and pinned SFTP server private IPv4."),
    ssh_user: str = typer.Option(..., "--ssh-user",
        help="Existing PC-A public-key SSH account."),
    remote_path: str = typer.Option(..., "--remote-path",
        help="Existing PC-A secret absolute SFTP path, with forward slashes."),
    known_hosts: Path = typer.Option(..., "--known-hosts",
        help="PC-A SSH host key file already independently verified."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    coordinator_port: int = typer.Option(8878, "--coordinator-port",
        min=1024, max=65535),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    config_file: Path = typer.Option(DEFAULT_CLIENT_CONFIG, "--config-file"),
    apply: bool = typer.Option(False, "--apply",
        help="Fetch pinned SFTP credential and configure PC-B, then check signed status."),
) -> None:
    """Preview without network/writes; apply never trusts unknown SSH host keys."""
    _load_env_file()
    try:
        report = prepare_pc_b_lan(
            coordinator_ip=coordinator_ip, ssh_user=ssh_user,
            remote_path=remote_path, known_hosts=known_hosts,
            ssh_port=ssh_port, coordinator_port=coordinator_port,
            secret_file=secret_file, config_file=config_file, apply=apply,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        code = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_PC_B_PREPARE_REJECTED",
            "reason": code if code.startswith("LAN_PC_B_") else type(exc).__name__,
            "changes_may_be_partial": apply,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_PC_B_PREVIEW_ELIGIBLE", "LAN_PC_B_PREPARED_AUTHENTICATED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-pc-a-prepare")
def examples_ytdlp_lan_pc_a_prepare(
    bind_ip: str = typer.Option(..., "--bind-ip", help="PC-A private LAN IPv4."),
    peer_ip: str = typer.Option(..., "--peer-ip", help="PC-B private LAN IPv4."),
    port: int = typer.Option(8878, "--port", min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    config_file: Path = typer.Option(DEFAULT_CLIENT_CONFIG, "--config-file"),
    state_path: Path = typer.Option(DEFAULT_COORDINATOR_STATE, "--state-path"),
    apply: bool = typer.Option(False, "--apply",
        help="Prepare local credential, exact-peer firewall and task; never start task."),
) -> None:
    """One PC-A command: full no-write preview, or guarded ordered setup."""
    try:
        report = prepare_pc_a_lan(
            bind_ip=bind_ip, peer_ip=peer_ip, port=port,
            task_name=task_name, secret_file=secret_file,
            config_file=config_file, state_path=state_path, apply=apply,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        code = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_PC_A_PREPARE_REJECTED",
            "reason": code if code.startswith("LAN_PC_A_") else type(exc).__name__,
            "changes_may_be_partial": apply,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_PC_A_PREVIEW_ELIGIBLE", "LAN_PC_A_PREPARED_NOT_STARTED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-firewall-prepare")
def examples_ytdlp_lan_firewall_prepare(
    bind_ip: str = typer.Option(..., "--bind-ip", help="PC-A's local RFC1918 IPv4."),
    peer_ip: str = typer.Option(..., "--peer-ip", help="Only permitted PC-B RFC1918 IPv4."),
    port: int = typer.Option(8878, "--port", min=1024, max=65535),
    apply: bool = typer.Option(False, "--apply", help="Explicitly add one exact rule on PC-A."),
) -> None:
    """Preview by default; never overwrite or delete Windows Firewall rules."""
    try:
        report = prepare_lan_firewall(
            bind_ip=bind_ip, peer_ip=peer_ip, port=port, apply=apply,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_FIREWALL_REJECTED",
            "reason": (str(exc) if str(exc).startswith("LAN_FIREWALL_")
                       else type(exc).__name__),
            "changes_may_be_partial": apply,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] not in {
        "LAN_FIREWALL_PREVIEW_ELIGIBLE", "LAN_FIREWALL_ALREADY_SCOPED",
        "LAN_FIREWALL_RULE_ADDED",
    }:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-task-prepare")
def examples_ytdlp_lan_task_prepare(
    bind_ip: str = typer.Option(..., "--bind-ip", help="PC-A LAN listener IPv4."),
    peer_ip: str = typer.Option(..., "--peer-ip", help="Only allowed PC-B LAN IPv4."),
    port: int = typer.Option(8878, "--port", min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    state_path: Path = typer.Option(DEFAULT_COORDINATOR_STATE, "--state-path"),
    apply: bool = typer.Option(False, "--apply",
        help="Register only when existing secret ACL, narrow firewall and task absence pass."),
) -> None:
    """Preview by default; optionally register PC-A task without starting it."""
    try:
        result = inspect_lan_task_registration(
            bind_ip=bind_ip, peer_ip=peer_ip, port=port, task_name=task_name,
            secret_file=secret_file, state_path=state_path, apply=apply,
        )
    except (OSError, ValueError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LAN_TASK_PREPARE_REJECTED",
            "reason": str(exc) if str(exc).startswith("LAN_TASK_") else type(exc).__name__,
            "task_registration_unconfirmed": apply,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(result, sort_keys=True))
    if result["status"] in {"LAN_TASK_APPLY_BLOCKED", "LAN_TASK_PREVIEW_BLOCKED"}:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-bootstrap-local")
def examples_ytdlp_lan_bootstrap_local(
    role: str = typer.Option(..., "--role", help="pc-a or pc-b on this Windows PC."),
    coordinator_ip: str = typer.Option(..., "--coordinator-ip",
        help="PC-A's explicit RFC1918 LAN IPv4; never use an internet IP."),
    coordinator_port: int = typer.Option(8878, "--coordinator-port", min=1024, max=65535),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    config_file: Path = typer.Option(DEFAULT_CLIENT_CONFIG, "--config-file"),
    apply: bool = typer.Option(False, "--apply",
        help="Opt in to new PC-A secret, secure NTFS ACL and client profile writes."),
) -> None:
    """Preview first; optionally initialize only safe local LAN client state."""
    try:
        report = bootstrap_local_lan(
            role=role, coordinator_ip=coordinator_ip,
            secret_file=secret_file, config_file=config_file,
            coordinator_port=coordinator_port, apply=apply,
        )
    except (ValueError, OSError, UnicodeError, LocalYtDlpDeferred) as exc:
        reason = str(exc)
        typer.echo(json.dumps({
            "status": "LAN_BOOTSTRAP_REJECTED",
            "reason": (reason if reason.startswith("LAN_BOOTSTRAP_")
                       else type(exc).__name__),
            "changes_may_be_partial": apply,
            "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, sort_keys=True))
    if report["status"] == "LAN_BOOTSTRAP_TRUSTED_CREDENTIAL_REQUIRED":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-setup-doctor")
def examples_ytdlp_lan_setup_doctor(
    role: str = typer.Option(..., "--role",
        help="Specify which machine is being inspected: pc-a or pc-b."),
    secret_file: Path | None = typer.Option(None, "--secret-file",
        help="Optional local credential path (defaults to saved client path on PC-B)."),
    peer_ip: str | None = typer.Option(None, "--peer-ip",
        help="PC-B private IPv4 used to check PC-A firewall scope."),
    known_hosts: Path | None = typer.Option(None, "--known-hosts",
        help="Independently verified local SSH known_hosts file; never fetched."),
    ssh_host: str | None = typer.Option(None, "--ssh-host",
        help="PC-A private IPv4 to look up in known_hosts; no SSH connection."),
    ssh_port: int = typer.Option(22, "--ssh-port", min=1, max=65535),
    port: int = typer.Option(8878, "--port", min=1024, max=65535),
    task_name: str = typer.Option("Holoclip yt-dlp LAN Coordinator", "--task-name"),
) -> None:
    """Read-only PC-A/B deployment preflight; no media or task modifications."""
    _load_env_file()
    try:
        report = inspect_lan_setup(
            role=role, secret_file=secret_file, peer_ip=peer_ip,
            known_hosts=known_hosts, ssh_host=ssh_host,
            ssh_port=ssh_port, port=port, task_name=task_name,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_SETUP_DOCTOR_ARGUMENT_REJECTED",
            "reason": type(exc).__name__, "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "LOCAL_SETUP_OBSERVATIONS_COMPLETE":
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-doctor")
def examples_ytdlp_lan_doctor() -> None:
    """Authenticate to the coordinator without yt-dlp or a lease."""
    _load_env_file()
    report = diagnose_lan_client()
    typer.echo(json.dumps(report))
    if not report["authenticated"]:
        raise typer.Exit(6)


@app.command("examples-ytdlp-lan-configure")
def examples_ytdlp_lan_configure(
    url: str = typer.Option(..., "--url",
        help="PC-A private IPv4 URL including port, never a public endpoint."),
    secret_file: Path = typer.Option(..., "--secret-file",
        help="Existing local copy of the shared LAN credential; never pass secret bytes."),
    config_file: Path = typer.Option(DEFAULT_CLIENT_CONFIG, "--config-file",
        help="Local-only JSON configuration path (default ignored by Git)."),
    replace: bool = typer.Option(False, "--replace",
        help="Explicitly replace a different saved LAN client profile."),
) -> None:
    """Create private local connection settings without calling YouTube."""
    try:
        status = configure_lan_client(
            url=url, secret_file=secret_file,
            config_path=config_file, replace=replace,
        )
    except (OSError, ValueError, UnicodeError) as exc:
        typer.echo(json.dumps({
            "status": "LOCAL_LAN_CLIENT_CONFIGURATION_REJECTED",
            "reason": type(exc).__name__, "ready": False,
        }))
        raise typer.Exit(6) from exc
    typer.echo(json.dumps({
        "status": status,
        "credential_bytes_copied": False, "remote_authentication_verified": False,
        "ready": False,
    }))


@app.command("examples-ytdlp-lan-serve")
def examples_ytdlp_lan_serve(
    bind_ip: str = typer.Option("127.0.0.1", "--bind-ip"),
    port: int = typer.Option(8878, "--port", min=1024, max=65535),
    secret_file: Path = typer.Option(DEFAULT_SECRET_PATH, "--secret-file"),
    state_path: Path = typer.Option(DEFAULT_COORDINATOR_STATE, "--state-path"),
) -> None:
    """Run opt-in single-host coordinator on a trusted private LAN IP."""
    serve_lan_ytdlp_coordinator(
        bind_ip=bind_ip, port=port,
        secret_file=secret_file, state_path=state_path,
    )


@app.command("examples-ytdlp-lan-status")
def examples_ytdlp_lan_status() -> None:
    """Legacy alias of LAN doctor: signed status only, never YouTube."""
    examples_ytdlp_lan_doctor()


@app.command("examples-source-controls-report")
def examples_source_controls_report(
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
) -> None:
    """Summarize real acquired-video constructed controls, never editor accuracy."""
    report = report_saved_source_controls(state)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("examples-collect-source-patterns")
def examples_collect_source_patterns(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    output: Path = typer.Option(Path("data/sources/source_visual_scout")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    cycles: int = typer.Option(3, min=1, max=20),
    max_clips: int = typer.Option(2, min=1, max=2),
    max_candidates: int = typer.Option(2, min=1, max=2),
    scan_limit: int = typer.Option(20, min=1, max=100),
    target_distinct_streams: int = typer.Option(3, min=1, max=50),
    discover: bool = typer.Option(True, "--discover/--no-discover"),
    report_path: Path = typer.Option(Path("data/examples/source-pattern-last-run.json")),
) -> None:
    """Discover and measure bounded public source-video controls; no gold claims."""
    _load_env_file()
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    report = asyncio.run(run_locked_source_pattern_collection(
        provider, store=store, state_path=state, output=output,
        policy_rules=load_policy_rules(policy_file), cycles=cycles,
        max_clips=max_clips, max_candidates=max_candidates,
        scan_limit=scan_limit, target_distinct_streams=target_distinct_streams,
        discover=discover, report_path=report_path, progress=typer.echo,
    ))
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    # A skipped scheduled run must not appear as an ordinary completed task.
    if report.get("status") in {
        "BLOCKED_CONCURRENT_COLLECTION",
        "BLOCKED_LOCAL_YTDLP_PACING_STATE",
    }:
        raise typer.Exit(6)


@app.command("examples-verify-source-visual")
def examples_verify_source_visual(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    output: Path = typer.Option(Path("data/sources/source_visual_scout")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    max_clips: int = typer.Option(1, min=1, max=2),
    max_candidates: int = typer.Option(2, min=1, max=2),
) -> None:
    """Probe pending public source videos visually; never certify source identity."""
    _load_env_file()
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    with _state_lock(state) as acquired:
        if not acquired:
            _blocked_corpus_state_writer(state)
        report = verify_pending_source_visual(
            provider, store=store, state_path=state, output=output,
            policy_rules=load_policy_rules(policy_file), max_clips=max_clips,
            max_candidates=max_candidates, progress=typer.echo,
        )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def examples_grow_corpus(
    store: Path = typer.Option(Path("data/examples/human_clips.jsonl")),
    state: Path = typer.Option(Path("data/examples/corpus_growth.json")),
    database: Path = typer.Option(Path("data/holoclip.db")),
    profile: Path = typer.Option(Path("data/examples/editorial_profile.json")),
    policy_file: Path = typer.Option(Path("data/policy/clip_rules.jsonl")),
    scan_limit: int = typer.Option(20, min=1, max=100),
    max_new_examples: int = typer.Option(3, min=1, max=100),
    target_trusted: int = typer.Option(30, min=30),
    target_streams: int = typer.Option(15, min=15),
    discover_only: bool = typer.Option(False),
    cycles: int = typer.Option(1, min=1, max=100),
) -> None:
    """Grow learning corpus and reserve new streams for independent evaluation."""
    _load_env_file()
    settings = RoutingSettings.from_env()
    groq = GroqTranscriptionSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()
    provider = YtDlpSourceProvider(channel=os.getenv("HOLOCLIP_YTDLP_CHANNEL", "nightly"))
    rules = load_policy_rules(policy_file)

    async def run():
        with SQLiteCanonicalRepository(database) as repository:
            cache = StageArtifactCache(repository, Path("data/artifacts/stages"))
            if discover_only:
                async def unused(**kwargs):
                    raise AssertionError("metadata-only discovery cannot acquire audio")
                return await grow_corpus(provider, store=store, state_path=state, ingest=unused,
                    policy_rules=rules, scan_limit=scan_limit, max_new_examples=max_new_examples,
                    target_trusted=target_trusted, target_streams=target_streams, discover_only=True,
                    progress=typer.echo)
            async with (MeshLLMClient(settings, event_callback=typer.echo) as llm,
                        MeshEmbeddingClient(embedding_settings) as embedding):
                async def ingest(**kwargs):
                    return await ingest_human_example_from_urls(provider, **kwargs,
                        download_root=Path("data/sources/examples"), transcript_root=Path("data/transcripts"),
                        examples_store=store, groq_settings=groq, llm=llm, llm_model=settings.model,
                        llm_reasoning_effort=settings.strong_reasoning_effort, embedding_provider=embedding,
                        repository=repository, stage_cache=cache, policy_rules=rules,
                        allow_policy_review=False, progress=typer.echo)
                for cycle in range(cycles):
                    typer.echo(f"Corpus growth cycle {cycle + 1}/{cycles}")
                    current_examples = load_human_clip_store(store)
                    current_stats = compute_human_clip_stats(current_examples)
                    trusted_streams = len({x.stream_id for x in current_examples
                        if x.stream_id and evaluate_human_example_learning_quality(x).decision is LearningQuality.TRUSTED})
                    # The corpus count floors are not sufficient learning readiness.
                    # Keep collecting within the configured cycle budget if pattern coverage
                    # or other model gates remain unmet after reaching the count targets.
                    current_profile = _load_ready_editorial_profile(profile)
                    effective_target = target_trusted
                    if (current_stats.trusted_for_learning >= target_trusted
                            and trusted_streams >= target_streams
                            and (current_profile is None or not current_profile.readiness.ready)):
                        effective_target = current_stats.trusted_for_learning + max_new_examples
                    report = await grow_corpus(provider, store=store, state_path=state, ingest=ingest,
                        policy_rules=rules, scan_limit=scan_limit, max_new_examples=max_new_examples,
                        target_trusted=effective_target, target_streams=target_streams, progress=typer.echo)
                    if state.exists():
                        visual_report = verify_pending_source_visual(
                            provider, store=store, state_path=state,
                            output=Path("data/sources/source_visual_scout"),
                            policy_rules=rules, max_clips=1, max_candidates=2,
                            progress=typer.echo,
                        )
                        report["source_visual_verification"] = visual_report
                    try:
                        ready_profile = await _refresh_editorial_profile(
                            llm,
                            settings=settings,
                            stage_cache=cache,
                            path=profile,
                            store=store,
                        )
                    except (
                        NvidiaNimTimeoutError,
                        NvidiaNimAuthorizationError,
                        ValueError,
                    ) as exc:
                        typer.echo(
                            "Editorial profile refresh deferred during corpus growth; "
                            f"continuing acquisition within the configured cycle budget: {exc}"
                        )
                        ready_profile = None
                    if (report["trusted_examples"] >= target_trusted and report["trusted_streams"] >= target_streams
                            and ready_profile is not None):
                        break
                return report
    with _state_lock(state) as acquired:
        if not acquired:
            _blocked_corpus_state_writer(state)
        report = asyncio.run(run())
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


def _require_learning_ready_for_review(path: Path) -> None:
    if _load_ready_editorial_profile(path) is None:
        typer.echo("Editorial learning is not ready. Operator labels remain frozen validation data; "
                   "grow the trusted human corpus and run automated learning first.", err=True)
        raise typer.Exit(6)


@app.command()
def editorial_ab(
    transcript: Path = typer.Argument(..., exists=True),
    stream_id: str = typer.Option(...),
    profile: Path = typer.Option(Path("data/examples/editorial_profile.json")),
    output: Path = typer.Option(Path("data/evaluation/editorial_ab.json")),
    database: Path = typer.Option(Path("data/holoclip.db")),
    top_k: int = typer.Option(5, min=1, max=10),
) -> None:
    """Automatically compare profile ON/OFF on an unseen stream after readiness."""
    ready = _load_ready_editorial_profile(profile)
    if ready is None:
        typer.echo("Editorial learning readiness=false; ON/OFF evaluation is deferred.", err=True)
        raise typer.Exit(6)
    _load_env_file()
    settings = RoutingSettings.from_env()
    examples = load_human_clip_store(Path(ready.induction_config.get("corpus_store", str(profile.parent / "human_clips.jsonl"))))
    async def run():
        with SQLiteCanonicalRepository(database) as repository:
            cache = StageArtifactCache(repository, Path("data/artifacts/stages"))
            async with MeshLLMClient(settings, event_callback=typer.echo) as llm:
                return await run_editorial_ab(llm, transcript=transcript, stream_id=stream_id,
                    examples=examples, profile=ready, fast_model=settings.fast_model, strong_model=settings.model,
                    fast_reasoning_effort=settings.fast_reasoning_effort,
                    strong_reasoning_effort=settings.strong_reasoning_effort, stage_cache=cache, top_k=top_k)
    try:
        report = asyncio.run(run())
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(6) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"Saved automatic ON/OFF diagnostic: {output}")


@app.command()
def retrieval_ab(
    transcript: Path = typer.Argument(..., exists=True, readable=True),
    stream_id: str | None = typer.Option(None, "--stream-id"),
    top_k: int = typer.Option(5, "--top-k", min=1, max=20),
    window_minutes: float = typer.Option(
        10.0,
        "--window-minutes",
        min=2.0,
        max=30.0,
    ),
    overlap_minutes: float = typer.Option(
        2.0,
        "--overlap-minutes",
        min=0.0,
        max=10.0,
    ),
    positive_k: int = typer.Option(3, "--positive-k", min=0, max=20),
    negative_k: int = typer.Option(3, "--negative-k", min=0, max=20),
    final_temperature: float = typer.Option(
        0.0,
        "--final-temperature",
        min=0.0,
        max=2.0,
    ),
    output_root: Path = typer.Option(
        Path("data/evaluation/ab"),
        "--output-root",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing paired run for the same stream id.",
    ),
) -> None:
    """Run baseline vs retrieval-guided discovery/final-selection evaluation."""
    _load_env_file()
    nim_settings = RoutingSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()
    segments = load_transcript(transcript)
    resolved_stream_id = stream_id or transcript.stem

    baseline_path = output_root / "baseline" / f"{resolved_stream_id}.json"
    retrieval_path = output_root / "retrieval" / f"{resolved_stream_id}.json"
    blind_path = output_root / "blind" / f"{resolved_stream_id}.json"
    guided_only_path = (
        output_root / "guided-only" / f"{resolved_stream_id}.json"
    )
    manifest_path = output_root / "manifests" / f"{resolved_stream_id}.json"

    existing = [
        path
        for path in (
            baseline_path,
            retrieval_path,
            blind_path,
            guided_only_path,
            manifest_path,
        )
        if path.exists()
    ]
    if existing and not overwrite:
        typer.echo(
            "paired evaluation output already exists; "
            "use --overwrite to replace it:",
            err=True,
        )
        for path in existing:
            typer.echo(f"  {path}", err=True)
        raise typer.Exit(2)

    chunks = chunk_segments(
        segments,
        window_seconds=window_minutes * 60.0,
        overlap_seconds=overlap_minutes * 60.0,
    )

    async def _run(
        stage_cache: StageArtifactCache,
        repository: SQLiteCanonicalRepository,
    ) -> dict[str, object]:
        async with MeshLLMClient(
            nim_settings,
            event_callback=typer.echo,
        ) as llm:
            baseline_detector = CandidateDetector(
                llm,
                fast_model=nim_settings.fast_model,
                strong_model=nim_settings.model,
                fast_reasoning_effort=nim_settings.fast_reasoning_effort,
                strong_reasoning_effort=nim_settings.strong_reasoning_effort,
                progress=typer.echo,
                stage_cache=stage_cache,
                cache_provider="capability_mesh",
                final_temperature=final_temperature,
            )

            typer.echo(
                f"A/B broad discovery: {len(chunks)} transcript chunks "
                "(executed once for both variants)"
            )
            broad = await baseline_detector.discover_broad(chunks)
            pool_fingerprint = candidate_pool_fingerprint(broad)

            typer.echo("A/B baseline final review: retrieval OFF")
            baseline_candidates = await baseline_detector.final_select(
                broad,
                top_k=top_k,
            )

            async with MeshEmbeddingClient(
                embedding_settings
            ) as embedding:
                documents = repository.list_retrieval_documents(
                    embedding_model=embedding.model,
                    embedding_version=RETRIEVAL_VERSION,
                )
                if not documents:
                    raise RuntimeError(
                        "retrieval index is empty for the configured embedding model"
                    )
                corpus_fingerprint = retrieval_corpus_fingerprint(documents)

                retrieval_detector = CandidateDetector(
                    llm,
                    fast_model=nim_settings.fast_model,
                    strong_model=nim_settings.model,
                    fast_reasoning_effort=nim_settings.fast_reasoning_effort,
                    strong_reasoning_effort=nim_settings.strong_reasoning_effort,
                    progress=typer.echo,
                    stage_cache=stage_cache,
                    cache_provider="capability_mesh",
                    embedding_provider=embedding,
                    retrieval_repository=repository,
                    retrieval_positive_k=positive_k,
                    retrieval_negative_k=negative_k,
                    final_temperature=final_temperature,
                    strict_retrieval=True,
                )
                typer.echo("A/B retrieval-guided discovery: retrieval ON")
                retrieval_pool = await retrieval_detector.discover_guided(
                    chunks,
                    broad,
                )
                retrieval_pool_fingerprint = candidate_pool_fingerprint(
                    retrieval_pool
                )
                guided_added_candidates = max(
                    0,
                    len(retrieval_pool) - len(broad),
                )
                typer.echo(
                    "A/B guided final review: retrieval OFF; "
                    f"{len(retrieval_pool)} candidates "
                    f"({guided_added_candidates} added by guided discovery)"
                )
                retrieval_candidates = await baseline_detector.final_select(
                    retrieval_pool,
                    top_k=top_k,
                )
                guided_only_run = build_guided_only_run(
                    stream_id=resolved_stream_id,
                    transcript_path=str(transcript),
                    baseline_pool=broad,
                    guided_pool=retrieval_pool,
                )

            shared_metadata = {
                "experiment": "retrieval-guided-ab-v3",
                "candidate_pool_count": len(broad),
                "candidate_pool_fingerprint": pool_fingerprint,
                "retrieval_candidate_pool_count": len(retrieval_pool),
                "retrieval_candidate_pool_fingerprint": (
                    retrieval_pool_fingerprint
                ),
                "guided_added_candidates": guided_added_candidates,
                "retrieval_version": RETRIEVAL_VERSION,
                "retrieval_document_count": len(documents),
                "retrieval_corpus_fingerprint": corpus_fingerprint,
                "embedding_model": embedding_settings.model,
                "positive_k": positive_k,
                "negative_k": negative_k,
                "top_k": top_k,
                "window_minutes": window_minutes,
                "overlap_minutes": overlap_minutes,
                "fast_model": nim_settings.fast_model,
                "strong_model": nim_settings.model,
                "final_temperature": final_temperature,
                "guided_discovery_enabled": True,
                "final_retrieval_enabled": False,
            }

            baseline_run = build_run(
                stream_id=resolved_stream_id,
                transcript_path=str(transcript),
                candidates=baseline_candidates,
                metadata={
                    **shared_metadata,
                    "variant": "baseline",
                    "retrieval_enabled": False,
                    "guided_discovery_enabled": False,
                    "final_retrieval_enabled": False,
                },
            )
            retrieval_run = build_run(
                stream_id=resolved_stream_id,
                transcript_path=str(transcript),
                candidates=retrieval_candidates,
                metadata={
                    **shared_metadata,
                    "variant": "retrieval",
                    "retrieval_enabled": True,
                    "guided_discovery_enabled": True,
                    "final_retrieval_enabled": False,
                },
            )
            blind_run = build_blind_ab_run(
                baseline_run,
                retrieval_run,
            )
            save_run(baseline_run, baseline_path)
            save_run(retrieval_run, retrieval_path)
            save_run(blind_run, blind_path)
            save_run(guided_only_run, guided_only_path)
            save_retrieval_ab_manifest(
                RetrievalABManifest(
                    stream_id=resolved_stream_id,
                    transcript_path=str(transcript),
                    baseline_run_path=str(baseline_path),
                    retrieval_run_path=str(retrieval_path),
                    blind_run_path=str(blind_path),
                    guided_only_run_path=str(guided_only_path),
                    candidate_pool_count=len(broad),
                    candidate_pool_fingerprint=pool_fingerprint,
                    retrieval_candidate_pool_count=len(retrieval_pool),
                    retrieval_candidate_pool_fingerprint=(
                        retrieval_pool_fingerprint
                    ),
                    guided_added_candidates=guided_added_candidates,
                    retrieval_document_count=len(documents),
                    retrieval_corpus_fingerprint=corpus_fingerprint,
                    retrieval_version=RETRIEVAL_VERSION,
                    embedding_model=embedding_settings.model,
                    positive_k=positive_k,
                    negative_k=negative_k,
                    top_k=top_k,
                    window_minutes=window_minutes,
                    overlap_minutes=overlap_minutes,
                    fast_model=nim_settings.fast_model,
                    strong_model=nim_settings.model,
                    final_temperature=final_temperature,
                    guided_discovery_enabled=True,
                    final_retrieval_enabled=False,
                ),
                manifest_path,
            )

            return {
                "stream_id": resolved_stream_id,
                "broad_candidates": len(broad),
                "candidate_pool_fingerprint": pool_fingerprint,
                "retrieval_candidates_after_guided_discovery": len(
                    retrieval_pool
                ),
                "retrieval_candidate_pool_fingerprint": (
                    retrieval_pool_fingerprint
                ),
                "guided_added_candidates": guided_added_candidates,
                "retrieval_documents": len(documents),
                "retrieval_corpus_fingerprint": corpus_fingerprint,
                "baseline_selected": len(baseline_candidates),
                "retrieval_selected": len(retrieval_candidates),
                "baseline_run": str(baseline_path),
                "retrieval_run": str(retrieval_path),
                "blind_run": str(blind_path),
                "guided_only_candidates": len(guided_only_run.candidates),
                "guided_only_run": str(guided_only_path),
                "manifest": str(manifest_path),
            }

    try:
        with SQLiteCanonicalRepository(database) as repository:
            stage_cache = StageArtifactCache(
                repository,
                Path("data/artifacts/stages"),
            )
            result = asyncio.run(_run(stage_cache, repository))
    except NvidiaNimAuthorizationError as exc:
        typer.echo(f"NVIDIA NIM error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NvidiaNimTimeoutError as exc:
        typer.echo(f"NVIDIA NIM timeout: {exc}", err=True)
        raise typer.Exit(3) from exc
    except (
        NvidiaEmbeddingAuthorizationError,
        NvidiaEmbeddingRateLimitDeferred,
        EmbeddingProviderError,
        RuntimeError,
    ) as exc:
        typer.echo(f"retrieval A/B failed: {exc}", err=True)
        raise typer.Exit(4) from exc

    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def doctor() -> None:
    """Inspect free contracts, model lifecycle and qualified capabilities without inference."""
    _load_env_file()
    async def inspect():
        runtime = ProviderRuntime()
        try:
            await runtime.startup()
            required = [(Capability.REASONING_STRONG, ExecutionMode.TEXT),
                        (Capability.REASONING_FAST, ExecutionMode.TEXT),
                        (Capability.ASR_SEGMENTS, ExecutionMode.AUDIO),
                        (Capability.EMBEDDING, ExecutionMode.EMBEDDING),
                        (Capability.VIDEO_MULTIFRAME, ExecutionMode.MULTIFRAME)]
            routes = {cap.value: [r.entry.identity for r in runtime.router.candidates(
                runtime.contract("readiness:" + cap.value, mode, {cap}))] for cap, mode in required}
            return {"cost_policy": "ZERO_COST_ONLY", "paid_fallback_allowed": False,
                    "qualified_routes": routes, "ready": all(routes.values()),
                    "status": "READY" if all(routes.values()) else "BLOCKED_DEPENDENCY"}
        finally:
            await runtime.aclose()
    report = asyncio.run(inspect())
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ready"]:
        raise typer.Exit(3)


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
            async with MeshEmbeddingClient(settings) as provider:
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
    source_start_hint: float | None = typer.Option(
        None,
        "--source-start-hint",
        min=0.0,
        help=(
            "Authoritative expected source start in seconds. A t= query inside "
            "--source-url is diagnostic only unless this option is supplied."
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
    nim_settings = RoutingSettings.from_env()
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
                MeshLLMClient(
                    nim_settings,
                    event_callback=typer.echo,
                ) as llm,
                MeshEmbeddingClient(embedding_settings) as embedding,
            ):
                return await ingest_human_example_from_urls(
                    source_provider,
                    clip_url=clip_url,
                    source_url=source_url,
                    source_start_hint_seconds=source_start_hint,
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
    nim_settings = RoutingSettings.from_env()
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
                MeshLLMClient(
                    nim_settings,
                    event_callback=typer.echo,
                ) as llm,
                MeshEmbeddingClient(embedding_settings) as embedding,
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
                            source_start_hint_seconds=item.source_start_hint_seconds,
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
    settings = RoutingSettings.from_env()
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
            async with MeshLLMClient(
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
                    cache_provider="capability_mesh",
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

    nim_settings = RoutingSettings.from_env()
    embedding_settings = NimEmbeddingSettings.from_env()

    async def _run() -> dict[str, object]:
        updated = 0
        alignment_changed = 0
        alignment_geometry_changed = 0
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
            async with MeshLLMClient(
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
                    if realigned.alignment_geometry_changed:
                        alignment_geometry_changed += 1

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
                        realigned.alignment_geometry_changed
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
                                cache_provider="capability_mesh",
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
            async with MeshEmbeddingClient(
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
            "alignment_geometry_changed": alignment_geometry_changed,
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
            async with MeshEmbeddingClient(settings) as provider:
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
            async with MeshEmbeddingClient(settings) as provider:
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
def examples_audit(
    store: Path = typer.Option(
        Path("data/examples/human_clips.jsonl"),
        "--store",
    ),
    database: Path = typer.Option(
        Path("data/holoclip.db"),
        "--database",
    ),
    quality: str = typer.Option(
        "all",
        "--quality",
        help="Filter: all, trusted, review, or rejected.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Write the complete audit to a file instead of stdout.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Render the batch audit as JSON.",
    ),
) -> None:
    """Audit human-made clip examples in one read-only batch."""
    normalized_quality = quality.strip().lower()
    if normalized_quality == "all":
        quality_filter = None
    else:
        try:
            quality_filter = LearningQuality(normalized_quality)
        except ValueError as exc:
            typer.echo(
                "--quality must be one of: all, trusted, review, rejected",
                err=True,
            )
            raise typer.Exit(2) from exc

    examples = load_human_clip_store(store)
    selected = [
        example
        for example in examples
        if quality_filter is None
        or evaluate_human_example_learning_quality(example).decision
        is quality_filter
    ]

    retrieval_documents_by_example: dict[str, list] = {}
    with SQLiteCanonicalRepository(database) as repository:
        for example in selected:
            retrieval_documents_by_example[example.id] = (
                repository.list_retrieval_documents_for_example(example.id)
            )

    batch = build_human_example_audit_batch(
        examples,
        retrieval_documents_by_example=retrieval_documents_by_example,
        quality_filter=quality_filter,
    )
    if as_json:
        rendered = json.dumps(
            batch.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    else:
        rendered = format_human_example_audit_batch(batch)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        typer.echo(
            f"wrote {batch.matched_examples} example audit(s): {output}"
        )
        return

    typer.echo(rendered)


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
    editorial_profile: Path = typer.Option(Path("data/examples/editorial_profile.json")),
) -> None:
    """Append one accept/maybe/reject judgment to the evaluation log."""
    _require_learning_ready_for_review(editorial_profile)
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
    """Review one saved run with single-key-style choices after automated learning."""
    _require_learning_ready_for_review(Path("data/examples/editorial_profile.json"))
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
def retrieval_ab_diff(
    stream_id: str = typer.Option(..., "--stream-id"),
    output_root: Path = typer.Option(
        Path("data/evaluation/ab"),
        "--output-root",
    ),
    feedback_file: Path = typer.Option(
        Path("data/evaluation/feedback.jsonl"),
        "--feedback-file",
    ),
) -> None:
    """Inspect post-label qualitative differences between paired A/B runs."""
    baseline_path = output_root / "baseline" / f"{stream_id}.json"
    retrieval_path = output_root / "retrieval" / f"{stream_id}.json"
    if not baseline_path.exists() or not retrieval_path.exists():
        typer.echo(
            "paired run files are missing for the requested stream id",
            err=True,
        )
        raise typer.Exit(2)

    diff = build_retrieval_ab_diff(
        load_run(baseline_path),
        load_run(retrieval_path),
        load_feedback(feedback_file),
    )
    typer.echo(
        json.dumps(
            diff.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def guided_discovery_metrics(
    output_root: Path = typer.Option(
        Path("data/evaluation/ab"),
        "--output-root",
    ),
    feedback_file: Path = typer.Option(
        Path("data/evaluation/feedback.jsonl"),
        "--feedback-file",
    ),
) -> None:
    """Measure intrinsic quality and selector misses for guided-only candidates."""
    guided_runs = load_runs(output_root / "guided-only")
    treatment_runs = load_runs(output_root / "retrieval")
    guided_streams = {run.stream_id for run in guided_runs}
    treatment_streams = {run.stream_id for run in treatment_runs}
    if guided_streams != treatment_streams:
        typer.echo(
            "guided-only and retrieval directories do not contain "
            "the same stream ids",
            err=True,
        )
        raise typer.Exit(2)

    metrics = compute_guided_discovery_metrics(
        guided_runs,
        treatment_runs,
        load_feedback(feedback_file),
    )
    typer.echo(
        json.dumps(
            metrics.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def retrieval_ab_metrics(
    output_root: Path = typer.Option(
        Path("data/evaluation/ab"),
        "--output-root",
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
    """Compare paired retrieval-OFF/ON evaluation metrics."""
    baseline_runs = load_runs(output_root / "baseline")
    retrieval_runs = load_runs(output_root / "retrieval")

    baseline_streams = {run.stream_id for run in baseline_runs}
    retrieval_streams = {run.stream_id for run in retrieval_runs}
    if baseline_streams != retrieval_streams:
        missing_retrieval = sorted(baseline_streams - retrieval_streams)
        missing_baseline = sorted(retrieval_streams - baseline_streams)
        typer.echo(
            "paired A/B run directories do not contain the same stream ids",
            err=True,
        )
        if missing_retrieval:
            typer.echo(
                "missing retrieval runs: " + ", ".join(missing_retrieval),
                err=True,
            )
        if missing_baseline:
            typer.echo(
                "missing baseline runs: " + ", ".join(missing_baseline),
                err=True,
            )
        raise typer.Exit(2)

    feedback = load_feedback(feedback_file)
    known_clips = load_known_clips(known_clips_file)
    baseline = compute_metrics(
        baseline_runs,
        feedback,
        known_clips,
    )
    retrieval = compute_metrics(
        retrieval_runs,
        feedback,
        known_clips,
    )
    typer.echo(
        json.dumps(
            {
                "paired_streams": len(baseline_streams),
                "baseline": baseline.model_dump(mode="json"),
                "retrieval": retrieval.model_dump(mode="json"),
                "delta_retrieval_minus_baseline": compare_evaluation_metrics(
                    baseline,
                    retrieval,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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
