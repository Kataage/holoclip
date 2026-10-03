from __future__ import annotations

from pathlib import Path

from holoclip.alignment import ALIGNMENT_METHOD
from holoclip.example_audit import (
    build_human_example_audit,
    format_human_example_audit,
    parse_youtube_time_hint,
)
from holoclip.examples import (
    WEAK_NEGATIVE_METHOD,
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
    build_retrieval_document_drafts,
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
        alignment_method=ALIGNMENT_METHOD,
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
        weak_negative_method=WEAK_NEGATIVE_METHOD,
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
    drafts, _ = build_retrieval_document_drafts([example])
    documents = [
        RetrievalDocument(
            **draft.model_dump(),
            embedding_model="embed",
            embedding_version=RETRIEVAL_VERSION,
            embedding=[1.0, float(index + 1)],
        )
        for index, draft in enumerate(drafts)
    ]

    report = build_human_example_audit(
        example,
        retrieval_documents=documents,
    )

    assert len(report.alignment_segments) == 2
    assert report.alignment_segments[0].clip_excerpt == "最初の話 続きの話"
    assert report.alignment_segments[0].source_excerpt == "元配信の最初 元配信の続き"
    assert report.alignment_segments[1].source_gap_from_previous == 60
    assert report.alignment_segments[1].source_delta_from_previous == 60
    assert report.clip_selected_seconds == 30
    assert report.source_selected_seconds == 30
    assert report.source_bounding_seconds == 90
    assert report.source_gap_seconds == 60
    assert report.alignment_quality.clip_timeline_seconds == 30
    assert report.alignment_quality.aligned_clip_seconds == 30
    assert report.alignment_quality.clip_coverage_ratio == 1.0
    assert report.learning_quality.value == "trusted"
    assert report.learning_quality_reasons == []
    assert report.positive_retrieval_eligible is True
    assert report.weak_negatives_eligible == 1
    assert report.weak_negatives_suppressed == 0
    assert report.declared_source_start_hint == 120
    assert report.first_alignment_delta_from_hint == 0
    assert report.retrieval_documents[0].kind == "positive"
    assert report.warnings == []

    rendered = format_human_example_audit(report)
    assert "Alignment #1" in rendered
    assert "removed/gap inside bounding range: 60.0s" in rendered
    assert "clip coverage: 100.0%" in rendered
    assert "decision: trusted" in rendered
    assert "positive retrieval eligible: True" in rendered
    assert "weak negatives eligible: 1" in rendered
    assert "first alignment - declared hint: +0.0s" in rendered
    assert "元配信の最初" in rendered
    assert f"Retrieval documents ({len(documents)})" in rendered


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



def test_audit_warns_on_nonmonotonic_weak_singleton(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.json"
    source_path = tmp_path / "source.json"
    save_transcript(
        [TranscriptSegment(start=0, end=30, text="clip")],
        clip_path,
    )
    save_transcript(
        [TranscriptSegment(start=0, end=1000, text="source")],
        source_path,
    )
    example = HumanClipExample(
        id="bad-order",
        clip_url="https://youtu.be/clip",
        clip_title="bad order",
        source_start=100,
        source_end=900,
        alignment_confidence=0.7,
        alignment_method="transcript-anchor-v2",
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=10,
                source_start=700,
                source_end=710,
                confidence=0.8,
                matched_anchors=4,
            ),
            AlignmentSegmentExample(
                clip_start=10,
                clip_end=16,
                source_start=100,
                source_end=106,
                confidence=0.55,
                matched_anchors=1,
            ),
            AlignmentSegmentExample(
                clip_start=16,
                clip_end=30,
                source_start=800,
                source_end=814,
                confidence=0.8,
                matched_anchors=4,
            ),
        ],
        clip_transcript_path=str(clip_path),
        source_transcript_path=str(source_path),
    )

    report = build_human_example_audit(example)

    assert report.alignment_segments[1].source_delta_from_previous == -610
    assert any("non-monotonic alignment" in item for item in report.warnings)
    assert any("weak isolated alignment" in item for item in report.warnings)
    rendered = format_human_example_audit(report)
    assert "source delta from previous: -610.0s" in rendered
    assert "non-monotonic alignment" in rendered



def test_audit_warns_when_retrieval_documents_are_stale(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.json"
    source_path = tmp_path / "source.json"
    save_transcript(
        [TranscriptSegment(start=0, end=10, text="clip")],
        clip_path,
    )
    save_transcript(
        [TranscriptSegment(start=100, end=110, text="source")],
        source_path,
    )
    example = HumanClipExample(
        id="stale-example",
        clip_url="https://youtu.be/clip",
        clip_title="stale",
        source_start=100,
        source_end=110,
        alignment_confidence=0.9,
        alignment_method="transcript-anchor-v4",
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=10,
                source_start=100,
                source_end=110,
                confidence=0.9,
                matched_anchors=3,
            )
        ],
        clip_transcript_path=str(clip_path),
        source_transcript_path=str(source_path),
    )
    stale = RetrievalDocument(
        id="old-positive",
        kind=RetrievalDocumentKind.POSITIVE,
        example_id=example.id,
        text="setup: old data",
        text_fingerprint="old-fingerprint",
        embedding_model="embed",
        embedding=[1.0],
    )

    report = build_human_example_audit(
        example,
        retrieval_documents=[stale],
    )

    assert any(
        "retrieval index does not match" in warning
        for warning in report.warnings
    )
