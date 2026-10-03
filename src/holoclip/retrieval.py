from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from holoclip.domain import SourceMoment, stable_id
from holoclip.example_quality import (
    LearningQuality,
    evaluate_human_example_learning_quality,
)
from holoclip.examples import WEAK_NEGATIVE_METHOD, HumanClipExample
from holoclip.execution import make_input_fingerprint
from holoclip.models import ClipCandidate
from holoclip.providers.embedding.base import EmbeddingProvider


RETRIEVAL_VERSION = "human-structure-v2"


class RetrievalDocumentKind(StrEnum):
    POSITIVE = "positive"
    WEAK_NEGATIVE = "weak_negative"


class RetrievalDocument(BaseModel):
    id: str
    kind: RetrievalDocumentKind
    example_id: str
    text: str
    text_fingerprint: str
    embedding_model: str
    embedding_version: str = RETRIEVAL_VERSION
    embedding: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalDocumentDraft(BaseModel):
    id: str
    kind: RetrievalDocumentKind
    example_id: str
    text: str
    text_fingerprint: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalHit(BaseModel):
    document_id: str
    example_id: str
    kind: RetrievalDocumentKind
    score: float
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalBundle(BaseModel):
    query_text: str
    embedding_model: str
    embedding_version: str = RETRIEVAL_VERSION
    positives: list[RetrievalHit] = Field(default_factory=list)
    negatives: list[RetrievalHit] = Field(default_factory=list)


class IndexBuildStats(BaseModel):
    examples_seen: int = 0
    positive_documents: int = 0
    weak_negative_documents: int = 0
    embedded_documents: int = 0
    reused_documents: int = 0
    removed_documents: int = 0
    skipped_unenriched_examples: int = 0
    skipped_quality_review: int = 0
    skipped_quality_rejected: int = 0
    suppressed_legacy_weak_negative_documents: int = 0


def retrieval_corpus_fingerprint(
    documents: list[RetrievalDocument],
) -> str:
    payload = [
        {
            "id": document.id,
            "kind": document.kind.value,
            "example_id": document.example_id,
            "text_fingerprint": document.text_fingerprint,
            "embedding_model": document.embedding_model,
            "embedding_version": document.embedding_version,
        }
        for document in sorted(documents, key=lambda item: item.id)
    ]
    return make_input_fingerprint(payload)


def _clean_list(values: list[str]) -> str:
    return ", ".join(sorted({value.strip() for value in values if value.strip()}))


def human_example_structural_text(example: HumanClipExample) -> str | None:
    core = [
        ("setup", example.setup),
        ("development", example.development),
        ("payoff", example.payoff),
        ("why_selected", example.why_selected),
    ]
    if not any(value.strip() for _, value in core):
        return None

    lines = [f"{label}: {value.strip()}" for label, value in core if value.strip()]
    if example.traits:
        lines.append(f"traits: {_clean_list(example.traits)}")
    lines.append(f"context_requirement: {example.context_requirement}")
    return "\n".join(lines)


def weak_negative_structural_text(
    example: HumanClipExample,
    index: int,
) -> str:
    negative = example.weak_negatives[index]
    return "\n".join(
        [
            f"weak_negative_reason: {negative.reason}",
            f"unselected_source_excerpt: {negative.transcript_excerpt.strip()}",
        ]
    )


def build_retrieval_document_drafts(
    examples: list[HumanClipExample],
) -> tuple[list[RetrievalDocumentDraft], int]:
    drafts: list[RetrievalDocumentDraft] = []
    skipped = 0

    for example in examples:
        quality = evaluate_human_example_learning_quality(example)
        if quality.decision is not LearningQuality.TRUSTED:
            continue

        positive_text = human_example_structural_text(example)
        if positive_text is None:
            skipped += 1
        else:
            drafts.append(
                RetrievalDocumentDraft(
                    id=stable_id("retr", RETRIEVAL_VERSION, "positive", example.id),
                    kind=RetrievalDocumentKind.POSITIVE,
                    example_id=example.id,
                    text=positive_text,
                    text_fingerprint=make_input_fingerprint(positive_text),
                    metadata={
                        "clip_title": example.clip_title,
                        "clip_channel": example.clip_channel,
                        "members": example.members,
                        "topics": example.topics,
                        "traits": example.traits,
                        "context_requirement": example.context_requirement,
                        "source_event_id": example.source_event_id,
                        "relationship_context": example.relationship_context,
                    },
                )
            )

        if example.weak_negative_method == WEAK_NEGATIVE_METHOD:
            for index, negative in enumerate(example.weak_negatives):
                text = weak_negative_structural_text(example, index)
                drafts.append(
                    RetrievalDocumentDraft(
                        id=stable_id(
                            "retr",
                            RETRIEVAL_VERSION,
                            "weak_negative",
                            example.id,
                            index,
                            negative.reason,
                            round(negative.start, 3),
                            round(negative.end, 3),
                        ),
                        kind=RetrievalDocumentKind.WEAK_NEGATIVE,
                        example_id=example.id,
                        text=text,
                        text_fingerprint=make_input_fingerprint(text),
                        metadata={
                            "reason": negative.reason,
                            "start": negative.start,
                            "end": negative.end,
                            "members": example.members,
                            "topics": example.topics,
                            "traits": example.traits,
                            "relationship_context": example.relationship_context,
                        },
                    )
                )

    return drafts, skipped


def clip_candidate_query_text(candidate: ClipCandidate) -> str:
    lines = [
        f"setup: {candidate.setup.strip()}",
        f"development: {candidate.development.strip()}",
        f"payoff: {candidate.payoff.strip()}",
        f"why_interesting: {candidate.why_interesting.strip()}",
    ]
    if candidate.traits:
        lines.append(f"traits: {_clean_list(candidate.traits)}")
    lines.append(f"context_requirement: {candidate.context_requirement}")
    return "\n".join(line for line in lines if line.split(": ", 1)[-1].strip())


