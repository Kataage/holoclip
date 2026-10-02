from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from holoclip.domain import (
    CanonicalTranscriptSegment,
    ClipTimeline,
    EditorialIdea,
    EvidenceItem,
    ReviewRecord,
    SourceAsset,
    SourceMoment,
    TimelineSegment,
    make_editorial_idea_id,
    make_review_record_id,
    make_source_asset_id,
    make_source_moment_id,
    make_timeline_segment_id,
    make_transcript_segment_id,
)
from holoclip.repository import RepositoryVersionError, SQLiteCanonicalRepository


def source(external_id: str = "stream-a") -> SourceAsset:
    item_id = make_source_asset_id("youtube", external_id)
    return SourceAsset(
        id=item_id,
        platform="youtube",
        external_id=external_id,
        canonical_url=f"https://www.youtube.com/watch?v={external_id}",
        title=f"title {external_id}",
        access_class="public",
        availability="available",
        source_type="stream",
    )


def test_empty_database_migrates_to_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"

    with SQLiteCanonicalRepository(path) as repository:
        row = repository._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()

    assert row is not None
    assert row["value"] == "3"


def test_source_transcript_evidence_and_moment_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"
    asset = source()
    segment = CanonicalTranscriptSegment(
        id=make_transcript_segment_id(asset.id, 10, 15, "こんにちは"),
        source_asset_id=asset.id,
        start=10,
        end=15,
        text="こんにちは",
        speaker="A",
    )
    evidence = EvidenceItem(
        id="ev_1",
        source_asset_id=asset.id,
        kind="transcript_semantic",
        start=10,
        end=15,
        payload={"trait": "reveal"},
        confidence=0.8,
        producer="test",
    )
    moment = SourceMoment(
        id=make_source_moment_id(asset.id, 10, 15, "驚きの告白"),
        source_asset_id=asset.id,
        evidence_ids=[evidence.id],
        transcript_segment_ids=[segment.id],
        start=10,
        end=15,
        summary="驚きの告白",
        traits=["reveal"],
        confidence=0.9,
    )

    with SQLiteCanonicalRepository(path) as repository:
        repository.upsert_source_asset(asset)
        repository.upsert_transcript_segment(segment)
        repository.upsert_evidence(evidence)
        repository.upsert_source_moment(moment)

        assert repository.get_source_asset(asset.id) == asset
        assert repository.get_transcript_segment(segment.id) == segment
        assert repository.get_evidence(evidence.id) == evidence
        assert repository.get_source_moment(moment.id) == moment


def test_upsert_is_idempotent_and_updates_payload(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"
    asset = source()

    with SQLiteCanonicalRepository(path) as repository:
        repository.upsert_source_asset(asset)
        repository.upsert_source_asset(asset)
        updated = asset.model_copy(update={"title": "updated title"})
        repository.upsert_source_asset(updated)

        count = repository._connection.execute(
            "SELECT COUNT(*) AS count FROM source_assets"
        ).fetchone()
        assert count is not None
        assert count["count"] == 1
        assert repository.get_source_asset(asset.id) == updated


def test_multisource_timeline_relationships_survive_reload(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"
    first = source("stream-a")
    second = source("stream-b")
    segments = [
        TimelineSegment(
            id=make_timeline_segment_id(first.id, 100, 130, 0),
            source_asset_id=first.id,
            source_start=100,
            source_end=130,
            output_order=0,
            purpose="setup",
        ),
        TimelineSegment(
            id=make_timeline_segment_id(second.id, 500, 540, 1),
            source_asset_id=second.id,
            source_start=500,
            source_end=540,
            output_order=1,
            purpose="payoff",
        ),
    ]
    timeline = ClipTimeline(
        id="timeline_multi",
        segments=segments,
        estimated_duration=70,
    )

    with SQLiteCanonicalRepository(path) as repository:
        repository.upsert_source_asset(first)
        repository.upsert_source_asset(second)
        repository.upsert_clip_timeline(timeline)

    with SQLiteCanonicalRepository(path) as repository:
        assert repository.get_clip_timeline(timeline.id) == timeline
        assert repository.timeline_ids_for_source(first.id) == [timeline.id]
        assert repository.timeline_ids_for_source(second.id) == [timeline.id]


def test_editorial_idea_relationships_survive_reload(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"
    first = source("stream-a")
    second = source("stream-b")
    moment = SourceMoment(
        id=make_source_moment_id(first.id, 100, 140, "昔の発言"),
        source_asset_id=first.id,
        start=100,
        end=140,
        summary="昔の発言",
    )
    idea = EditorialIdea(
        id=make_editorial_idea_id(
            "historical_callback",
            [moment.id],
            "昔の発言を今の展開につなぐ",
        ),
        archetype="historical_callback",
        thesis="昔の発言を今の展開につなぐ",
        viewer_promise="過去を知らない人にも流れが分かる",
        hook="昔は絶対やらないと言っていた",
        target_duration_range={
            "minimum_seconds": 120,
            "maximum_seconds": 480,
        },
        required_source_asset_ids=[first.id],
        optional_source_asset_ids=[second.id],
        source_moment_ids=[moment.id],
        generation_horizon="historical",
    )

    with SQLiteCanonicalRepository(path) as repository:
        repository.upsert_source_asset(first)
        repository.upsert_source_asset(second)
        repository.upsert_source_moment(moment)
        repository.upsert_editorial_idea(idea)

    with SQLiteCanonicalRepository(path) as repository:
        assert repository.get_editorial_idea(idea.id) == idea
        assert repository.idea_ids_for_source(first.id) == [idea.id]
        assert repository.idea_ids_for_source(second.id) == [idea.id]


def test_review_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "holoclip.db"
    created_at = "2026-10-02T18:00:00+09:00"
    record = ReviewRecord(
        id=make_review_record_id("editorial_idea", "idea_1", created_at),
        target_type="editorial_idea",
        target_id="idea_1",
        decision="revise",
        reasons=["context_missing"],
        freeform_note="過去発言を追加したい",
        created_at=created_at,
    )

    with SQLiteCanonicalRepository(path) as repository:
        repository.upsert_review_record(record)
        assert repository.get_review_record(record.id) == record


def test_export_and_import_jsonl(tmp_path: Path) -> None:
    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    export_path = tmp_path / "sources.jsonl"
    asset = source()

    with SQLiteCanonicalRepository(first_db) as repository:
        repository.upsert_source_asset(asset)
        assert repository.export_jsonl("source_assets", export_path) == 1

    with SQLiteCanonicalRepository(second_db) as repository:
        assert repository.import_jsonl("source_assets", export_path) == 1
        assert repository.get_source_asset(asset.id) == asset


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '999')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RepositoryVersionError, match="newer than supported"):
        SQLiteCanonicalRepository(path)


def test_version_one_database_migrates_execution_tables(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')"
    )
    connection.commit()
    connection.close()

    with SQLiteCanonicalRepository(path) as repository:
        version = repository._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        stage_table = repository._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stage_runs'"
        ).fetchone()
        artifact_table = repository._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='artifact_records'"
        ).fetchone()
        retrieval_table = repository._connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='retrieval_documents'"
        ).fetchone()

    assert version is not None
    assert version["value"] == "3"
    assert stage_table is not None
    assert artifact_table is not None
    assert retrieval_table is not None


def test_version_two_database_migrates_retrieval_table(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2')"
    )
    connection.commit()
    connection.close()

    with SQLiteCanonicalRepository(path) as repository:
        version = repository._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        retrieval_table = repository._connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='retrieval_documents'"
        ).fetchone()

    assert version is not None
    assert version["value"] == "3"
    assert retrieval_table is not None
