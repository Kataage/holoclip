from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from holoclip.analysis.prompts import (
    discovery_messages,
    final_review_messages,
    guided_discovery_messages,
)
from holoclip.execution import StageArtifactCache, StageStatus, make_input_fingerprint
from holoclip.models import (
    CandidateRetrievalHit,
    CandidateRetrievalTrace,
    CandidateWarning,
    ClipCandidate,
    TranscriptChunk,
)
from holoclip.providers.embedding.base import EmbeddingProvider, EmbeddingProviderError
from holoclip.providers.llm.base import LLMProvider
from holoclip.retrieval import (
    RetrievalBundle,
    clip_candidate_query_text,
    retrieve_structural_examples_many,
)


def _candidate_from_item(item: dict[str, Any], chunk: TranscriptChunk) -> ClipCandidate | None:
    try:
        start = float(item["start"])
        end = float(item["end"])
    except (KeyError, TypeError, ValueError):
        return None

    if end <= start or start < chunk.start - 1 or end > chunk.end + 1:
        return None

    warnings: list[CandidateWarning] = []
    for value in item.get("warnings", []):
        try:
            warnings.append(CandidateWarning(value))
        except ValueError:
            continue

    try:
        return ClipCandidate(
            start=start,
            end=end,
            working_title=str(item.get("working_title", "")).strip() or "Untitled candidate",
            setup=str(item.get("setup", "")).strip(),
            development=str(item.get("development", "")).strip(),
            payoff=str(item.get("payoff", "")).strip(),
            why_interesting=str(item.get("why_interesting", "")).strip(),
            traits=[str(value) for value in item.get("traits", [])],
            context_requirement=str(item.get("context_requirement", "medium")),
            confidence=float(item.get("confidence", 0.5)),
            source_chunk_ids=[chunk.id],
            warnings=warnings,
        )
    except (TypeError, ValueError):
        return None


def _bundle_evidence(bundle: RetrievalBundle) -> dict[str, object]:
    return {
        "positive_examples": [
            {
                "example_id": hit.example_id,
                "document_id": hit.document_id,
                "similarity": round(hit.score, 4),
                "structure": hit.text[:240],
            }
            for hit in bundle.positives
        ],
        "weak_negatives": [
            {
                "example_id": hit.example_id,
                "document_id": hit.document_id,
                "similarity": round(hit.score, 4),
                "structure": hit.text[:240],
                "reason": hit.metadata.get("reason"),
            }
            for hit in bundle.negatives
        ],
    }


def _guided_query_text(
    chunk: TranscriptChunk,
    existing_candidates: list[ClipCandidate],
) -> str:
    if existing_candidates:
        strongest = sorted(
            existing_candidates,
            key=lambda item: (
                -item.confidence,
                item.start,
                item.end,
                item.working_title,
            ),
        )[:3]
        return "\n\n".join(
            clip_candidate_query_text(candidate)
            for candidate in strongest
        )

    excerpt = " ".join(chunk.text.split())
    return f"transcript_excerpt: {excerpt[:1800]}"


def merge_guided_candidates(
    baseline: list[ClipCandidate],
    guided: list[ClipCandidate],
) -> list[ClipCandidate]:
    baseline_copy = [item.model_copy(deep=True) for item in baseline]
    non_overlapping = [
        candidate
        for candidate in guided
        if not any(
            _overlap_ratio(candidate, existing) >= 0.6
            for existing in baseline_copy
        )
    ]
    guided_deduped = dedupe_candidates(non_overlapping)
    return sorted(
        [*baseline_copy, *guided_deduped],
        key=lambda item: (item.start, item.end, item.working_title),
    )


def _overlap_ratio(left: ClipCandidate, right: ClipCandidate) -> float:
    intersection = max(0.0, min(left.end, right.end) - max(left.start, right.start))
    shortest = min(left.end - left.start, right.end - right.start)
    return 0.0 if shortest <= 0 else intersection / shortest


def dedupe_candidates(candidates: list[ClipCandidate]) -> list[ClipCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item.confidence,
            item.start,
            item.end,
            item.working_title,
            tuple(sorted(item.source_chunk_ids)),
        ),
    )
    kept: list[ClipCandidate] = []

    for candidate in ordered:
        duplicate = next(
            (existing for existing in kept if _overlap_ratio(candidate, existing) >= 0.6),
            None,
        )
        if duplicate is None:
            kept.append(candidate)
            continue

        duplicate.source_chunk_ids = sorted(
            set(duplicate.source_chunk_ids + candidate.source_chunk_ids)
        )
        duplicate.traits = sorted(set(duplicate.traits + candidate.traits))

    return sorted(kept, key=lambda item: item.start)


