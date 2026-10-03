from pathlib import Path

import pytest

from holoclip.examples import (
    AlignmentSegmentExample,
    HumanClipExample,
    compute_human_clip_stats,
    load_human_clip_manifest,
    load_human_clip_store,
    make_human_clip_example_id,
    upsert_human_clip_examples,
)


def test_manifest_generates_stable_id(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        """
{
  "examples": [
    {
      "clip_url": "https://www.youtube.com/watch?v=clip",
      "clip_title": "clip title"
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )

    examples = load_human_clip_manifest(path)

    assert examples[0].id == make_human_clip_example_id(
        "https://www.youtube.com/watch?v=clip"
    )


def test_source_range_must_be_complete() -> None:
    with pytest.raises(ValueError):
        HumanClipExample(
            id="x",
            clip_url="https://example.invalid/clip",
            clip_title="clip",
            source_start=10,
        )


def test_store_upserts_and_reports_stats(tmp_path: Path) -> None:
    store = tmp_path / "human_clips.jsonl"
    first = HumanClipExample(
        id="same",
        clip_url="https://example.invalid/clip",
        clip_title="first",
        stream_id="stream-a",
        members=["member-a"],
        source_start=100,
        source_end=130,
        setup="setup",
        development="development",
        payoff="payoff",
    )
    second = first.model_copy(update={"clip_title": "updated"})

    assert upsert_human_clip_examples(store, [first]) == (1, 0, 1)
    assert upsert_human_clip_examples(store, [second]) == (0, 1, 1)

    loaded = load_human_clip_store(store)
    assert loaded[0].clip_title == "updated"

    stats = compute_human_clip_stats(loaded)
    assert stats.total == 1
    assert stats.aligned == 1
    assert stats.unaligned == 0
    assert stats.with_story_structure == 1
    assert stats.trusted_for_learning == 0
    assert stats.needs_review == 1
    assert stats.rejected_for_learning == 0
    assert stats.unique_streams == 1
    assert stats.unique_members == 1


def test_alignment_segments_round_trip_in_store(tmp_path: Path) -> None:
    store = tmp_path / "human_clips.jsonl"
    item = HumanClipExample(
        id="multi",
        clip_url="https://example.invalid/multi",
        clip_title="multi",
        source_start=100,
        source_end=220,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=30,
                source_start=100,
                source_end=130,
                confidence=0.9,
                matched_anchors=4,
            ),
            AlignmentSegmentExample(
                clip_start=30,
                clip_end=60,
                source_start=190,
                source_end=220,
                confidence=0.88,
                matched_anchors=3,
            ),
        ],
    )

    upsert_human_clip_examples(store, [item])
    loaded = load_human_clip_store(store)[0]

    assert len(loaded.alignment_segments) == 2
    assert loaded.alignment_segments[1].source_start == 190
