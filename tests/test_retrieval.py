from __future__ import annotations

from pathlib import Path

import pytest

from holoclip.alignment import ALIGNMENT_METHOD
from holoclip.examples import (
    WEAK_NEGATIVE_METHOD,
    AlignmentSegmentExample,
    HumanClipExample,
    WeakNegativeExample,
)
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import (
    RETRIEVAL_VERSION,
    RetrievalDocument,
    build_retrieval_document_drafts,
    index_human_examples,
    retrieval_corpus_fingerprint,
    retrieve_structural_examples,
)


class FakeEmbeddingProvider:
    model = "fake-embed"

    def __init__(self) -> None:
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        return [
            float(lowered.count("告白") + lowered.count("reveal")),
            float(lowered.count("失敗") + lowered.count("failure")),
            float(lowered.count("関係") + lowered.count("relationship")),
            1.0,
        ]

    async def embed_passages(self, texts) -> list[list[float]]:
        batch = list(texts)
        self.passage_calls.append(batch)
        return [self._vector(text) for text in batch]

    async def embed_queries(self, texts) -> list[list[float]]:
        batch = list(texts)
        self.query_calls.extend(batch)
        return [self._vector(text) for text in batch]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]


def enriched_example(
    *,
    example_id: str = "positive-a",
    why_selected: str = "意外な告白からオチまで成立する",
) -> HumanClipExample:
    return HumanClipExample(
        id=example_id,
        clip_url=f"https://example.invalid/{example_id}",
        clip_title="title",
        source_start=100,
        source_end=160,
        alignment_confidence=0.9,
        alignment_method=ALIGNMENT_METHOD,
        alignment_matched_anchors=3,
        alignment_total_anchors=3,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=60,
                source_start=100,
                source_end=160,
                confidence=0.9,
                matched_anchors=3,
            )
        ],
        setup="普通の雑談から始まる",
        development="本人が意外な告白をする",
        payoff="最後に笑えるオチへつながる",
        why_selected=why_selected,
        traits=["unexpected_confession", "reveal", "setup_payoff"],
        topics=["新人時代"],
        members=["member-a"],
        relationship_context="member-a と member-b の先輩後輩関係",
        context_requirement="low",
        weak_negative_method=WEAK_NEGATIVE_METHOD,
        weak_negatives=[
            WeakNegativeExample(
                start=10,
                end=30,
                transcript_excerpt="同じ話題だが展開もオチもない雑談",
                reason="adjacent_unselected_before",
            ),
            WeakNegativeExample(
                start=40,
                end=60,
                transcript_excerpt="失敗したが前振りがなく単発で終わる",
                reason="adjacent_unselected_after",
            ),
        ],
    )


def test_build_retrieval_drafts_uses_abstract_positive_structure() -> None:
    example = enriched_example()
    drafts, skipped = build_retrieval_document_drafts([example])

    assert skipped == 0
    positives = [item for item in drafts if item.kind == "positive"]
    negatives = [item for item in drafts if item.kind == "weak_negative"]
    assert len(positives) == 1
    assert len(negatives) == 2
    assert "setup:" in positives[0].text
    assert "why_selected:" in positives[0].text
    assert "traits:" in positives[0].text
    assert "topics:" not in positives[0].text
    assert "members:" not in positives[0].text
    assert "relationship:" not in positives[0].text
    assert "parent_topics:" not in negatives[0].text
    assert "parent_members:" not in negatives[0].text
    assert "parent_positive_traits:" not in negatives[0].text
    assert example.clip_title not in positives[0].text

    assert positives[0].metadata["members"] == ["member-a"]
    assert positives[0].metadata["topics"] == ["新人時代"]
    assert positives[0].metadata["relationship_context"] == (
        "member-a と member-b の先輩後輩関係"
    )
    assert negatives[0].metadata["members"] == ["member-a"]
    assert negatives[0].metadata["topics"] == ["新人時代"]
    assert negatives[0].metadata["traits"] == [
        "unexpected_confession",
        "reveal",
        "setup_payoff",
    ]


@pytest.mark.asyncio
async def test_index_reuses_unchanged_documents_and_reembeds_changed_only(
    tmp_path: Path,
) -> None:
    provider = FakeEmbeddingProvider()
    database = tmp_path / "holoclip.db"
    example = enriched_example()

    with SQLiteCanonicalRepository(database) as repository:
        first = await index_human_examples(provider, repository, [example])
        second = await index_human_examples(provider, repository, [example])

        changed = example.model_copy(
            update={"why_selected": "意外な告白とrevealが二段階で続く"}
        )
        third = await index_human_examples(provider, repository, [changed])

    assert first.embedded_documents == 3
    assert first.reused_documents == 0
    assert second.embedded_documents == 0
    assert second.reused_documents == 3
    assert third.embedded_documents == 1
    assert third.reused_documents == 2
    assert len(provider.passage_calls) == 2
    assert len(provider.passage_calls[0]) == 3
    assert len(provider.passage_calls[1]) == 1


