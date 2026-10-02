from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holoclip.config import GroqTranscriptionSettings
from holoclip.example_ingest import (
    SourceResolutionError,
    ingest_human_example_from_urls,
)
from holoclip.execution import StageArtifactCache
from holoclip.examples import load_human_clip_store
from holoclip.models import TranscriptSegment
from holoclip.providers.source.base import (
    AcquiredSource,
    AcquisitionProfile,
    SourceCandidate,
    SourceMetadata,
)
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import RETRIEVAL_VERSION
from holoclip import example_ingest


class FakeSourceProvider:
    def __init__(self, tmp_path: Path, *, ambiguous: bool = False) -> None:
        self.tmp_path = tmp_path
        self.ambiguous = ambiguous

    def probe(self, url: str) -> SourceMetadata:
        if "clip" in url:
            return SourceMetadata(
                provider="fake",
                url=url,
                webpage_url="https://www.youtube.com/watch?v=clip",
                external_id="clip",
                title="人間切り抜き",
                channel="切り抜きch",
                description="元配信 https://www.youtube.com/watch?v=source",
                provider_version="2026.10.02",
                provider_channel="nightly",
                js_runtime="node",
            )
        return SourceMetadata(
            provider="fake",
            url=url,
            webpage_url="https://www.youtube.com/watch?v=source",
            external_id="source",
            title="元配信",
            channel="公式ch",
            provider_version="2026.10.02",
            provider_channel="nightly",
            js_runtime="node",
        )

    def source_candidates(self, metadata: SourceMetadata) -> list[SourceCandidate]:
        candidates = [
            SourceCandidate(
                url="https://www.youtube.com/watch?v=source",
                score=0.90,
                reason="description_explicit_source_label",
            )
        ]
        if self.ambiguous:
            candidates.append(
                SourceCandidate(
                    url="https://www.youtube.com/watch?v=source2",
                    score=0.90,
                    reason="description_explicit_source_label",
                )
            )
        return candidates

    def acquire(
        self,
        url: str,
        output_root: Path,
        *,
        profile: AcquisitionProfile = AcquisitionProfile.TRAINING_AUDIO,
    ) -> AcquiredSource:
        metadata = self.probe(url)
        path = output_root / metadata.external_id / "media.webm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return AcquiredSource(
            metadata=metadata,
            profile=profile,
            media_path=path,
            reused=False,
        )


class FakeLLM:
    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=0.2,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> str:
        raise AssertionError("chat should not be called")

    async def chat_json(
        self,
        messages,
        *,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> dict[str, Any]:
        return {
            "setup": "給料の話題から始まる",
            "development": "給料が入らなかった時期を告白",
            "payoff": "節約生活のオチにつながる",
            "why_selected": "意外な告白から明確なオチまである",
            "traits": ["unexpected_confession", "setup_payoff"],
            "members": ["member"],
            "topics": ["お金"],
            "relationship_context": "",
            "context_requirement": "low",
        }


class FakeEmbeddingProvider:
    model = "fake-embed"

    async def embed_passages(self, texts):
        return [[1.0, float(index + 1)] for index, _ in enumerate(texts)]

    async def embed_queries(self, texts):
        return [[1.0, 1.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 1.0]


def clip_transcript() -> list[TranscriptSegment]:
    return [
        TranscriptSegment(start=0, end=4, text="今日は給料の話なんだけど"),
        TranscriptSegment(start=5, end=9, text="二ヶ月くらい給料が入らなくて"),
        TranscriptSegment(start=10, end=14, text="でももやし生活だから大丈夫だった"),
    ]


def source_transcript() -> list[TranscriptSegment]:
    return [
        TranscriptSegment(start=100, end=110, text="その前の普通の雑談です"),
        TranscriptSegment(start=120, end=124, text="今日は給料の話なんだけど"),
        TranscriptSegment(start=125, end=129, text="二ヶ月くらい給料が入らなくて"),
        TranscriptSegment(start=130, end=134, text="でももやし生活だから大丈夫だった"),
        TranscriptSegment(start=150, end=160, text="次の普通の雑談です"),
    ]


@pytest.mark.asyncio
async def test_url_ingestion_runs_alignment_enrichment_and_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def fake_transcribe(media_path, settings, *, stage_cache=None, progress=None):
        return clip_transcript() if "clip" in str(media_path) else source_transcript()

    monkeypatch.setattr(example_ingest, "transcribe_media", fake_transcribe)

    database = tmp_path / "holoclip.db"
    store = tmp_path / "human_clips.jsonl"
    with SQLiteCanonicalRepository(database) as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")
        result = await ingest_human_example_from_urls(
            FakeSourceProvider(tmp_path),
            clip_url="https://www.youtube.com/watch?v=clip",
            source_url=None,
            download_root=tmp_path / "sources",
            transcript_root=tmp_path / "transcripts",
            examples_store=store,
            groq_settings=GroqTranscriptionSettings(
                api_key="test",
                rpm=60000,
            ),
            llm=FakeLLM(),
            llm_model="strong",
            llm_reasoning_effort="high",
            embedding_provider=FakeEmbeddingProvider(),
            repository=repository,
            stage_cache=cache,
            min_confidence=0.55,
        )

        documents = repository.list_retrieval_documents(
            embedding_model="fake-embed",
            embedding_version=RETRIEVAL_VERSION,
        )

    examples = load_human_clip_store(store)
    assert result.source_auto_detected is True
    assert result.aligned is True
    assert result.enriched is True
    assert result.alignment_segments == 1
    assert len(examples) == 1
    assert examples[0].source_start == pytest.approx(120.0, abs=1.0)
    assert examples[0].why_selected == "意外な告白から明確なオチまである"
    assert examples[0].weak_negatives
    assert examples[0].acquisition_provenance["provider_version"] == "2026.10.02"
    assert examples[0].acquisition_provenance["provider_channel"] == "nightly"
    assert examples[0].acquisition_provenance["js_runtime"] == "node"
    assert result.acquisition_provider_version == "2026.10.02"
    assert any(document.kind == "positive" for document in documents)
    assert any(document.kind == "weak_negative" for document in documents)


@pytest.mark.asyncio
async def test_ambiguous_source_description_is_not_auto_learned(
    tmp_path: Path,
) -> None:
    database = tmp_path / "holoclip.db"
    with SQLiteCanonicalRepository(database) as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")
        with pytest.raises(SourceResolutionError) as caught:
            await ingest_human_example_from_urls(
                FakeSourceProvider(tmp_path, ambiguous=True),
                clip_url="https://www.youtube.com/watch?v=clip",
                source_url=None,
                download_root=tmp_path / "sources",
                transcript_root=tmp_path / "transcripts",
                examples_store=tmp_path / "human_clips.jsonl",
                groq_settings=GroqTranscriptionSettings(api_key="test"),
                llm=FakeLLM(),
                llm_model="strong",
                llm_reasoning_effort="high",
                embedding_provider=FakeEmbeddingProvider(),
                repository=repository,
                stage_cache=cache,
            )

    assert len(caught.value.candidates) == 2
    assert not (tmp_path / "human_clips.jsonl").exists()
