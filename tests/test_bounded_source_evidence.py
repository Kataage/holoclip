import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from holoclip.bounded_source_evidence import (
    _valid_ranges, measure_reserved_source_pairs,
)
from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from holoclip.providers.source.base import AcquiredVideoRange, SourceMetadata
from holoclip.providers.source.yt_dlp import YtDlpSourceProvider
from test_editorial_profile import trusted_example
from test_holdout_audio_alignment import _pipeline, _run_pipeline
from test_holdout_preparation import fake_measure


def _source_evidence(tmp_path, provider=None):
    provider, store, state, prepared, _ = _pipeline(tmp_path, provider=provider)
    assert _run_pipeline(provider, store, state, prepared)["counts"] == {"PARTIAL_AUDIO_MATCH": 1}
    audio_root = prepared / "audio"
    audio_path = audio_root / "holdout-audio-alignment.json"
    data = json.loads(audio_path.read_text(encoding="utf-8"))
    row = next(iter(data["records"].values()))
    row.update({
        "anchors": [
            {"clip_start": t, "clip_end": t + 6,
             "source_start": t + 40, "source_end": t + 46,
             "correlation": .95, "uniqueness_margin": .11}
            for t in (0, 9, 18)
        ],
        "ranges": [{"clip_start": 0, "clip_end": 24, "source_start": 40,
                    "source_end": 64, "anchors": 3, "min_correlation": .95}],
    })
    audio_path.write_text(json.dumps(data), encoding="utf-8")
    return provider, store, state, prepared, audio_root


class SectionProvider:
    """Fake source wrapper; delegates source lookups, never whole-stream video."""

    def __init__(self, underlying):
        self.underlying = underlying
        self.range_calls = []
        self.last_seconds = 0
        self.policy_block = False

    def probe(self, url):
        data = self.underlying.probe(url)
        if self.policy_block and "stream" in url:
            return data.model_copy(update={"title": "do not clip source"})
        return data

    def source_candidates(self, metadata):
        return self.underlying.source_candidates(metadata)

    def acquire_video_range(self, url, output_root, *, start, end):
        self.range_calls.append((url, start, end))
        self.last_seconds = end - start
        media = output_root / "source" / f"{start:.3f}-{end:.3f}" / "section.mkv"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"deterministic bounded original video")
        return AcquiredVideoRange(
            metadata=self.probe(url), media_path=media,
            requested_start=start, requested_end=end,
        )


def _probe_for(provider):
    def probe(path):
        return {"streams": [{"codec_type": "video"}],
                "format": {"duration": str(provider.last_seconds)}}
    return probe


def measured_fixture(*args, **kwargs):
    window = fake_measure(*args, **kwargs)
    manifest = json.loads(
        (Path(window.video_path).parent / "measurement.json").read_text(encoding="utf-8")
    )
    return SimpleNamespace(
        video_path=window.video_path,
        media_sha256=manifest["window"]["media_sha256"],
    )


def _execute(provider, store, state, prepared, audio_root, **kwargs):
    return measure_reserved_source_pairs(
        provider, store=store, state_path=state,
        prepared_root=prepared, audio_root=audio_root,
        output=prepared / "source_pairs", policy_rules=kwargs.pop("policy_rules", []),
        max_clips=2, probe=_probe_for(provider), measure=measured_fixture, **kwargs,
    )


def test_exact_bounded_section_and_paired_windows_resumable(tmp_path):
    origin, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(origin)
    first = _execute(provider, store, state, prepared, audio_root)
    assert first["counts"] == {"MEASURED_UNREVIEWED_SOURCE_PAIRS": 1}
    assert len(provider.range_calls) == 1
    _, start, end = provider.range_calls[0]
    assert start == 37 and end == 67
    report = json.loads((prepared / "source_pairs" / "holdout-source-pairs.json")
                        .read_text(encoding="utf-8"))
    assert report["benchmark_held_out"] is False
    assert report["event_split_verified"] is False
    assert report["frame_accurate_cuts_verified"] is False
    assert report["qualification_allowed"] is False
    assert report["reviewed_gold_available"] is False
    row = next(iter(report["records"].values()))
    assert len(row["pairs"]) == 1
    assert row["pairs"][0]["absolute_seek_verified"] is False
    assert Path(row["pairs"][0]["source_measurement"]).exists()
    assert Path(row["pairs"][0]["finished_measurement"]).exists()
    second = _execute(provider, store, state, prepared, audio_root)
    assert second["counts"] == {"MEASURED_UNREVIEWED_SOURCE_PAIRS": 1}
    assert len(provider.range_calls) == 1
    Path(row["pairs"][0]["source_video_path"]).write_bytes(b"changed")
    repaired = _execute(provider, store, state, prepared, audio_root)
    assert repaired["counts"] == {"MEASURED_UNREVIEWED_SOURCE_PAIRS": 1}
    assert len(provider.range_calls) == 2