@pytest.mark.asyncio
async def test_index_removes_documents_for_deleted_examples(tmp_path: Path) -> None:
    provider = FakeEmbeddingProvider()
    database = tmp_path / "holoclip.db"
    first = enriched_example(example_id="a")
    second = enriched_example(example_id="b")

    with SQLiteCanonicalRepository(database) as repository:
        await index_human_examples(provider, repository, [first, second])
        stats = await index_human_examples(provider, repository, [first])
        remaining = repository.list_retrieval_documents(
            embedding_model=provider.model,
            embedding_version=RETRIEVAL_VERSION,
        )

    assert stats.removed_documents == 3
    assert {item.example_id for item in remaining} == {"a"}


@pytest.mark.asyncio
async def test_retrieval_returns_positive_and_negative_separately(
    tmp_path: Path,
) -> None:
    provider = FakeEmbeddingProvider()
    database = tmp_path / "holoclip.db"
    reveal = enriched_example(example_id="reveal")
    failure = HumanClipExample(
        id="failure",
        clip_url="https://example.invalid/failure",
        clip_title="failure",
        source_start=200,
        source_end=260,
        alignment_confidence=0.9,
        alignment_method=ALIGNMENT_METHOD,
        alignment_matched_anchors=3,
        alignment_total_anchors=3,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=60,
                source_start=200,
                source_end=260,
                confidence=0.9,
                matched_anchors=3,
            )
        ],
        setup="自信満々で挑戦する",
        development="すぐに失敗する",
        payoff="強がりとの落差で笑いになる",
        why_selected="confidenceからfailureへの反転が明確",
        traits=["failure_after_confidence"],
        weak_negative_method=WEAK_NEGATIVE_METHOD,
        weak_negatives=[
            WeakNegativeExample(
                start=1,
                end=2,
                transcript_excerpt="普通に成功して終わる",
                reason="similar_unselected_same_source",
            )
        ],
    )

    with SQLiteCanonicalRepository(database) as repository:
        await index_human_examples(provider, repository, [reveal, failure])
        bundle = await retrieve_structural_examples(
            provider,
            repository,
            "本人が突然の告白をしてrevealし最後にオチがある",
            positive_k=2,
            negative_k=2,
        )

    assert len(bundle.positives) == 2
    assert bundle.positives[0].example_id == "reveal"
    assert bundle.positives[0].score >= bundle.positives[1].score
    assert bundle.negatives
    assert all(item.kind == "weak_negative" for item in bundle.negatives)
    assert provider.query_calls == [
        "本人が突然の告白をしてrevealし最後にオチがある"
    ]



@pytest.mark.asyncio
async def test_quality_downgrade_removes_previously_indexed_documents(
    tmp_path: Path,
) -> None:
    provider = FakeEmbeddingProvider()
    database = tmp_path / "holoclip.db"
    example = enriched_example(example_id="quality-gated")

    with SQLiteCanonicalRepository(database) as repository:
        first = await index_human_examples(provider, repository, [example])
        review = example.model_copy(
            update={"alignment_method": "transcript-anchor-v2"}
        )
        second = await index_human_examples(provider, repository, [review])
        remaining = repository.list_retrieval_documents(
            embedding_model=provider.model,
            embedding_version=RETRIEVAL_VERSION,
        )

    assert first.embedded_documents == 3
    assert second.skipped_quality_review == 1
    assert second.skipped_quality_rejected == 0
    assert second.removed_documents == 3
    assert remaining == []



def test_retrieval_corpus_fingerprint_is_order_independent() -> None:
    first = RetrievalDocument(
        id="a",
        kind="positive",
        example_id="example-a",
        text="setup: a",
        text_fingerprint="fp-a",
        embedding_model="model",
        embedding_version=RETRIEVAL_VERSION,
        embedding=[1.0, 0.0],
    )
    second = RetrievalDocument(
        id="b",
        kind="weak_negative",
        example_id="example-b",
        text="weak_negative_reason: b",
        text_fingerprint="fp-b",
        embedding_model="model",
        embedding_version=RETRIEVAL_VERSION,
        embedding=[0.0, 1.0],
    )

    assert retrieval_corpus_fingerprint([first, second]) == (
        retrieval_corpus_fingerprint([second, first])
    )

    changed = second.model_copy(update={"text_fingerprint": "fp-b2"})
    assert retrieval_corpus_fingerprint([first, second]) != (
        retrieval_corpus_fingerprint([first, changed])
    )
