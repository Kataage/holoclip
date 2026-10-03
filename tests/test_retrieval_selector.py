from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.execution import make_input_fingerprint
from holoclip.models import ClipCandidate, TranscriptChunk
from holoclip.providers.embedding.base import EmbeddingProviderError
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import RETRIEVAL_VERSION, RetrievalDocument


class RecordingLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None

    async def chat_json(
        self,
        messages,
        *,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> dict[str, Any]:
        self.messages = list(messages)
        return {
            "ranked": [
                {
                    "index": 0,
                    "confidence": 0.94,
                    "reason": "setupとpayoffが明確",
                    "retrieval_assessment": "positiveに近くweak negativeより構造が強い",
                }
            ]
        }



class GuidedRecordingLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []

    async def chat_json(
        self,
        messages,
        *,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> dict[str, Any]:
        payload = list(messages)
        self.messages.append(payload)
        return {
            "candidates": [
                {
                    "start": 110,
                    "end": 150,
                    "working_title": "baseline duplicate",
                    "setup": "same setup",
                    "development": "same development",
                    "payoff": "same payoff",
                    "why_interesting": "duplicate",
                    "traits": ["duplicate"],
                    "context_requirement": "low",
                    "confidence": 0.99,
                    "warnings": [],
                },
                {
                    "start": 220,
                    "end": 260,
                    "working_title": "guided new moment",
                    "setup": "new setup",
                    "development": "new development",
                    "payoff": "new payoff",
                    "why_interesting": "new mechanism",
                    "traits": ["new"],
                    "context_requirement": "low",
                    "confidence": 0.75,
                    "warnings": [],
                },
            ]
        }


class FakeEmbeddingProvider:
    model = "fake-embed"

    async def embed_passages(self, texts):
        raise AssertionError("not used")

    async def embed_queries(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return (await self.embed_queries([text]))[0]


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    async def embed_queries(self, texts):
        raise EmbeddingProviderError("temporary embedding outage")


def candidate() -> ClipCandidate:
    return ClipCandidate(
        start=100,
        end=160,
        working_title="意外な告白",
        setup="普通の雑談から始まる",
        development="本人が予想外の告白をする",
        payoff="最後に笑えるオチが付く",
        why_interesting="告白からオチまで一本の話になっている",
        traits=["unexpected_confession", "setup_payoff"],
        confidence=0.8,
    )


def retrieval_document(
    *,
    document_id: str,
    kind: str,
    example_id: str,
    text: str,
    vector: list[float],
) -> RetrievalDocument:
    return RetrievalDocument(
        id=document_id,
        kind=kind,
        example_id=example_id,
        text=text,
        text_fingerprint=make_input_fingerprint(text),
        embedding_model="fake-embed",
        embedding_version=RETRIEVAL_VERSION,
        embedding=vector,
        metadata={"reason": "adjacent_unselected_before"} if kind == "weak_negative" else {},
    )


@pytest.mark.asyncio
async def test_final_selector_receives_positive_and_negative_retrieval(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()
    provider = FakeEmbeddingProvider()
    progress: list[str] = []

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="positive-doc",
                kind="positive",
                example_id="positive-example",
                text=(
                    "setup: 普通の雑談\n"
                    "development: 意外な告白\n"
                    "payoff: 最後にオチ\n"
                    "traits: unexpected_confession"
                ),
                vector=[1.0, 0.0],
            )
        )
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="negative-doc",
                kind="weak_negative",
                example_id="negative-parent",
                text="weak_negative_reason: adjacent\nunselected_source_excerpt: オチがない雑談",
                vector=[0.8, 0.2],
            )
        )

        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            embedding_provider=provider,
            retrieval_repository=repository,
            progress=progress.append,
        )
        selected = await detector.final_select([candidate()], top_k=1)

    assert len(selected) == 1
    trace = selected[0].retrieval_trace
    assert trace is not None
    assert trace.embedding_model == "fake-embed"
    assert trace.positives[0].document_id == "positive-doc"
    assert trace.weak_negatives[0].document_id == "negative-doc"
    assert trace.selector_reason == "positiveに近くweak negativeより構造が強い"

    assert llm.messages is not None
    prompt = llm.messages[1]["content"]
    assert "positive-doc" in prompt
    assert "negative-doc" in prompt
    assert "weak_negatives" in prompt
    assert any("Retrieval: attached examples" in line for line in progress)