def source_moment_query_text(moment: SourceMoment) -> str:
    lines = [f"summary: {moment.summary.strip()}"]
    if moment.traits:
        lines.append(f"traits: {_clean_list(moment.traits)}")
    if moment.topics:
        lines.append(f"topics: {_clean_list(moment.topics)}")
    if moment.entities:
        lines.append(f"entities: {_clean_list(moment.entities)}")
    lines.append(f"moment_type: {moment.moment_type}")
    return "\n".join(lines)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimensions must match and be non-empty")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


async def index_human_examples(
    provider: EmbeddingProvider,
    repository: Any,
    examples: list[HumanClipExample],
) -> IndexBuildStats:
    quality = [
        evaluate_human_example_learning_quality(example).decision
        for example in examples
    ]
    drafts, skipped = build_retrieval_document_drafts(examples)
    stats = IndexBuildStats(
        examples_seen=len(examples),
        skipped_unenriched_examples=skipped,
        skipped_quality_review=sum(
            1 for decision in quality if decision is LearningQuality.REVIEW
        ),
        skipped_quality_rejected=sum(
            1 for decision in quality if decision is LearningQuality.REJECTED
        ),
        suppressed_legacy_weak_negative_documents=sum(
            len(example.weak_negatives)
            for example, decision in zip(examples, quality, strict=True)
            if decision is LearningQuality.TRUSTED
            and example.weak_negative_method != WEAK_NEGATIVE_METHOD
        ),
        positive_documents=sum(
            1 for draft in drafts if draft.kind is RetrievalDocumentKind.POSITIVE
        ),
        weak_negative_documents=sum(
            1 for draft in drafts if draft.kind is RetrievalDocumentKind.WEAK_NEGATIVE
        ),
    )

    to_embed: list[RetrievalDocumentDraft] = []
    for draft in drafts:
        existing = repository.get_retrieval_document(draft.id)
        if (
            existing is not None
            and existing.text_fingerprint == draft.text_fingerprint
            and existing.embedding_model == provider.model
            and existing.embedding_version == RETRIEVAL_VERSION
        ):
            stats.reused_documents += 1
            continue
        to_embed.append(draft)

    if to_embed:
        vectors = await provider.embed_passages([draft.text for draft in to_embed])
        if len(vectors) != len(to_embed):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        for draft, vector in zip(to_embed, vectors, strict=True):
            repository.upsert_retrieval_document(
                RetrievalDocument(
                    **draft.model_dump(),
                    embedding_model=provider.model,
                    embedding_version=RETRIEVAL_VERSION,
                    embedding=vector,
                )
            )
            stats.embedded_documents += 1

    active_ids = {draft.id for draft in drafts}
    stats.removed_documents = repository.delete_retrieval_documents_not_in(
        embedding_model=provider.model,
        embedding_version=RETRIEVAL_VERSION,
        active_ids=active_ids,
    )
    return stats


def _rank_bundle(
    *,
    query_text: str,
    query_vector: list[float],
    documents: list[RetrievalDocument],
    embedding_model: str,
    positive_k: int,
    negative_k: int,
) -> RetrievalBundle:
    positives: list[RetrievalHit] = []
    negatives: list[RetrievalHit] = []
    for document in documents:
        score = cosine_similarity(query_vector, document.embedding)
        hit = RetrievalHit(
            document_id=document.id,
            example_id=document.example_id,
            kind=document.kind,
            score=score,
            text=document.text,
            metadata=document.metadata,
        )
        if document.kind is RetrievalDocumentKind.POSITIVE:
            positives.append(hit)
        else:
            negatives.append(hit)

    positives.sort(key=lambda item: item.score, reverse=True)
    negatives.sort(key=lambda item: item.score, reverse=True)
    return RetrievalBundle(
        query_text=query_text,
        embedding_model=embedding_model,
        positives=positives[:positive_k],
        negatives=negatives[:negative_k],
    )


async def retrieve_structural_examples_many(
    provider: EmbeddingProvider,
    repository: Any,
    query_texts: list[str],
    *,
    positive_k: int = 3,
    negative_k: int = 3,
) -> list[RetrievalBundle]:
    if positive_k < 0 or negative_k < 0:
        raise ValueError("retrieval K values must be non-negative")
    queries = [query.strip() for query in query_texts]
    if any(not query for query in queries):
        raise ValueError("retrieval query must not be empty")
    if not queries:
        return []

    documents = repository.list_retrieval_documents(
        embedding_model=provider.model,
        embedding_version=RETRIEVAL_VERSION,
    )
    if not documents:
        return [
            RetrievalBundle(
                query_text=query,
                embedding_model=provider.model,
            )
            for query in queries
        ]

    query_vectors = await provider.embed_queries(queries)
    if len(query_vectors) != len(queries):
        raise RuntimeError("embedding provider returned an unexpected query vector count")

    return [
        _rank_bundle(
            query_text=query,
            query_vector=vector,
            documents=documents,
            embedding_model=provider.model,
            positive_k=positive_k,
            negative_k=negative_k,
        )
        for query, vector in zip(queries, query_vectors, strict=True)
    ]


async def retrieve_structural_examples(
    provider: EmbeddingProvider,
    repository: Any,
    query_text: str,
    *,
    positive_k: int = 3,
    negative_k: int = 3,
) -> RetrievalBundle:
    return (
        await retrieve_structural_examples_many(
            provider,
            repository,
            [query_text],
            positive_k=positive_k,
            negative_k=negative_k,
        )
    )[0]
