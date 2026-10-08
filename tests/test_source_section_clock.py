"""Bounded source clock correspondence, PTS and no-promotion safeguards."""
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_source_clock_pipeline import calibrate_reserved_section_clocks
from holoclip.source_section_clock import calibrate_section_clock, locate_section_audio_origin
from test_bounded_source_evidence import SectionProvider, _execute, _source_evidence
from test_editorial_profile import trusted_example


def features(n, seed=29):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, 12))
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype("float32")


def test_unique_segment_matches_full_source_audio_without_source_hint():
    source = features(2000)
    result = locate_section_audio_origin(source[300:460].copy(), source)
    assert result["status"] == "PARTIAL_SECTION_AUDIO_CLOCK"
    assert result["origin_seconds"] == 75.
    assert len(result["anchors"]) >= 3
    assert result["resolution_seconds"] == .25
    assert result["unknowns"]


def test_duplicate_reordered_or_missing_audio_fails_closed():
    source = features(1800)
    assert locate_section_audio_origin(
        source[400:560], np.concatenate((source, source)),
    )["status"] == "UNKNOWN_NONUNIQUE_SOURCE_AUDIO"
    disorder = np.concatenate((source[200:300], source[900:1000]))
    result = locate_section_audio_origin(disorder, source)
    assert result["origin_seconds"] is None
    assert result["status"] in {
        "UNKNOWN_NONUNIQUE_SOURCE_AUDIO", "UNKNOWN_INCONSISTENT_SOURCE_CLOCK",
    }
    assert locate_section_audio_origin(source[20:50], source)[
        "status"
    ] == "UNKNOWN_SHORT_OR_MISSING_AUDIO"
    with pytest.raises(ValueError):
        locate_section_audio_origin(source[:100, :9], source)


def test_native_section_pts_are_not_full_original_frame_pts(tmp_path):
    path = tmp_path / "section.mkv"
    path.write_bytes(b"test")
    result = calibrate_section_clock(
        path, path, tmp_path / "cache",
        probe=lambda path: {"format": {"duration": 30},
                            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
        pts=lambda path: [3, 3.04, 3.08, 32.9],
        audio_pts=lambda path: 3.,
        fingerprint=lambda path, cache: np.zeros((120, 12), dtype=np.float32),
        locate=lambda section, original: {
            "version": "bounded-source-audio-clock-v1",
            "status": "PARTIAL_SECTION_AUDIO_CLOCK", "origin_seconds": 100.,
        },
    )
    assert result["frame_pts_verified"] is True
    assert result["original_video_absolute_pts_verified"] is False
    assert result["source_native_video_pts_range"] == [3, 32.9]
    assert result["approx_original_video_range"] == [100., 129.9]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_real_ffprobe_native_section_video_and_audio_pts(tmp_path):
    from holoclip.source_section_clock import _first_audio_pts
    from holoclip.video_measurement import frame_pts

    video = tmp_path / "av.mkv"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=192x108:rate=8:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=540:sample_rate=8000:duration=4",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video),
    ], check=True, timeout=90)
    video_pts = frame_pts(video)
    audio_pts = _first_audio_pts(video)
    assert len(video_pts) >= 16
    assert np.isfinite(audio_pts)
    assert abs(audio_pts - video_pts[0]) < .5


def setup(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(underlying)
    assert _execute(provider, store, state, prepared, audio_root)["counts"] == {
        "MEASURED_UNREVIEWED_SOURCE_PAIRS": 1
    }
    return provider, store, state, prepared, audio_root


def run(provider, store, state, prepared, audio_root, **kwargs):
    return calibrate_reserved_section_clocks(
        provider, store=store, state_path=state, prepared_root=prepared,
        audio_root=audio_root, pairs_root=prepared / "source_pairs",
        output=prepared / "source_clocks", policy_rules=kwargs.pop("policy_rules", []),
        max_clips=2, **kwargs,
    )


def calibrated(*args):
    return {
        "version": "bounded-source-audio-clock-v1",
        "status": "PARTIAL_SECTION_AUDIO_CLOCK",
        "origin_seconds": 40.,
        "approx_original_video_range": [40., 68.],
        "frame_pts_verified": True, "original_video_absolute_pts_verified": False,
    }


def test_clock_pipeline_hash_bound_resume_and_zero_gold(tmp_path):
    provider, store, state, prepared, audio_root = setup(tmp_path)
    called = []
    def run_once(*args):
        called.append(1)
        return calibrated(*args)

    first = run(provider, store, state, prepared, audio_root, calibrate=run_once)
    assert first["counts"] == {"MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS": 1}
    report = json.loads(
        (prepared / "source_clocks" / "holdout-source-clock.json").read_text(encoding="utf-8")
    )
    for name in (
        "qualification_allowed", "benchmark_held_out",
        "reviewed_gold_available", "event_split_verified",
        "frame_accurate_cuts_verified", "original_video_absolute_pts_verified",
    ):
        assert report[name] is False
    run(provider, store, state, prepared, audio_root, calibrate=run_once)
    assert len(called) == 1


def test_corrupted_source_section_and_split_conflict_prevent_metadata_probe(tmp_path):
    provider, store, state, prepared, audio_root = setup(tmp_path)
    pair = next(iter(json.loads(
        (prepared / "source_pairs" / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )["records"].values()))["pairs"][0]
    Path(pair["source_video_path"]).write_bytes(b"corrupt")
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Must block without network")
    assert run(NoNetwork(), store, state, prepared, audio_root)["counts"] == {
        "BLOCKED_MEASUREMENT_INTEGRITY": 1
    }
    upsert_human_clip_examples(
        store, [trusted_example("leaked", stream_id="stream-evaluation")],
    )
    assert run(NoNetwork(), store, state, prepared, audio_root)["counts"] == {
        "BLOCKED_SPLIT_CONFLICT": 1
    }


def test_changed_policy_and_observed_scope_fails_closed(tmp_path):
    provider, store, state, prepared, audio_root = setup(tmp_path)
    provider.policy_block = True
    assert run(provider, store, state, prepared, audio_root, calibrate=calibrated)[
        "counts"
    ] == {"QUARANTINED_POLICY": 1}
    provider.policy_block = False
    scoped = ClipPolicyRule(
        id="restricted-late-range", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=69, end=71,
        reason="restricted source interval",
    )
    result = run(
        provider, store, state, prepared, audio_root,
        calibrate=calibrated, policy_rules=[scoped],
    )
    assert result["counts"] == {"QUARANTINED_OBSERVED_RANGE_POLICY": 1}


def test_unexpected_download_seek_stays_unknown(tmp_path):
    provider, store, state, prepared, audio_root = setup(tmp_path)
    result = run(
        provider, store, state, prepared, audio_root,
        calibrate=lambda *args: {
            **calibrated(*args), "origin_seconds": 85.,
            "approx_original_video_range": [85., 113.],
        },
    )
    assert result["counts"] == {"MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS": 1}
    report = json.loads(
        (prepared / "source_clocks" / "holdout-source-clock.json").read_text(encoding="utf-8")
    )
    section = next(iter(report["records"].values()))["section_clocks"][0]
    assert section["status"] == "UNKNOWN_REQUESTED_SEEK_DISAGREEMENT"
    assert section["origin_seconds"] is None
    assert section["approx_original_video_range"] is None