@pytest.mark.asyncio
async def test_retrieval_failure_falls_back_to_baseline_final_selector(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()
    progress: list[str] = []

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="positive-doc",
                kind="positive",
                example_id="positive-example",
                text="setup: test\npayoff: test",
                vector=[1.0, 0.0],
            )
        )
        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            embedding_provider=FailingEmbeddingProvider(),
            retrieval_repository=repository,
            progress=progress.append,
        )
        selected = await detector.final_select([candidate()], top_k=1)

    assert len(selected) == 1
    assert selected[0].retrieval_trace is None
    assert any("Retrieval unavailable" in line for line in progress)
    assert llm.messages is not None
    assert "positive_examples" not in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_empty_retrieval_index_keeps_candidate_trace_empty(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()
    progress: list[str] = []

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            embedding_provider=FakeEmbeddingProvider(),
            retrieval_repository=repository,
            progress=progress.append,
        )
        selected = await detector.final_select([candidate()], top_k=1)

    assert selected[0].retrieval_trace is None
    assert any("index is empty" in line for line in progress)



@pytest.mark.asyncio
async def test_strict_retrieval_propagates_embedding_failure(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="positive-doc",
                kind="positive",
                example_id="positive-example",
                text="setup: test\npayoff: test",
                vector=[1.0, 0.0],
            )
        )
        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            embedding_provider=FailingEmbeddingProvider(),
            retrieval_repository=repository,
            strict_retrieval=True,
        )

        with pytest.raises(EmbeddingProviderError):
            await detector.final_select([candidate()], top_k=1)



@pytest.mark.asyncio
async def test_guided_discovery_adds_new_candidate_without_replacing_baseline(
    tmp_path: Path,
) -> None:
    llm = GuidedRecordingLLM()
    provider = FakeEmbeddingProvider()
    chunk = TranscriptChunk(
        id="chunk-0000",
        start=0,
        end=300,
        text="長い雑談の中で複数の話題が続く",
        segment_indices=[0],
    )
    baseline = ClipCandidate(
        start=100,
        end=160,
        working_title="baseline moment",
        setup="普通の雑談",
        development="意外な告白",
        payoff="最後にオチ",
        why_interesting="告白からオチまで成立",
        traits=["unexpected_confession"],
        confidence=0.8,
        source_chunk_ids=["chunk-0000"],
    )

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="positive-doc",
                kind="positive",
                example_id="positive-example",
                text=(
                    "setup: 普通の雑談\n"
                    "development: 意外な告白\n"
                    "payoff: 最後にオチ"
                ),
                vector=[1.0, 0.0],
            )
        )
        repository.upsert_retrieval_document(
            retrieval_document(
                document_id="negative-doc",
                kind="weak_negative",
                example_id="negative-example",
                text=(
                    "weak_negative_reason: adjacent\n"
                    "unselected_source_excerpt: オチがない雑談"
                ),
                vector=[0.8, 0.2],
            )
        )

        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            embedding_provider=provider,
            retrieval_repository=repository,
            strict_retrieval=True,
        )
        merged = await detector.discover_guided([chunk], [baseline])

    assert [item.working_title for item in merged] == [
        "baseline moment",
        "guided new moment",
    ]
    assert merged[0].confidence == 0.8
    assert len(llm.messages) == 1
    prompt = llm.messages[0][1]["content"]
    assert "Already-known first-pass candidates" in prompt
    assert "baseline moment" in prompt
    assert "positive-doc" in prompt
    assert "negative-doc" in prompt
    assert "Return only ADDITIONAL candidates" in prompt
