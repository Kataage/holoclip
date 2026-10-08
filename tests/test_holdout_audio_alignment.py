import json
import shutil

import numpy as np
import pytest

from holoclip.audio_anchor_alignment import (
    VERSION, fingerprint_media, match_features,
)
from holoclip.examples import upsert_human_clip_examples
from holoclip.corpus_growth import CorpusGrowthItem
from holoclip.holdout_preparation import prioritize_unprocessed
from holoclip.holdout_audio_pipeline import align_reserved_holdouts
from test_editorial_profile import trusted_example
from test_holdout_preparation import VideoProvider, files, run


def _features(count, seed=14):
    rng = np.random.default_rng(seed)
    raw = rng.normal(size=(count, 12))
    return (raw / np.linalg.norm(raw, axis=1, keepdims=True)).astype(np.float32)


def test_audio_fingerprint_matches_absolute_source_offsets():
    source = _features(1800)
    clip = source[300:540].copy()
    result = match_features(clip, source)
    assert result["status"] == "PARTIAL_AUDIO_MATCH"
    assert len(result["anchors"]) >= 3
    assert len(result["ranges"]) == 1
    assert abs(result["ranges"][0]["source_start"] - 300 * .25) <= .25
    assert result["ranges"][0]["anchors"] >= 3
    assert result["ranges"][0]["min_correlation"] > .98
    assert all(a["uniqueness_margin"] > .065 for a in result["anchors"])


def test_multiple_audio_cuts_preserved_as_separate_approximate_ranges():
    source = _features(2000)
    clip = np.concatenate([source[200:320], source[650:770], source[1050:1170]], axis=0)
    result = match_features(clip, source)
    assert result["status"] == "PARTIAL_AUDIO_MATCH"
    assert len(result["ranges"]) >= 3
    assert result["ranges"][0]["source_start"] == 50
    assert result["ranges"][1]["source_start"] >= 162
    assert result["ranges"][-1]["source_start"] >= 262
    assert result["source_monotonic"] is True
    assert all(r["min_correlation"] >= .79 for r in result["ranges"])
    assert result["unresolved"]


def test_repeated_source_event_and_unrelated_clip_cannot_establish_unique_match():
    source = _features(500)
    duplicated = np.concatenate([source, source], axis=0)
    clip = source[180:380].copy()
    assert match_features(clip, duplicated)["status"] == "INSUFFICIENT_UNIQUE_ANCHORS"
    assert match_features(_features(200, seed=999), source)["status"] == "INSUFFICIENT_UNIQUE_ANCHORS"
    assert match_features(np.zeros((200, 12), dtype=np.float32), source)["status"] == (
        "INSUFFICIENT_UNIQUE_ANCHORS"
    )


def test_malformed_or_short_audio_is_never_promoted():
    with pytest.raises(ValueError):
        match_features(np.full((80, 12), np.nan), _features(200))
    with pytest.raises(ValueError):
        match_features(_features(100)[:, :10], _features(200))
    assert match_features(_features(10), _features(100))["status"] == "INSUFFICIENT_AUDIO"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_real_ffmpeg_pcm_audio_fingerprint_reuses_verified_cache(tmp_path):
    import math
    import wave

    wav = tmp_path / "original.wav"
    sample_rate = 8000
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        seconds = np.arange(sample_rate * 4) / sample_rate
        signal = .7 * np.sin(2 * math.pi * (150 * seconds + 30 * seconds**2))
        handle.writeframes((signal * 32767).astype("<i2").tobytes())
    first = fingerprint_media(wav, tmp_path / "cache")
    second = fingerprint_media(wav, tmp_path / "cache")
    assert first.shape == second.shape
    assert first.shape[1] == 12
    assert np.allclose(first, second)
    assert len(list((tmp_path / "cache").glob("*.npz"))) == 1
    changed = tmp_path / "changed.wav"
    changed.write_bytes(wav.read_bytes() + b"append")
    # Different bytes cannot alias an existing cache fingerprint path.
    fingerprint_media(changed, tmp_path / "cache")
    assert len(list((tmp_path / "cache").glob("*.npz"))) == 2


def _fake_audio_align(clip, source, root):
    return {"version": VERSION, "status": "PARTIAL_AUDIO_MATCH",
            "anchors": [{"clip_start": 0, "source_start": 10}],
            "ranges": [{"clip_start": 0, "clip_end": 8,
                        "source_start": 10, "source_end": 18}],
            "unresolved": ["No exact cut boundary"],
            "fingerprint_version": VERSION}


