from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from holoclip.analysis.prompts import discovery_messages, final_review_messages
from holoclip.execution import StageArtifactCache, StageStatus, make_input_fingerprint
from holoclip.models import CandidateWarning, ClipCandidate, TranscriptChunk
from holoclip.providers.llm.base import LLMProvider


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


def _overlap_ratio(left: ClipCandidate, right: ClipCandidate) -> float:
    intersection = max(0.0, min(left.end, right.end) - max(left.start, right.start))
    shortest = min(left.end - left.start, right.end - right.start)
    return 0.0 if shortest <= 0 else intersection / shortest


def dedupe_candidates(candidates: list[ClipCandidate]) -> list[ClipCandidate]:
    ordered = sorted(candidates, key=lambda item: item.confidence, reverse=True)
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
    ) -> None:
        self.llm = llm
        self.fast_model = fast_model
        self.strong_model = strong_model
        self.fast_reasoning_effort = fast_reasoning_effort
        self.strong_reasoning_effort = strong_reasoning_effort
        self.progress = progress
        self.stage_cache = stage_cache
        self.cache_provider = cache_provider

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
        flattened: list[ClipCandidate] = []
        completed = 0

        for task in asyncio.as_completed(tasks):
            chunk, batch = await task
            completed += 1
            flattened.extend(batch)
            self._emit(
                f"[{completed}/{len(chunks)}] broad "
                f"{chunk.start / 60:.1f}-{chunk.end / 60:.1f} min: "
                f"{len(batch)} candidates"
            )

        deduped = dedupe_candidates(flattened)
        self._emit(f"Broad discovery: {len(flattened)} raw -> {len(deduped)} deduped")
        return deduped

    async def final_select(
        self,
        candidates: list[ClipCandidate],
        *,
        top_k: int = 5,
    ) -> list[ClipCandidate]:
        if not candidates:
            return []

        self._emit(
            f"Final review: {len(candidates)} candidates with {self.strong_model}"
        )
        payload = await self.llm.chat_json(
            final_review_messages(candidates, top_k=top_k),
            model=self.strong_model,
            temperature=0.1,
            max_tokens=3072,
            reasoning_effort=self.strong_reasoning_effort,
        )
        ranked = payload.get("ranked")
        if not isinstance(ranked, list):
            return sorted(candidates, key=lambda item: item.confidence, reverse=True)[:top_k]

        selected: list[ClipCandidate] = []
        seen: set[int] = set()

        for item in ranked:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if index in seen or index < 0 or index >= len(candidates):
                continue

            candidate = candidates[index].model_copy(deep=True)
            try:
                candidate.confidence = max(
                    0.0,
                    min(1.0, float(item.get("confidence", candidate.confidence))),
                )
            except (TypeError, ValueError):
                pass
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
