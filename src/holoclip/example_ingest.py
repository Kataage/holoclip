from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from holoclip.alignment import ALIGNMENT_METHOD, align_clip_to_source
from holoclip.config import GroqTranscriptionSettings
from holoclip.examples import (
    HumanClipExample,
    make_human_clip_example_id,
    upsert_human_clip_examples,
)
from holoclip.execution import StageArtifactCache
from holoclip.learning import enrich_human_clip_example
from holoclip.providers.embedding.base import EmbeddingProvider
from holoclip.providers.llm.base import LLMProvider
from holoclip.providers.source.base import (
    AcquisitionProfile,
    SourceCandidate,
    SourceProvider,
)
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.retrieval import IndexBuildStats, index_human_examples
from holoclip.transcript import load_transcript, save_transcript
from holoclip.transcription import transcribe_media


class SourceResolutionError(RuntimeError):
    def __init__(self, candidates: list[SourceCandidate]) -> None:
        self.candidates = candidates
        message = "original source URL could not be resolved unambiguously"
        if candidates:
            message += f" ({len(candidates)} candidate(s) found)"
        super().__init__(message)


class HumanExampleIngestResult(BaseModel):
    example_id: str
    clip_url: str
    source_url: str
    source_auto_detected: bool
    clip_media_path: Path
    source_media_path: Path
    clip_transcript_path: Path
    source_transcript_path: Path
    alignment_confidence: float
    alignment_segments: int
    aligned: bool
    enriched: bool
    retrieval_index: IndexBuildStats | None = None
    acquisition_provider: str | None = None
    acquisition_provider_version: str | None = None
    acquisition_provider_channel: str | None = None
    acquisition_js_runtime: str | None = None


async def _transcript_for_media(
    media_path: Path,
    target: Path,
    *,
    groq_settings: GroqTranscriptionSettings,
    stage_cache: StageArtifactCache,
    progress: Callable[[str], None] | None,
):
    if target.exists():
        if progress is not None:
            progress(f"reusing transcript: {target}")
        return load_transcript(target)

    if progress is not None:
        progress(f"transcribing: {media_path}")
    segments = await transcribe_media(
        media_path,
        groq_settings,
        stage_cache=stage_cache,
        progress=progress,
    )
    save_transcript(segments, target)
    if progress is not None:
        progress(f"saved transcript: {target}")
    return segments


