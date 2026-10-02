from __future__ import annotations

from pathlib import Path

import pytest

from holoclip.example_batch import (
    BatchStatus,
    ExampleBatchItem,
    load_batch_manifest,
    load_batch_state,
    save_batch_state,
    should_skip_succeeded,
    summarize_batch_state,
    update_batch_item_state,
)


def test_load_batch_manifest_and_reject_duplicates(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        "\n".join(
            [
                '{"clip_url":"https://youtu.be/a"}',
                '{"clip_url":"https://youtu.be/b","source_url":"https://youtu.be/source"}',
            ]
        ),
        encoding="utf-8",
    )

    items = load_batch_manifest(manifest)

    assert len(items) == 2
    assert items[1].source_url == "https://youtu.be/source"

    manifest.write_text(
        "\n".join(
            [
                '{"clip_url":"https://youtu.be/a"}',
                '{"clip_url":"https://youtu.be/a"}',
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate clip URL"):
        load_batch_manifest(manifest)


def test_batch_state_skips_only_same_successful_fingerprint(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.jsonl"
    item = ExampleBatchItem(clip_url="https://youtu.be/a")
    state = load_batch_state(
        tmp_path / "missing-state.json",
        manifest_path=manifest,
    )

    update_batch_item_state(
        state,
        item,
        status=BatchStatus.SUCCEEDED,
        example_id=item.id,
    )

    assert should_skip_succeeded(item, state) is True
    assert should_skip_succeeded(item, state, force=True) is False

    changed = ExampleBatchItem(
        clip_url=item.clip_url,
        source_url="https://youtu.be/new-source",
    )
    assert changed.id == item.id
    assert changed.fingerprint != item.fingerprint
    assert should_skip_succeeded(changed, state) is False


def test_batch_state_round_trip_and_attempt_count(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.jsonl"
    state_path = tmp_path / "state.json"
    item = ExampleBatchItem(clip_url="https://youtu.be/a")
    state = load_batch_state(state_path, manifest_path=manifest)

    first = update_batch_item_state(
        state,
        item,
        status=BatchStatus.RETRYABLE,
        last_error="timeout",
    )
    second = update_batch_item_state(
        state,
        item,
        status=BatchStatus.SUCCEEDED,
        example_id=item.id,
    )

    assert first.attempts == 1
    assert second.attempts == 2

    save_batch_state(state_path, state)
    loaded = load_batch_state(state_path, manifest_path=manifest)

    assert loaded.items[item.id].status is BatchStatus.SUCCEEDED
    assert loaded.items[item.id].attempts == 2


def test_summarize_batch_state(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.jsonl"
    state_path = tmp_path / "state.json"
    items = [
        ExampleBatchItem(clip_url="https://youtu.be/a"),
        ExampleBatchItem(clip_url="https://youtu.be/b"),
        ExampleBatchItem(clip_url="https://youtu.be/c"),
    ]
    state = load_batch_state(state_path, manifest_path=manifest)

    update_batch_item_state(state, items[0], status=BatchStatus.SUCCEEDED)
    update_batch_item_state(state, items[1], status=BatchStatus.NEEDS_SOURCE)
    update_batch_item_state(state, items[2], status=BatchStatus.POLICY_BLOCKED)

    summary = summarize_batch_state(
        items,
        state,
        processed=3,
        skipped_existing_success=0,
        state_path=state_path,
    )

    assert summary.total == 3
    assert summary.counts["succeeded"] == 1
    assert summary.counts["needs_source"] == 1
    assert summary.counts["policy_blocked"] == 1