def _pipeline(tmp_path, provider=None, align=_fake_audio_align):
    store, state, prepared_root = files(tmp_path)
    provider = provider or VideoProvider()
    assert run(provider, store, state, prepared_root)["counts"] == {
        "PREPARED_UNREVIEWED": 1
    }
    return provider, store, state, prepared_root, align


def _run_pipeline(provider, store, state, prepared, align=_fake_audio_align):
    return align_reserved_holdouts(
        provider, store=store, state_path=state, prepared_root=prepared,
        output=prepared / "audio", policy_rules=[], max_clips=2, align=align,
    )


def test_unattended_source_audio_and_checksum_bound_resume(tmp_path):
    provider, store, state, prepared, _ = _pipeline(tmp_path)
    first = _run_pipeline(provider, store, state, prepared)
    assert first["counts"] == {"PARTIAL_AUDIO_MATCH": 1}
    assert provider.calls == 2  # finished video then one original audio
    data = json.loads((prepared / "audio" / "holdout-audio-alignment.json")
                      .read_text(encoding="utf-8"))
    assert data["qualification_allowed"] is False
    assert data["reviewed_gold_available"] is False
    assert data["event_split_verified"] is False
    assert data["benchmark_held_out"] is False
    row = next(iter(data["records"].values()))
    assert row["status"] == "PARTIAL_AUDIO_MATCH"
    assert row["source_media_sha256"]
    assert _run_pipeline(provider, store, state, prepared)["counts"] == {
        "PARTIAL_AUDIO_MATCH": 1
    }
    assert provider.calls == 3  # source provider rechecks/caches; matcher itself is reused


def test_learning_conflict_prevents_even_metadata_probe(tmp_path):
    provider, store, state, prepared, _ = _pipeline(tmp_path)
    upsert_human_clip_examples(store, [trusted_example("leaked", stream_id="stream-evaluation")])

    class NoNetwork(VideoProvider):
        def probe(self, url):
            raise AssertionError("No network on split collision")

    report = _run_pipeline(NoNetwork(), store, state, prepared)
    assert report["attempted"] == 0
    assert report["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_damaged_finished_measurement_blocks_original_download(tmp_path):
    provider, store, state, prepared, _ = _pipeline(tmp_path)
    original = next(iter(json.loads((prepared / "holdout-preparation.json")
                                    .read_text(encoding="utf-8"))["records"].values()))
    frame = __import__("pathlib").Path(original["windows"][0]).with_name("frame.png")
    frame.write_bytes(b"invalid")
    report = _run_pipeline(provider, store, state, prepared)
    assert report["attempted"] == 0
    assert report["counts"] == {"BLOCKED_UNVERIFIED_FINISHED_VIDEO": 1}
    assert provider.calls == 1


def test_changed_policy_quarantines_before_source_download(tmp_path):
    provider, store, state, prepared, _ = _pipeline(tmp_path)
    provider.restriction = True
    result = _run_pipeline(provider, store, state, prepared)
    assert result["counts"] == {"QUARANTINED_POLICY": 1}
    assert provider.calls == 1


def test_unreviewed_preparation_identity_must_match_reservation(tmp_path):
    provider, store, state, prepared, _ = _pipeline(tmp_path)
    data_path = prepared / "holdout-preparation.json"
    record = json.loads(data_path.read_text(encoding="utf-8"))
    next(iter(record["records"].values()))["source_url"] = "https://youtu.be/changed"
    data_path.write_text(json.dumps(record), encoding="utf-8")
    report = _run_pipeline(provider, store, state, prepared)
    assert report["counts"] == {"BLOCKED_UNVERIFIED_FINISHED_VIDEO": 1}
    assert provider.calls == 1


def test_unattended_queue_prioritizes_unprocessed_sources_over_cached_results():
    candidates = [
        CorpusGrowthItem(clip_url=f"https://youtu.be/{name}", query="test",
                         status="holdout_reserved", source_stream_id=f"stream-{name}")
        for name in ("cached", "failed", "new")
    ]
    report = {
        "youtube:cached": {"status": "PREPARED_UNREVIEWED"},
        "youtube:failed": {"status": "RETRYABLE_FAILURE"},
    }
    ordered = prioritize_unprocessed(candidates, report, {"PREPARED_UNREVIEWED"})
    assert [row.clip_url.rsplit("/", 1)[-1] for row in ordered] == ["new", "failed", "cached"]
