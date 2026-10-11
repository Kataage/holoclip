"""Cut-aware local audio mastering: bounded gain and no source mutation."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from holoclip import render_audio_finish as audio
from holoclip.domain import ClipTimeline, TimelineSegment


def _timeline():
    return ClipTimeline(
        id="offline-audio",
        segments=[
            TimelineSegment(
                id="a", source_asset_id="a", source_start=5,
                source_end=7, output_order=0,
            ),
            TimelineSegment(
                id="b", source_asset_id="b", source_start=19,
                source_end=21, output_order=1,
            ),
        ], estimated_duration=4,
    )


def _fixture(tmp_path):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"local trusted A/V placeholder")
    return video, hashlib.sha256(video.read_bytes()).hexdigest()


def test_ordered_output_ranges_and_numeric_gains():
    ranges = audio._ordered_cuts(_timeline())
    assert ranges == [(0, 2), (2, 4)]
    assert audio._gain(-32.0, -14.0) == 6.0
    assert audio._gain(-10.0, -1.0) == -6.0
    assert audio._gain(-70.0, -60.0) == 0.0
    assert audio._gain(-31.0, -3.0) == 1.5
    graph = audio._filter_graph(ranges, [3.0, -4.0])
    assert "asplit=2" in graph and "concat=n=2:v=0:a=1" in graph
    assert "alimiter=limit=0.84:attack=5:release=50:level=0" in graph
    assert "atrim=start=2.000000:end=4.000000" in graph
    assert "volume=+3.00dB" in graph and "volume=-4.00dB" in graph


@pytest.mark.parametrize("bad", [
    b"", b"mean_volume: -25.0 dB\n", b"max_volume: -7.0 dB\n",
    b"mean_volume: nan dB\nmax_volume: -10 dB\n",
    b"mean_volume: -19.0 dB\nmax_volume: -50.0 dB\n",
])
def test_missing_or_untrusted_meter_blocks(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(audio, "_safe_call", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr=bad,
    ))
    with pytest.raises(ValueError):
        audio._measure("ffmpeg", tmp_path / "original.mp4", 0.0, 1.0)


def test_measure_parses_stderr_but_does_not_expose_it(tmp_path, monkeypatch):
    commands = []
    def call(argv, *, timeout):
        commands.append(argv)
        return SimpleNamespace(
            returncode=0,
            stderr=b"token=SECRET\nmean_volume: -26.2 dB\nmax_volume: -6.0 dB\n",
        )
    monkeypatch.setattr(audio, "_safe_call", call)
    assert audio._measure("ffmpeg", tmp_path / "local.mp4", 2, 5) == (-26.2, -6.0)
    assert "file,pipe" in commands[0]
    assert "-ss" in commands[0] and "-t" in commands[0]


def test_no_write_if_hash_changes_or_output_exists(tmp_path, monkeypatch):
    video, sha = _fixture(tmp_path)
    monkeypatch.setattr(audio, "_probe", lambda *_: (4.0, True, True))
    monkeypatch.setattr(audio.shutil, "which", lambda x: pytest.fail("no FFmpeg"))
    bad = audio.level_preview_audio(
        mp4_output=video, video_sha256="b" * 64, timeline=_timeline(),
    )
    assert bad["status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"
    output = tmp_path / "preview.leveled.mp4"
    output.write_bytes(b"operator material must stay")
    bad = audio.level_preview_audio(
        mp4_output=video, video_sha256=sha, timeline=_timeline(),
    )
    assert bad["status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"
    assert output.read_bytes() == b"operator material must stay"


def test_preview_is_readonly_on_failure_and_valid_success_with_synthetic_media(
    tmp_path, monkeypatch,
):
    video, sha = _fixture(tmp_path)
    monkeypatch.setattr(audio, "_probe", lambda *_: (4.0, True, True))
    monkeypatch.setattr(audio.shutil, "which", lambda x: x)
    before = iter([(-32.0, -12.0), (-15.0, -2.0),
                   (-26.0, -7.0), (-21.0, -5.0)])
    monkeypatch.setattr(audio, "_measure", lambda *args: next(before))
    def fake_encoder(argv, *, timeout):
        if "-filter_complex" not in argv:
            pytest.fail("not the expected offline render")
        Path(argv[-1]).write_bytes(b"processed with video stream copied")
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(audio, "_safe_call", fake_encoder)
    result = audio.level_preview_audio(
        mp4_output=video, video_sha256=sha, timeline=_timeline(),
    )
    assert result["status"] == "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL"
    assert result["cut_count"] == 2 and result["max_abs_gain_db"] == 6.0
    assert result["before_mean_spread_db"] == 17.0
    assert result["after_mean_spread_db"] == 5.0
    assert result["ready"] is False
    target = tmp_path / "preview.leveled.mp4"
    assert result["output_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert video.read_bytes() == b"local trusted A/V placeholder"
    again = audio.level_preview_audio(
        mp4_output=video, video_sha256=sha, timeline=_timeline(),
    )
    assert again["status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"
    assert target.read_bytes() == b"processed with video stream copied"


def test_audio_quality_gate_rejects_unexplained_gain_or_clipping(tmp_path, monkeypatch):
    video, sha = _fixture(tmp_path)
    monkeypatch.setattr(audio, "_probe", lambda *_: (4.0, True, True))
    monkeypatch.setattr(audio.shutil, "which", lambda x: x)
    monkeypatch.setattr(audio, "_measure", lambda *args: (-15.0, -2.0))
    def encoder(argv, *, timeout):
        Path(argv[-1]).write_bytes(b"bad audio evidence")
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(audio, "_safe_call", encoder)
    result = audio.level_preview_audio(
        mp4_output=video, video_sha256=sha, timeline=_timeline(),
    )
    assert result["status"] == "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"
    assert not (tmp_path / "preview.leveled.mp4").exists()


def test_real_ffmpeg_cut_leveling_on_synthetic_audio(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("local FFmpeg required for real audio QA")
    video = tmp_path / "preview.mp4"
    proc = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=30:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-filter_complex",
        "[1:a]volume='if(lt(t,2),0.25,1.0)':eval=frame[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(video),
    ], check=False, capture_output=True, timeout=60)
    if proc.returncode != 0:
        pytest.skip("synthetic video encoder unavailable")
    original_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    result = audio.level_preview_audio(
        mp4_output=video, video_sha256=original_sha, timeline=_timeline(),
    )
    assert result["status"] == "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL"
    assert result["cut_count"] == 2
    assert result["after_mean_spread_db"] < result["before_mean_spread_db"]
    assert hashlib.sha256(video.read_bytes()).hexdigest() == original_sha
    assert audio._probe(video.with_name("preview.leveled.mp4"))[1:] == (True, True)