class CandidateDetector:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        fast_model: str,
        strong_model: str,
        fast_reasoning_effort: str = "low",
        strong_reasoning_effort: str = "high",
        progress: Callable[[str], None] | None = None,
        stage_cache: StageArtifactCache | None = None,
        cache_provider: str = "llm",
        embedding_provider: EmbeddingProvider | None = None,
        retrieval_repository: Any | None = None,
        retrieval_positive_k: int = 3,
        retrieval_negative_k: int = 3,
        final_temperature: float = 0.1,
        strict_retrieval: bool = False,
    ) -> None:
        self.llm = llm
        self.fast_model = fast_model
        self.strong_model = strong_model
        self.fast_reasoning_effort = fast_reasoning_effort
        self.strong_reasoning_effort = strong_reasoning_effort
        self.progress = progress
        self.stage_cache = stage_cache
        self.cache_provider = cache_provider
        self.embedding_provider = embedding_provider
        self.retrieval_repository = retrieval_repository
        self.retrieval_positive_k = retrieval_positive_k
        self.retrieval_negative_k = retrieval_negative_k
        self.final_temperature = final_temperature
        self.strict_retrieval = strict_retrieval

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    async def _discover_chunk(self, chunk: TranscriptChunk) -> list[ClipCandidate]:
        input_fingerprint = make_input_fingerprint(
            {
                "chunk_id": chunk.id,
                "start": chunk.start,
                "end": chunk.end,
                "text": chunk.text,
                "segment_indices": chunk.segment_indices,
            }
        )
        payload = None

        if self.stage_cache is not None:
            payload = self.stage_cache.reusable_json(
                stage="broad_candidate_discovery",
                subject_type="transcript_chunk",
                subject_id=chunk.id,
                input_fingerprint=input_fingerprint,
                stage_version="1",
                provider=self.cache_provider,
                model=self.fast_model,
                prompt_version="discovery-v1",
            )
            if payload is not None:
                self._emit(f"broad {chunk.id}: reused")

        if payload is None:
            run = None
            if self.stage_cache is not None:
                run = self.stage_cache.start(
                    stage="broad_candidate_discovery",
                    subject_type="transcript_chunk",
                    subject_id=chunk.id,
                    input_fingerprint=input_fingerprint,
                    stage_version="1",
                    provider=self.cache_provider,
                    model=self.fast_model,
                    prompt_version="discovery-v1",
                )
            try:
                payload = await self.llm.chat_json(
                    discovery_messages(chunk),
                    model=self.fast_model,
                    temperature=0.2,
                    max_tokens=2048,
                    reasoning_effort=self.fast_reasoning_effort,
                )
            except Exception as exc:
                if self.stage_cache is not None and run is not None:
                    self.stage_cache.fail(
                        run,
                        status=StageStatus.FAILED_RETRYABLE,
                        error=exc,
                    )
                raise

            if self.stage_cache is not None and run is not None:
                self.stage_cache.succeed_json(
                    run,
                    payload,
                    kind="broad_candidate_discovery",
                )
        raw = payload.get("candidates", [])
        if not isinstance(raw, list):
            return []

        found = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_item(item, chunk)
            if candidate is not None:
                found.append(candidate)
        return found

    async def discover_broad(self, chunks: list[TranscriptChunk]) -> list[ClipCandidate]:
        if not chunks:
            return []

        async def run_one(
            chunk: TranscriptChunk,
        ) -> tuple[TranscriptChunk, list[ClipCandidate]]:
            return chunk, await self._discover_chunk(chunk)

        tasks = [asyncio.create_task(run_one(chunk)) for chunk in chunks]
        batches_by_chunk_id: dict[str, list[ClipCandidate]] = {}
        completed = 0

        for task in asyncio.as_completed(tasks):
            chunk, batch = await task
            completed += 1
            batches_by_chunk_id[chunk.id] = batch
            self._emit(
                f"[{completed}/{len(chunks)}] broad "
                f"{chunk.start / 60:.1f}-{chunk.end / 60:.1f} min: "
                f"{len(batch)} candidates"
            )

        flattened = [
            candidate
            for chunk in chunks
            for candidate in batches_by_chunk_id.get(chunk.id, [])
        ]
        deduped = dedupe_candidates(flattened)
        self._emit(f"Broad discovery: {len(flattened)} raw -> {len(deduped)} deduped")
        return deduped


    async def _discover_guided_chunk(
        self,
        chunk: TranscriptChunk,
        *,
        existing_candidates: list[ClipCandidate],
        retrieval_evidence: dict[str, object],
    ) -> list[ClipCandidate]:
        input_fingerprint = make_input_fingerprint(
            {
                "chunk_id": chunk.id,
                "start": chunk.start,
                "end": chunk.end,
                "text": chunk.text,
                "segment_indices": chunk.segment_indices,
                "existing_candidates": [
                    {
                        "start": item.start,
                        "end": item.end,
                        "working_title": item.working_title,
                        "setup": item.setup,
                        "development": item.development,
                        "payoff": item.payoff,
                    }
                    for item in existing_candidates
                ],
                "retrieval_evidence": retrieval_evidence,
            }
        )
        payload = None

        if self.stage_cache is not None:
            payload = self.stage_cache.reusable_json(
                stage="retrieval_guided_discovery",
                subject_type="transcript_chunk",
                subject_id=chunk.id,
                input_fingerprint=input_fingerprint,
                stage_version="1",
                provider=self.cache_provider,
                model=self.fast_model,
                prompt_version="guided-discovery-v1",
            )
            if payload is not None:
                self._emit(f"guided {chunk.id}: reused")

        if payload is None:
            run = None
            if self.stage_cache is not None:
                run = self.stage_cache.start(
                    stage="retrieval_guided_discovery",
                    subject_type="transcript_chunk",
                    subject_id=chunk.id,
                    input_fingerprint=input_fingerprint,
                    stage_version="1",
                    provider=self.cache_provider,
                    model=self.fast_model,
                    prompt_version="guided-discovery-v1",
                )
            try:
                payload = await self.llm.chat_json(
                    guided_discovery_messages(
                        chunk,
                        existing_candidates=existing_candidates,
                        retrieval_evidence=retrieval_evidence,
                    ),
                    model=self.fast_model,
                    temperature=0.1,
                    max_tokens=2048,
                    reasoning_effort=self.fast_reasoning_effort,
                )
            except Exception as exc:
                if self.stage_cache is not None and run is not None:
                    self.stage_cache.fail(
                        run,
                        status=StageStatus.FAILED_RETRYABLE,
                        error=exc,
                    )
                raise

            if self.stage_cache is not None and run is not None:
                self.stage_cache.succeed_json(
                    run,
                    payload,
                    kind="retrieval_guided_discovery",
                )

        raw = payload.get("candidates", [])
        if not isinstance(raw, list):
            return []

        found: list[ClipCandidate] = []
        for item in raw[:6]:
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_item(item, chunk)
            if candidate is not None:
                found.append(candidate)
        return found

    async def discover_guided(
        self,
        chunks: list[TranscriptChunk],
        baseline_candidates: list[ClipCandidate],
    ) -> list[ClipCandidate]:
        if not chunks:
            return [item.model_copy(deep=True) for item in baseline_candidates]
        if self.embedding_provider is None or self.retrieval_repository is None:
            if self.strict_retrieval:
                raise EmbeddingProviderError(
                    "retrieval-guided discovery requires embedding provider and repository"
                )
            return [item.model_copy(deep=True) for item in baseline_candidates]

        existing_by_chunk: dict[str, list[ClipCandidate]] = {
            chunk.id: [] for chunk in chunks
        }
        for candidate in baseline_candidates:
            for chunk_id in candidate.source_chunk_ids:
                if chunk_id in existing_by_chunk:
                    existing_by_chunk[chunk_id].append(candidate)

        query_texts = [
            _guided_query_text(
                chunk,
                existing_by_chunk.get(chunk.id, []),
            )
            for chunk in chunks
        ]
        try:
            bundles = await retrieve_structural_examples_many(
                self.embedding_provider,
                self.retrieval_repository,
                query_texts,
                positive_k=self.retrieval_positive_k,
                negative_k=self.retrieval_negative_k,
            )
        except EmbeddingProviderError:
            if self.strict_retrieval:
                raise
            self._emit(
                "Retrieval unavailable: guided discovery skipped"
            )
            return [item.model_copy(deep=True) for item in baseline_candidates]

        guided: list[ClipCandidate] = []
        for index, (chunk, bundle) in enumerate(
            zip(chunks, bundles, strict=True),
            start=1,
        ):
            evidence = _bundle_evidence(bundle)
            batch = await self._discover_guided_chunk(
                chunk,
                existing_candidates=existing_by_chunk.get(chunk.id, []),
                retrieval_evidence=evidence,
            )
            guided.extend(batch)
            self._emit(
                f"[{index}/{len(chunks)}] guided "
                f"{chunk.start / 60:.1f}-{chunk.end / 60:.1f} min: "
                f"{len(batch)} additional candidates"
            )

        merged = merge_guided_candidates(baseline_candidates, guided)
        self._emit(
            "Guided discovery: "
            f"{len(baseline_candidates)} baseline + {len(guided)} raw guided "
            f"-> {len(merged)} merged"
        )
        return merged


    async def _attach_retrieval(
        self,
        candidates: list[ClipCandidate],
    ) -> tuple[list[ClipCandidate], list[dict[str, object]] | None]:
        working = [candidate.model_copy(deep=True) for candidate in candidates]
        if self.embedding_provider is None or self.retrieval_repository is None:
            return working, None

        query_texts = [clip_candidate_query_text(candidate) for candidate in working]
        try:
            bundles = await retrieve_structural_examples_many(
                self.embedding_provider,
                self.retrieval_repository,
                query_texts,
                positive_k=self.retrieval_positive_k,
                negative_k=self.retrieval_negative_k,
            )
        except EmbeddingProviderError as exc:
            if self.strict_retrieval:
                raise
            self._emit(f"Retrieval unavailable: {exc}; continuing without examples")
            return working, None

        evidence: list[dict[str, object]] = []
        attached = 0
        for candidate, bundle in zip(working, bundles, strict=True):
            positive_hits = [
                CandidateRetrievalHit(
                    document_id=hit.document_id,
                    example_id=hit.example_id,
                    score=hit.score,
                )
                for hit in bundle.positives
            ]
            negative_hits = [
                CandidateRetrievalHit(
                    document_id=hit.document_id,
                    example_id=hit.example_id,
                    score=hit.score,
                )
                for hit in bundle.negatives
            ]
            if positive_hits or negative_hits:
                candidate.retrieval_trace = CandidateRetrievalTrace(
                    embedding_model=bundle.embedding_model,
                    embedding_version=bundle.embedding_version,
                    positives=positive_hits,
                    weak_negatives=negative_hits,
                )
                attached += 1

            evidence.append(_bundle_evidence(bundle))

        if attached:
            self._emit(
                f"Retrieval: attached examples to {attached}/{len(working)} candidates "
                f"({self.retrieval_positive_k} positive + "
                f"{self.retrieval_negative_k} weak-negative max each)"
            )
            return working, evidence

        self._emit("Retrieval: index is empty; final review uses the baseline prompt")
        return working, None

    async def final_select(
        self,
        candidates: list[ClipCandidate],
        *,
        top_k: int = 5,
    ) -> list[ClipCandidate]:
        if not candidates:
            return []

        review_candidates, retrieval_evidence = await self._attach_retrieval(candidates)

        self._emit(
            f"Final review: {len(review_candidates)} candidates with {self.strong_model}"
        )
        payload = await self.llm.chat_json(
            final_review_messages(
                review_candidates,
                top_k=top_k,
                retrieval_evidence=retrieval_evidence,
            ),
            model=self.strong_model,
            temperature=self.final_temperature,
            max_tokens=3072,
            reasoning_effort=self.strong_reasoning_effort,
        )
        ranked = payload.get("ranked")
        if not isinstance(ranked, list):
            return sorted(
                review_candidates,
                key=lambda item: item.confidence,
                reverse=True,
            )[:top_k]

        selected: list[ClipCandidate] = []
        seen: set[int] = set()

        for item in ranked:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if index in seen or index < 0 or index >= len(review_candidates):
                continue

            candidate = review_candidates[index].model_copy(deep=True)
            try:
                candidate.confidence = max(
                    0.0,
                    min(1.0, float(item.get("confidence", candidate.confidence))),
                )
            except (TypeError, ValueError):
                pass
            if candidate.retrieval_trace is not None:
                assessment = str(
                    item.get("retrieval_assessment")
                    or item.get("reason")
                    or ""
                ).strip()
                candidate.retrieval_trace.selector_reason = assessment or None
            selected.append(candidate)
            seen.add(index)
            if len(selected) >= top_k:
                break

        self._emit(f"Final review: selected {len(selected)} candidates")
        return selected

    async def run(
        self,
        chunks: list[TranscriptChunk],
        *,
        top_k: int = 5,
    ) -> list[ClipCandidate]:
        broad = await self.discover_broad(chunks)
        return await self.final_select(broad, top_k=top_k)
