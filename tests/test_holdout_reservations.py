import json
from types import SimpleNamespace

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState, grow_corpus
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_reservations import (
    export_holdout_inventory, holdout_inventory, should_reserve_holdout,
)
from holoclip.providers.source.base import SourceCandidate, SourceMetadata
from test_editorial_profile import trusted_example


class Discoverer:
    def __init__(self, streams):
        self.streams = streams

    def search_public_clips(self, query, *, limit, offset):
        return [self.probe(f"https://youtu.be/clip-{i}") for i in range(len(self.streams))]

    def probe(self, url):
        identifier = url.rsplit("/", 1)[-1]
        is_source = identifier.startswith("stream-")
        return SourceMetadata(
            provider="fake", url=url, external_id=identifier,
            title="public source" if is_source else "public clip",
            availability="public", live_status="was_live" if is_source else "not_live",
            channel="editor" if not is_source else "source-channel",
        )

    def source_candidates(self, metadata):
        index = int(metadata.external_id.rsplit("-", 1)[-1])
        return [SourceCandidate(url=f"https://youtu.be/{self.streams[index]}",
                                score=.99, reason="description_explicit_source_label")]


@pytest.mark.asyncio
async def test_autonomous_holdout_reservation_is_source_grouped_and_never_ingested(tmp_path):
    training = tmp_path / "clips.jsonl"
    state_file = tmp_path / "growth.json"
    upsert_human_clip_examples(training, [trusted_example("already", stream_id="old-training")])
    reserved_stream = next(f"stream-{i}" for i in range(1000)
                           if should_reserve_holdout(f"stream-{i}"))
    provider = Discoverer([reserved_stream, reserved_stream])
    called = []

    async def ingest(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(example_id="unexpected")

    result = await grow_corpus(
        provider, store=training, state_path=state_file, ingest=ingest,
        policy_rules=[], scan_limit=5, max_new_examples=2,
    )
    assert result["attempted"] == 0
    assert not called
    state = CorpusGrowthState.model_validate_json(state_file.read_text(encoding="utf-8"))
    assert state.holdout_stream_ids == [reserved_stream]
    assert {x.status for x in state.items.values()} == {"holdout_reserved"}
    report = export_holdout_inventory(training, state_file, tmp_path / "inventory.json")
    assert report["qualification_allowed"] is False
    assert report["reviewed_gold_available"] is False
    assert len(report["reserved_clips"]) == 2
    assert json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8")) == report


@pytest.mark.asyncio
async def test_existing_training_source_never_becomes_holdout(tmp_path):
    stream = next(f"stream-{i}" for i in range(1000)
                  if should_reserve_holdout(f"stream-{i}"))
    store = tmp_path / "clips.jsonl"
    upsert_human_clip_examples(store, [trusted_example("already", stream_id=stream)])
    state_file = tmp_path / "growth.json"

    async def ingest(**kwargs):
        raise RuntimeError("retryable does not promote any data")

    await grow_corpus(
        Discoverer([stream]), store=store, state_path=state_file,
        ingest=ingest, policy_rules=[], scan_limit=5, max_new_examples=1,
    )
    state = CorpusGrowthState.model_validate_json(state_file.read_text(encoding="utf-8"))
    assert state.holdout_stream_ids == []
    assert next(iter(state.items.values())).status == "retryable"


def test_manual_training_overlap_blocks_reservation_instead_of_promoting(tmp_path):
    store = tmp_path / "clips.jsonl"
    state_file = tmp_path / "growth.json"
    state = CorpusGrowthState(
        holdout_stream_ids=["same-stream"],
        items={"clip": CorpusGrowthItem(
            clip_url="https://youtu.be/clip", source_url="https://youtu.be/same-stream",
            source_stream_id="same-stream", query="test", status="holdout_reserved",
        )},
    )
    state_file.write_text(state.model_dump_json(), encoding="utf-8")
    upsert_human_clip_examples(store, [trusted_example("leaked", stream_id="same-stream")])
    report = holdout_inventory(store, state_file)
    assert report["status"] == "BLOCKED_SPLIT_CONFLICT"
    assert report["conflicted_streams"] == ["same-stream"]
    assert report["qualification_allowed"] is False


def test_holdout_hash_is_stable_and_invalid_config_rejected():
    assert should_reserve_holdout("stream-1") == should_reserve_holdout("stream-1")
    with pytest.raises(ValueError):
        should_reserve_holdout("")
    with pytest.raises(ValueError):
        should_reserve_holdout("stream-1", every=1)
