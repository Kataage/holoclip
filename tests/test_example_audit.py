from __future__ import annotations

from pathlib import Path

from holoclip.example_audit import (
    build_human_example_audit,
    format_human_example_audit,
    parse_youtube_time_hint,
)
from holoclip.examples import (
    AlignmentSegmentExample,
    HumanClipExample,
    WeakNegativeExample,
)
from holoclip.models import TranscriptSegment
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.retrieval import (
    RETRIEVAL_VERSION,
    RetrievalDocument,
    RetrievalDocumentKind,
)
from holoclip.transcript import save_transcript


def test_parse_youtube_time_hint() -> None:
    assert parse_youtube_time_hint("https://youtu.be/x?t=3039") == 3039
    assert parse_youtube_time_hint("https://youtu.be/x?t=50m39s") == 3039
    assert parse_youtube_time_hint("https://youtu.be/x?t=1h2m3s") == 3723
    assert parse_youtube_time_hint("https://youtu.be/x") is None


def test_audit_exposes_alignment_transcripts_gaps_and_source_hint(
    tmp_path: Path,
) -> None:
    clip_path = tmp_path / "clip.json"
    source_path = tmp_path / "source.json"
    save_transcript(
        [
            TranscriptSegment(start=0, end=10, text="最初の話"),
            TranscriptSegment(start=10, end=20, text="続きの話"),
            TranscriptSegment(start=20, end=30, text="最後の話"),
        ],
        clip_path,
    )
    save_transcript(
        [
            TranscriptSegment(start=120, end=130, text="元配信の最初"),
            TranscriptSegment(start=130, end=140, text="元配信の続き"),
            TranscriptSegment(start=200, end=210, text="離れた次の場面"),
        ],
        source_path,
    )
    example = HumanClipExample(
        id="audit-example",
        clip_url="https://youtu.be/clip",
        clip_title="audit clip",
        source_url="https://youtu.be/source",
        source_title="source stream",
        stream_id="source",
        source_start=120,
        source_end=210,
        alignment_confidence=0.82,
        alignment_method="transcript-anchor-v2",
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=20,
                source_start=120,
                source_end=140,
                confidence=0.9,
                matched_anchors=4,
            ),
            AlignmentSegmentExample(
                clip_start=20,
                clip_end=30,
                source_start=200,
                source_end=210,
                confidence=0.8,
                matched_anchors=3,
            ),
        ],
        clip_transcript_path=str(clip_path),
        source_transcript_path=str(source_path),
        setup="前振り",
        development="展開",
        payoff="オチ",
        why_selected="構成が明確",
        weak_negatives=[
            WeakNegativeExample(
                start=140,
                end=200,
                transcript_excerpt="切られた中間部分",
                reason="removed_between_selected_segments",
            )
        ],
        acquisition_provenance={
            "resolved_source_url": "https://youtu.be/source?t=120",
            "source_auto_detected": True,
            "source_resolution_reason": "description_explicit_source_label",
            "source_resolution_score": 0.9,
        },
    )
    document = RetrievalDocument(
        id="retr-a",
        kind=RetrievalDocumentKind.POSITIVE,
        example_id=example.id,
        text="setup: 前振り\npayoff: オチ",
        text_fingerprint="fp",
        embedding_model="embed",
        embedding_version=RETRIEVAL_VERSION,
        embedding=[1.0, 0.0],
    )

    report = build_human_example_audit(
        example,
        retrieval_documents=[document],
    )

    assert len(report.alignment_segments) == 2
    assert report.alignment_segments[0].clip_excerpt == "最初の話 続きの話"
    assert report.alignment_segments[0].source_excerpt == "元配信の最初 元配信の続き"
    assert report.alignment_segments[1].source_gap_from_previous == 60
    assert report.clip_selected_seconds == 30
    assert report.source_selected_seconds == 30
    assert report.source_bounding_seconds == 90
    assert report.source_gap_seconds == 60
    assert report.declared_source_start_hint == 120
    assert report.first_alignment_delta_from_hint == 0
    assert report.retrieval_documents[0].kind == "positive"
    assert report.warnings == []

    rendered = format_human_example_audit(report)
    assert "Alignment #1" in rendered
    assert "removed/gap inside bounding range: 60.0s" in rendered
    assert "first alignment - declared hint: +0.0s" in rendered
    assert "元配信の最初" in rendered
    assert "Retrieval documents (1)" in rendered


def test_repository_lists_retrieval_documents_for_example(tmp_path: Path) -> None:
    with SQLiteCanonicalRepository(tmp_path / "db.sqlite") as repository:
        for example_id in ("a", "b"):
            repository.upsert_retrieval_document(
                RetrievalDocument(
                    id=f"retr-{example_id}",
                    kind=RetrievalDocumentKind.POSITIVE,
                    example_id=example_id,
                    text=f"example {example_id}",
                    text_fingerprint=f"fp-{example_id}",
                    embedding_model="embed",
                    embedding=[1.0],
                )
            )

        documents = repository.list_retrieval_documents_for_example("a")

    assert len(documents) == 1
    assert documents[0].example_id == "a"