def test_audio_anchor_contract_disallows_invented_or_weak_ranges():
    anchors = [{"clip_start": i, "clip_end": i + 6,
                "source_start": i + 10, "source_end": i + 16,
                "correlation": .95, "uniqueness_margin": .15}
               for i in (0, 9, 18)]
    row = {"status": "PARTIAL_AUDIO_MATCH", "fingerprint_version": "audio-band-anchor-v1",
           "anchors": anchors, "ranges": [{
               "clip_start": 0, "clip_end": 24, "source_start": 10, "source_end": 34,
               "anchors": 3, "min_correlation": .95,
           }]}
    assert len(_valid_ranges(row, 30, max_ranges=2)) == 1
    row["ranges"][0]["source_start"] = 99
    assert not _valid_ranges(row, 30, max_ranges=2)
    row["ranges"][0]["source_start"] = 10
    row["anchors"][0]["uniqueness_margin"] = .03
    assert not _valid_ranges(row, 30, max_ranges=2)


def test_new_training_overlap_prevents_any_metadata_probe(tmp_path):
    _, store, state, prepared, audio_root = _source_evidence(tmp_path)
    upsert_human_clip_examples(store, [trusted_example("new", stream_id="stream-evaluation")])

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("training conflict must block before probing")

    result = measure_reserved_source_pairs(
        NoNetwork(), store=store, state_path=state,
        prepared_root=prepared, audio_root=audio_root,
        output=prepared / "source_pairs", policy_rules=[],
        measure=measured_fixture,
    )
    assert result["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_changed_source_policy_quarantines_before_section_fetch(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(underlying)
    provider.policy_block = True
    assert _execute(provider, store, state, prepared, audio_root)["counts"] == {
        "QUARANTINED_POLICY": 1
    }
    assert not provider.range_calls


def test_range_restriction_blocks_before_section_fetch(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(underlying)
    source_policy = ClipPolicyRule(
        id="no-section", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=50, end=55,
        reason="restricted range",
    )
    result = _execute(
        provider, store, state, prepared, audio_root,
        policy_rules=[source_policy],
    )
    assert result["counts"] == {"QUARANTINED_RANGE_POLICY": 1}
    assert not provider.range_calls


def test_corrupt_or_unsupported_audio_prevents_video_download(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    audio_path = audio_root / "holdout-audio-alignment.json"
    payload = json.loads(audio_path.read_text(encoding="utf-8"))
    next(iter(payload["records"].values()))["ranges"][0]["anchors"] = 900
    audio_path.write_text(json.dumps(payload), encoding="utf-8")
    provider = SectionProvider(underlying)
    assert _execute(provider, store, state, prepared, audio_root)["counts"] == {
        "BLOCKED_UNVERIFIED_AUDIO_ANCHORS": 1
    }
    assert not provider.range_calls


def test_section_duration_mismatch_is_retryable_and_not_promoted(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(underlying)

    def bogus_probe(media):
        return {"streams": [{"codec_type": "video"}],
                "format": {"duration": "4000"}}

    result = measure_reserved_source_pairs(
        provider, store=store, state_path=state, prepared_root=prepared,
        audio_root=audio_root, output=prepared / "source_pairs", policy_rules=[],
        probe=bogus_probe, measure=measured_fixture,
    )
    assert result["counts"] == {"RETRYABLE_FAILURE": 1}
    assert result["qualification_allowed"] is False


def test_yt_dlp_range_command_and_source_namespace(tmp_path, monkeypatch):
    provider = YtDlpSourceProvider(manage_updates=False)
    metadata = SourceMetadata(
        provider="fake", url="https://youtu.be/original", external_id="original",
        title="stream", availability="public", live_status="was_live", duration=200,
    )
    monkeypatch.setattr(provider, "probe", lambda url: metadata)
    observed = []

    def execute(args, *, label, retry_with_fresh_runtime=True):
        observed.append(args)
        folder = Path(args[args.index("--paths") + 1])
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / "media.mkv"
        file.write_bytes(b"short requested section")
        return SimpleNamespace(stdout=str(file))

    monkeypatch.setattr(provider, "_run_yt_dlp", execute)
    item = provider.acquire_video_range(
        metadata.url, tmp_path / "ranges", start=80., end=94.
    )
    assert item.media_path.is_file()
    assert item.requested_start == 80
    assert item.requested_end == 94
    cmd = observed[0]
    assert cmd[cmd.index("--download-sections") + 1] == "*80.000-94.000"
    assert "--force-keyframes-at-cuts" in cmd
    assert "--no-playlist" in cmd
    assert provider.acquire_video_range(
        metadata.url, tmp_path / "ranges", start=80., end=94.
    ).reused is True
    assert len(observed) == 1
    item.media_path.write_bytes(b"damaged cached section")
    repaired = provider.acquire_video_range(
        metadata.url, tmp_path / "ranges", start=80., end=94.
    )
    assert repaired.reused is False
    assert len(observed) == 2
    assert item.media_path.read_bytes() == b"short requested section"
    item2 = provider.acquire_video_range(
        metadata.url, tmp_path / "ranges", start=100., end=112.
    )
    assert item2.media_path.parent != item.media_path.parent
    with pytest.raises(ValueError):
        provider.acquire_video_range(metadata.url, tmp_path / "ranges", start=80., end=240.)