async def ingest_human_example_from_urls(
    source_provider: SourceProvider,
    *,
    clip_url: str,
    source_url: str | None,
    download_root: Path,
    transcript_root: Path,
    examples_store: Path,
    groq_settings: GroqTranscriptionSettings,
    llm: LLMProvider,
    llm_model: str,
    llm_reasoning_effort: str,
    embedding_provider: EmbeddingProvider,
    repository,
    stage_cache: StageArtifactCache,
    min_confidence: float = 0.55,
    weak_negative_window_seconds: float = 90.0,
    progress: Callable[[str], None] | None = None,
) -> HumanExampleIngestResult:
    clip_metadata = source_provider.probe(clip_url)

    resolved_source_url = source_url
    source_auto_detected = False
    chosen_source: SourceCandidate | None = None
    if not resolved_source_url:
        candidates = source_provider.source_candidates(clip_metadata)
        chosen = choose_source_candidate(candidates)
        if chosen is None:
            raise SourceResolutionError(candidates)
        chosen_source = chosen
        resolved_source_url = chosen.url
        source_auto_detected = True
        if progress is not None:
            progress(
                "auto-detected original source: "
                f"{resolved_source_url} ({chosen.reason}, score={chosen.score:.2f})"
            )

    if progress is not None:
        progress("acquiring human-made clip audio")
    clip_source = source_provider.acquire(
        clip_url,
        download_root / "clips",
        profile=AcquisitionProfile.TRAINING_AUDIO,
    )

    if progress is not None:
        progress("acquiring original source audio")
    original_source = source_provider.acquire(
        resolved_source_url,
        download_root / "sources",
        profile=AcquisitionProfile.TRAINING_AUDIO,
    )

    example_id = make_human_clip_example_id(clip_url)
    clip_transcript_path = transcript_root / "examples" / f"{example_id}.json"
    source_transcript_path = (
        transcript_root
        / "sources"
        / f"{original_source.metadata.external_id}.json"
    )

    clip_segments = await _transcript_for_media(
        clip_source.media_path,
        clip_transcript_path,
        groq_settings=groq_settings,
        stage_cache=stage_cache,
        progress=progress,
    )
    source_segments = await _transcript_for_media(
        original_source.media_path,
        source_transcript_path,
        groq_settings=groq_settings,
        stage_cache=stage_cache,
        progress=progress,
    )

    alignment = align_clip_to_source(clip_segments, source_segments)
    accepted = alignment.confidence >= min_confidence
    if progress is not None:
        progress(
            f"alignment confidence={alignment.confidence:.3f} "
            f"segments={len(alignment.segments)} "
            f"accepted={'yes' if accepted else 'no'}"
        )

    excerpt = " ".join(segment.text for segment in clip_segments)[:4000]
    example = HumanClipExample(
        id=example_id,
        clip_url=clip_metadata.webpage_url or clip_url,
        clip_title=clip_metadata.title,
        clip_channel=clip_metadata.channel,
        source_url=original_source.metadata.webpage_url or resolved_source_url,
        source_title=original_source.metadata.title,
        stream_id=original_source.metadata.external_id,
        source_start=alignment.source_start if accepted else None,
        source_end=alignment.source_end if accepted else None,
        alignment_confidence=alignment.confidence,
        alignment_method=ALIGNMENT_METHOD,
        alignment_matched_anchors=alignment.matched_anchors,
        alignment_segments=(
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
        clip_transcript_path=str(clip_transcript_path),
        source_transcript_path=str(source_transcript_path),
        transcript_excerpt=excerpt,
        notes=(
            f"ingested via SourceProvider={type(source_provider).__name__}; "
            "training acquisition profile=audio"
        ),
        acquisition_provenance={
            "provider": clip_source.metadata.provider,
            "provider_version": clip_source.metadata.provider_version,
            "provider_channel": clip_source.metadata.provider_channel,
            "js_runtime": clip_source.metadata.js_runtime,
            "clip_profile": clip_source.profile.value,
            "source_profile": original_source.profile.value,
            "requested_source_url": source_url,
            "resolved_source_url": resolved_source_url,
            "source_auto_detected": source_auto_detected,
            "source_resolution_reason": (
                chosen_source.reason if chosen_source is not None else "explicit_source_url"
            ),
            "source_resolution_score": (
                chosen_source.score if chosen_source is not None else 1.0
            ),
            "source_resolution_context": (
                chosen_source.context if chosen_source is not None else None
            ),
        },
    )
    upsert_human_clip_examples(examples_store, [example])

    enriched = False
    index_stats: IndexBuildStats | None = None
    if accepted:
        if progress is not None:
            progress("extracting editorial structure and weak negatives")
        example = await enrich_human_clip_example(
            llm,
            example,
            source_segments,
            model=llm_model,
            reasoning_effort=llm_reasoning_effort,
            weak_negative_window_seconds=weak_negative_window_seconds,
        )
        upsert_human_clip_examples(examples_store, [example])
        enriched = True

        from holoclip.examples import load_human_clip_store

        index_stats = await index_human_examples(
            embedding_provider,
            repository,
            load_human_clip_store(examples_store),
        )
        if progress is not None:
            progress(
                "retrieval index: "
                f"{index_stats.embedded_documents} embedded, "
                f"{index_stats.reused_documents} reused"
            )

    return HumanExampleIngestResult(
        example_id=example_id,
        clip_url=clip_metadata.webpage_url or clip_url,
        source_url=original_source.metadata.webpage_url or resolved_source_url,
        source_auto_detected=source_auto_detected,
        clip_media_path=clip_source.media_path,
        source_media_path=original_source.media_path,
        clip_transcript_path=clip_transcript_path,
        source_transcript_path=source_transcript_path,
        alignment_confidence=alignment.confidence,
        alignment_segments=len(alignment.segments),
        aligned=accepted,
        enriched=enriched,
        retrieval_index=index_stats,
        acquisition_provider=clip_source.metadata.provider,
        acquisition_provider_version=clip_source.metadata.provider_version,
        acquisition_provider_channel=clip_source.metadata.provider_channel,
        acquisition_js_runtime=clip_source.metadata.js_runtime,
    )
