"""Japanese subtitle burn-in is optional, offline and create-only."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from holoclip import render_subtitle_burnin as burn


SRT = (
    "1\n00:00:00,200 --> 00:00:00,800\nこんにちは！\n\n"
    "2\n00:00:01,000 --> 00:00:01,500\nありがとう\n"
)


def _files(tmp_path, *, srt=SRT):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"QA-checked synthetic mp4 bytes")
    sidecar = video.with_suffix(".srt")
    sidecar.write_text(srt, encoding="utf-8")
    return video, sidecar, hashlib.sha256(sidecar.read_bytes()).hexdigest()


def test_windows_crlf_srt_remains_valid_and_lone_cr_is_rejected():
    cues = burn._read_cues(SRT.replace("\n", "\r\n").encode("utf-8"))
    assert len(cues) == 2 and cues[0].text == "こんにちは！"
    with pytest.raises(ValueError, match="SRT_UNTRUSTED"):
        burn._read_cues(SRT.replace("\n", "\r", 1).encode("utf-8"))


def test_deterministic_ass_japanese_style_and_two_line_wrap():
    cues = burn._read_cues(SRT.encode("utf-8"))
    ass = burn._ass(cues, font_name="Meiryo")
    assert "PlayResX: 1280" in ass and "PlayResY: 720" in ass
    assert "Alignment, MarginL, MarginR, MarginV" in ass
    assert "Style: JP,Meiryo,43" in ass
    assert "0:00:00.20,0:00:00.80" in ass
    assert "こんにちは！" in ass
    assert r"\N" in burn._wrap("ありがとうございます" * 3)
    with pytest.raises(ValueError, match="TEXT_TOO_LONG"):
        burn._wrap("長い" * 60)


@pytest.mark.parametrize("srt", [
    "1\n00:00:00,100 --> 00:00:00,200\n短い\n",
    "1\n00:00:00,100 --> 00:00:01,100\n{\\move(1,2)}不正\n",
    "1\n00:00:00,100 --> 00:00:01,100\n一つ\n\n"
    "2\n00:00:00,500 --> 00:00:01,500\n重複\n",
    "1\n00:00:00,100 --> 00:00:01,100\n長い\n"
    "不正行\n",
    "2\n00:00:00,100 --> 00:00:01,100\n番号不正\n",
])
def test_bad_srt_rejected_before_ffmpeg(tmp_path, monkeypatch, srt):
    video, _, sha = _files(tmp_path, srt=srt)
    monkeypatch.setattr(
        burn, "_probe",
        lambda path: pytest.fail("invalid cues must not reach ffprobe"),
    )
    outcome = burn.burn_captioned_preview(
        mp4_output=video, video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
        subtitle_sha256=sha,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert not video.with_name("preview.captioned.mp4").exists()


def test_no_srt_never_invokes_encoder(tmp_path, monkeypatch):
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"original")
    monkeypatch.setattr(
        burn, "_burn",
        lambda *args, **kw: pytest.fail("no burn without verified SRT"),
    )
    outcome = burn.burn_captioned_preview(
        mp4_output=video, video_sha256="a" * 64, subtitle_sha256=None,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE"


def test_sha_mismatch_blocks_burn_before_ffmpeg(tmp_path, monkeypatch):
    video, subtitle, sha = _files(tmp_path)
    subtitle.write_text("modified", encoding="utf-8")
    monkeypatch.setattr(
        burn, "_probe",
        lambda path: pytest.fail("changed caption must not reach FFmpeg"),
    )
    outcome = burn.burn_captioned_preview(
        mp4_output=video,
        video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
        subtitle_sha256=sha,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert video.read_bytes() == b"QA-checked synthetic mp4 bytes"


def test_synthetic_encode_no_clobber_and_independent_video(tmp_path, monkeypatch):
    video, subtitle, sha = _files(tmp_path)
    original = video.read_bytes()
    font = tmp_path / "jp.ttf"
    font.write_bytes(b"mock Japanese font")
    monkeypatch.setattr(burn, "_japanese_font", lambda: (font, "Meiryo"))
    monkeypatch.setattr(burn.shutil, "which", lambda value: value)
    monkeypatch.setattr(burn, "_probe", lambda path: (2.0, True, True))
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        assert kwargs["cwd"].is_dir()
        assert (kwargs["cwd"] / "captions.ass").is_file()
        assert (kwargs["cwd"] / "fonts" / "caption.ttf").is_file()
        assert "-vf" in args and "ass=filename=captions.ass:fontsdir=fonts" in args
        assert "-map" in args and "-nostdin" in args and "-n" in args
        assert "http" not in " ".join(args)
        Path(args[-1]).write_bytes(b"new captioned A/V")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(burn.subprocess, "run", fake_run)
    kwargs = {
        "mp4_output": video,
        "video_sha256": hashlib.sha256(original).hexdigest(),
        "subtitle_sha256": sha,
    }
    result = burn.burn_captioned_preview(**kwargs)
    target = video.with_name("preview.captioned.mp4")
    if result["status"] != "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL":
        # Surface the exact failing precondition on Windows rather than
        # masking it behind the optional production error-code contract.
        burn._burn(video, subtitle, target,
                   video_sha=kwargs["video_sha256"], srt_sha=kwargs["subtitle_sha256"])
    assert result["status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL"
    assert result["output_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["ready"] is False and result["production_ready"] is False
    assert video.read_bytes() == original and subtitle.read_text(encoding="utf-8") == SRT
    assert len(seen) == 1
    repeat = burn.burn_captioned_preview(**kwargs)
    assert repeat["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert target.read_bytes() == b"new captioned A/V" and len(seen) == 1


def test_font_unavailable_does_not_write_or_change_source(tmp_path, monkeypatch):
    video, _, sha = _files(tmp_path)
    monkeypatch.setattr(burn, "_probe", lambda path: (2.0, True, True))
    monkeypatch.setattr(burn, "_japanese_font", lambda: None)
    outcome = burn.burn_captioned_preview(
        mp4_output=video,
        video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
        subtitle_sha256=sha,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert not list(tmp_path.glob("*.captioned.mp4"))
    assert not list(tmp_path.glob(".holoclip-burnin-*"))


def test_existing_caption_target_cannot_be_overwritten(tmp_path, monkeypatch):
    video, _, sha = _files(tmp_path)
    target = video.with_name("preview.captioned.mp4")
    target.write_bytes(b"operator media")
    monkeypatch.setattr(
        burn, "_probe", lambda path: pytest.fail("must not encode over existing output"),
    )
    outcome = burn.burn_captioned_preview(
        mp4_output=video, video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
        subtitle_sha256=sha,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    assert target.read_bytes() == b"operator media"


def test_real_offline_ffmpeg_japanese_caption_with_audio(tmp_path):
    """Linux CI installs libass+Noto; Windows may lack command-line FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe") or burn._japanese_font() is None:
        pytest.skip("FFmpeg/ffprobe/Japanese font unavailable on this runner")
    video = tmp_path / "preview.mp4"
    proc = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=30:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(video),
    ], capture_output=True, timeout=60, check=False)
    if proc.returncode != 0:
        pytest.skip("synthetic A/V encoding unavailable")
    subtitle = video.with_suffix(".srt")
    subtitle.write_text(SRT, encoding="utf-8")
    sha = hashlib.sha256(subtitle.read_bytes()).hexdigest()
    original_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    result = burn.burn_captioned_preview(
        mp4_output=video, video_sha256=original_sha, subtitle_sha256=sha,
    )
    assert result["status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL"
    assert burn._probe(video.with_name("preview.captioned.mp4"))[1:] == (True, True)
    assert hashlib.sha256(video.read_bytes()).hexdigest() == original_sha


def test_leveled_mp4_burns_original_sidecar_with_stable_caption_target(
    tmp_path, monkeypatch,
):
    original = tmp_path / "preview.mp4"
    original.write_bytes(b"original")
    srt = tmp_path / "preview.srt"
    srt.write_text(SRT, encoding="utf-8")
    leveled = tmp_path / "preview.leveled.mp4"
    leveled.write_bytes(b"verified audio")
    output = tmp_path / "preview.captioned.mp4"
    called = []

    def encode(video, subtitle, target, *, video_sha, srt_sha):
        called.append((video, subtitle, target))
        assert video == leveled and subtitle == srt and target == output
        assert video_sha == hashlib.sha256(leveled.read_bytes()).hexdigest()
        target.write_bytes(b"synthetic captioned output")
        return hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size

    monkeypatch.setattr(burn, "_burn", encode)
    status = burn.burn_captioned_preview(
        mp4_output=leveled,
        video_sha256=hashlib.sha256(leveled.read_bytes()).hexdigest(),
        subtitle_sha256=hashlib.sha256(srt.read_bytes()).hexdigest(),
    )
    assert status["status"] == "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL"
    assert called == [(leveled, srt, output)]
    assert original.read_bytes() == b"original"


def test_real_burn_guard_rejects_unrelated_subtitle_path(tmp_path):
    video = tmp_path / "preview.leveled.mp4"
    video.write_bytes(b"input")
    srt = tmp_path / "unrelated.srt"
    srt.write_text(SRT, encoding="utf-8")
    with pytest.raises(ValueError, match="INPUT_UNTRUSTED"):
        burn._burn(
            video, srt, tmp_path / "unrelated.captioned.mp4",
            video_sha=hashlib.sha256(video.read_bytes()).hexdigest(),
            srt_sha=hashlib.sha256(srt.read_bytes()).hexdigest(),
        )
