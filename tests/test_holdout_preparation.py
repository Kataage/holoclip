import json
from pathlib import Path
from types import SimpleNamespace

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_preparation import prepare_reserved_holdouts
from holoclip.providers.source.base import AcquiredSource, AcquisitionProfile, SourceCandidate, SourceMetadata
from holoclip.video_measurement import checksum_file
from test_editorial_profile import trusted_example


STREAM = "stream-evaluation"
CLIP = "https://youtu.be/held-clip"
SOURCE = "https://youtu.be/stream-evaluation"


class VideoProvider:
    def __init__(self):
        self.calls = 0
        self.public = True
        self.restriction = False
        self.ambiguous = False
        self.source_changed = False

    def probe(self, url):
        source = url == SOURCE
        return SourceMetadata(
            provider="fake", url=url,
            external_id=("changed" if self.source_changed else STREAM) if source else "held-clip",
            title="do not clip" if self.restriction else "public video",
            availability="public" if self.public else "subscriber_only",
            live_status="was_live" if source else "not_live", channel="editor",
        )

    def source_candidates(self, clip):
        if self.ambiguous:
            return []
        return [SourceCandidate(url=SOURCE, reason="description_explicit_source_label", score=.99)]

    def acquire(self, url, root, *, profile):
        assert profile == AcquisitionProfile.EDITING_VIDEO
        self.calls += 1
        media = root / "held-clip" / "editing_video" / "clip.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"public deterministic finished video")
        return AcquiredSource(metadata=self.probe(url), profile=profile, media_path=media)


def fake_probe(media):
    return {"streams": [{"codec_type": "video"}], "format": {"duration": "50"}}


def fake_measure(media, root, *, asset_id, start, end, transcript, dense_times):
    directory = root / f"window-{int(start):03d}"
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "bounded.mp4"
    video.write_bytes(f"window:{start}:{end}".encode())
    frame = directory / "frame.png"
    frame.write_bytes(b"frame")
    measurement = {
        "window": {
            "id": f"fake-{start}", "source_asset_id": asset_id,
            "source_start": start, "source_end": end, "video_path": str(video),
            "media_sha256": checksum_file(video), "frames": [
                {"id": "frame-1", "shot_id": "shot-1", "pts_seconds": 0.1,
                 "locator": str(frame), "checksum": checksum_file(frame)}
            ],
            "contact_sheet_paths": [], "transcript": {}, "measured_events": {},
            "extraction_version": "fixture",
        },
        "sheet_checksums": {},
    }
    (directory / "measurement.json").write_text(json.dumps(measurement), encoding="utf-8")
    return SimpleNamespace(video_path=str(video))


def files(tmp_path):
    store = tmp_path / "human.jsonl"
    state_path = tmp_path / "state.json"
    output = tmp_path / "out"
    upsert_human_clip_examples(store, [trusted_example("learning", stream_id="different")])
    state = CorpusGrowthState(
        holdout_stream_ids=[STREAM],
        items={"reserved": CorpusGrowthItem(
            clip_url=CLIP, source_url=SOURCE, source_stream_id=STREAM,
            query="test", status="holdout_reserved",
        )},
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return store, state_path, output


def run(provider, store, state, output, *, max_clips=3, policy_rules=None):
    return prepare_reserved_holdouts(
        provider, store=store, state_path=state, output=output,
        max_clips=max_clips, window_seconds=20, policy_rules=policy_rules or [],
        measure=fake_measure, probe=fake_probe,
    )


def test_reserved_finished_clip_is_measured_once_then_reused(tmp_path):
    store, state, output = files(tmp_path)
    provider = VideoProvider()
    first = run(provider, store, state, output)
    assert first["counts"] == {"PREPARED_UNREVIEWED": 1}
    assert provider.calls == 1
    report = json.loads((output / "holdout-preparation.json").read_text(encoding="utf-8"))
    assert report["qualification_allowed"] is False
    assert report["reviewed_gold_available"] is False
    assert report["benchmark_held_out"] is False
    assert report["event_split_verified"] is False
    row = next(iter(report["records"].values()))
    assert len(row["windows"]) == 3
    assert all(Path(path).is_file() for path in row["windows"])
    second = run(provider, store, state, output)
    assert second["counts"] == {"PREPARED_UNREVIEWED": 1}
    assert provider.calls == 1  # every cached frame/clip hash is rechecked
    Path(row["windows"][0]).with_name("frame.png").write_bytes(b"corrupted")
    repaired = run(provider, store, state, output)
    assert repaired["counts"] == {"PREPARED_UNREVIEWED": 1}
    assert provider.calls == 2


def test_train_holdout_collision_blocks_before_network(tmp_path):
    store, state, output = files(tmp_path)
    upsert_human_clip_examples(store, [trusted_example("leak", stream_id=STREAM)])

    class MustNotProbe(VideoProvider):
        def probe(self, url):
            raise AssertionError("split conflict must prevent all probing")

    result = run(MustNotProbe(), store, state, output)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_quarantine_public_metadata_source_identity_and_policy(tmp_path):
    for attribute, expected in [
        ("public", "QUARANTINED_NOT_PUBLIC_ENDED"),
        ("source_changed", "QUARANTINED_SOURCE_CHANGED"),
        ("ambiguous", "QUARANTINED_SOURCE_AMBIGUOUS"),
        ("restriction", "QUARANTINED_POLICY"),
    ]:
        root = tmp_path / attribute
        store, state, output = files(root)
        provider = VideoProvider()
        setattr(provider, attribute, False if attribute == "public" else True)
        result = run(provider, store, state, output)
        assert result["counts"] == {expected: 1}
        assert provider.calls == 0


def test_success_cache_rechecked_against_new_policy(tmp_path):
    store, state, output = files(tmp_path)
    provider = VideoProvider()
    run(provider, store, state, output)
    provider.restriction = True
    result = run(provider, store, state, output)
    assert result["counts"] == {"QUARANTINED_POLICY": 1}
    assert provider.calls == 1
    assert all(not row.get("qualification_allowed", False)
               for row in json.loads((output / "holdout-preparation.json")
                                     .read_text(encoding="utf-8"))["records"].values())


def test_failed_media_probe_is_retryable_without_gold(tmp_path):
    store, state, output = files(tmp_path)
    provider = VideoProvider()

    def failing_probe(media):
        raise RuntimeError("simulated media decode failure")

    result = prepare_reserved_holdouts(
        provider, store=store, state_path=state, output=output, policy_rules=[],
        measure=fake_measure, probe=failing_probe,
    )
    assert result["counts"] == {"RETRYABLE_FAILURE": 1}
    saved = json.loads((output / "holdout-preparation.json").read_text(encoding="utf-8"))
    row = next(iter(saved["records"].values()))
    assert row["error_class"] == "RuntimeError"
    assert "simulated" not in json.dumps(saved)
    assert saved["qualification_allowed"] is False
    assert run(provider, store, state, output)["counts"] == {"PREPARED_UNREVIEWED": 1}
