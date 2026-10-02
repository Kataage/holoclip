from __future__ import annotations

import json
from pathlib import Path

from holoclip.domain import SourcePlatform, make_source_asset_id
from holoclip.domain_adapters import candidate_to_source_moment
from holoclip.evaluation import build_run, save_run
from holoclip.examples import (
    AlignmentSegmentExample,
    HumanClipExample,
)
from holoclip.migration import migrate_legacy_data
from holoclip.models import ClipCandidate, TranscriptSegment
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.transcript import save_transcript


def candidate() -> ClipCandidate:
    return ClipCandidate(
        start=100,
        end=130,
        working_title="給料の話",
        setup="前振り",
        development="告白",
        payoff="オチ",
        why_interesting="意外な告白からオチまである",
        traits=["unexpected_confession"],
        confidence=0.9,
    )


def test_evaluation_run_migrates_idempotently_without_modifying_legacy_files(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    transcript_path = tmp_path / "transcript.json"
    media_path = tmp_path / "stream.mkv"
    media_path.write_bytes(b"video")

    save_transcript(
        [
            TranscriptSegment(start=95, end=105, text="前振り"),
            TranscriptSegment(start=105, end=120, text="本題"),
            TranscriptSegment(start=120, end=135, text="オチ"),
        ],
        transcript_path,
    )
    run_path = runs_dir / "stream-a.json"
    run = build_run(
        stream_id="stream-a",
        transcript_path=str(transcript_path),
        media_path=str(media_path),
        candidates=[candidate()],
    )
    save_run(run, run_path)
    original_run_bytes = run_path.read_bytes()
    original_transcript_bytes = transcript_path.read_bytes()

    examples_store = tmp_path / "missing-examples.jsonl"
    database = tmp_path / "holoclip.db"

    with SQLiteCanonicalRepository(database) as repository:
        first = migrate_legacy_data(
            repository,
            runs_dir=runs_dir,
            examples_store=examples_store,
        )
        second = migrate_legacy_data(
            repository,
            runs_dir=runs_dir,
            examples_store=examples_store,
        )

        source_id = make_source_asset_id("local", "stream-a")
        source = repository.get_source_asset(source_id)
        assert source is not None

        canonical_candidate = candidate_to_source_moment(
            source_id,
            candidate(),
        )
        moment = repository.get_source_moment(canonical_candidate.id)
        assert moment is not None
        assert moment.start == 100
        assert moment.end == 130
        assert len(moment.transcript_segment_ids) == 3

        counts = {
            "sources": repository._connection.execute(
                "SELECT COUNT(*) AS n FROM source_assets"
            ).fetchone()["n"],
            "segments": repository._connection.execute(
                "SELECT COUNT(*) AS n FROM transcript_segments"
            ).fetchone()["n"],
            "moments": repository._connection.execute(
                "SELECT COUNT(*) AS n FROM source_moments"
            ).fetchone()["n"],
        }

    assert first.evaluation_runs_seen == 1
    assert first.source_moments_processed == 1
    assert second.evaluation_runs_seen == 1
    assert counts == {"sources": 1, "segments": 3, "moments": 1}
    assert run_path.read_bytes() == original_run_bytes
    assert transcript_path.read_bytes() == original_transcript_bytes


def test_human_examples_preserve_continuous_and_multicut_timelines(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    examples_store = tmp_path / "human_clips.jsonl"

    continuous = HumanClipExample(
        id="continuous",
        clip_url="https://www.youtube.com/watch?v=clip-a",
        clip_title="continuous",
        source_url="https://www.youtube.com/watch?v=source-a",
        source_title="source",
        stream_id="source-a",
        source_start=100,
        source_end=140,
        alignment_confidence=0.91,
    )
    multicut = HumanClipExample(
        id="multicut",
        clip_url="https://www.youtube.com/watch?v=clip-b",
        clip_title="multicut",
        source_url="https://www.youtube.com/watch?v=source-a",
        source_title="source",
        stream_id="source-a",
        source_start=100,
        source_end=240,
        alignment_confidence=0.9,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=30,
                source_start=100,
                source_end=130,
                confidence=0.95,
                matched_anchors=4,
            ),
            AlignmentSegmentExample(
                clip_start=30,
                clip_end=60,
                source_start=210,
                source_end=240,
                confidence=0.92,
                matched_anchors=3,
            ),
        ],
    )
    examples_store.write_text(
        "\n".join(
            json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
            for item in [continuous, multicut]
        )
        + "\n",
        encoding="utf-8",
    )
    original_bytes = examples_store.read_bytes()

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        report = migrate_legacy_data(
            repository,
            runs_dir=runs_dir,
            examples_store=examples_store,
        )
        source_id = make_source_asset_id(SourcePlatform.YOUTUBE, "source-a")
        timeline_ids = repository.timeline_ids_for_source(source_id)
        timelines = [
            repository.get_clip_timeline(timeline_id)
            for timeline_id in timeline_ids
        ]

    assert report.human_examples_seen == 2
    assert report.clip_timelines_processed == 2
    assert len(timeline_ids) == 2
    assert sorted(len(item.segments) for item in timelines if item is not None) == [1, 2]
    assert examples_store.read_bytes() == original_bytes


def test_malformed_and_ambiguous_examples_are_reported_without_guessing(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    examples_store = tmp_path / "human_clips.jsonl"

    ambiguous = HumanClipExample(
        id="ambiguous",
        clip_url="https://example.invalid/clip",
        clip_title="ambiguous",
        source_start=10,
        source_end=20,
    )
    valid = HumanClipExample(
        id="valid",
        clip_url="https://www.youtube.com/watch?v=clip",
        clip_title="valid",
        source_url="https://youtu.be/source-ok",
        source_start=20,
        source_end=40,
        alignment_confidence=0.8,
    )
    examples_store.write_text(
        "\n".join(
            [
                "{not-json}",
                json.dumps(ambiguous.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(valid.model_dump(mode="json"), ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with SQLiteCanonicalRepository(tmp_path / "holoclip.db") as repository:
        report = migrate_legacy_data(
            repository,
            runs_dir=runs_dir,
            examples_store=examples_store,
        )

        valid_source_id = make_source_asset_id(
            SourcePlatform.YOUTUBE,
            "source-ok",
        )
        assert repository.get_source_asset(valid_source_id) is not None
        assert len(repository.timeline_ids_for_source(valid_source_id)) == 1

    messages = [item.message for item in report.diagnostics]
    assert report.errors == 2
    assert any("invalid human example" in message for message in messages)
    assert any("source identity is ambiguous" in message for message in messages)
    assert report.clip_timelines_processed == 1
