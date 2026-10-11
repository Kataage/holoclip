"""Measured read-only video/audio/subtitle diagnostics never equal editorial QA."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from holoclip import render_quality_diagnostics as diag
from holoclip.domain import ClipTimeline, TimelineSegment


def _timeline():
    return ClipTimeline(id="read-only-inspection", segments=[
        TimelineSegment(
            id="a", source_asset_id="asset", source_start=1,
            source_end=3, output_order=0,
        ),
        TimelineSegment(
            id="b", source_asset_id="asset", source_start=5,
            source_end=7, output_order=1,
        ),
    ], estimated_duration=4)


def _case(tmp_path, *, transcript=True):
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"mock local MP4 bytes")
    final = tmp_path / "preview.leveled.mp4"
    final.write_bytes(b"mock trusted encoded local MP4")
    srt_sha = None
    if transcript:
        srt = source.with_suffix(".srt")
        srt.write_bytes(
            "1\n00:00:00,200 --> 00:00:01,100\nこんにちは\n\n"
            "2\n00:00:02,100 --> 00:00:03,100\nすごいですね\n".encode("utf-8")
        )
        srt_sha = hashlib.sha256(srt.read_bytes()).hexdigest()
    return source, final, srt_sha


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _invoke(source, final, srt_sha):
    return diag.evaluate_local_edit_quality(
        mp4_output=source, video_sha256=_hash(source),
        final_output=final, final_sha256=_hash(final),
        timeline=_timeline(), caption_sha256=srt_sha,
    )


def test_video_log_parser_counts_qualified_black_and_freeze(tmp_path, monkeypatch):
    raw = (b"[blackdetect @ ff] black_start:0 black_end:1.5 black_duration:1.5\n"
           b"[freezedetect @ ff] lavfi.freezedetect.freeze_start:2.4\n")
    argv_seen = []
    def run(argv, **kwargs):
        argv_seen.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] <= 180
        return SimpleNamespace(returncode=0, stderr=raw)
    monkeypatch.setattr(diag.subprocess, "run", run)
    assert diag._video_events("ffmpeg", tmp_path / "base.mp4", 4.0) == (1, 1)
    command = argv_seen[0]
    assert "blackdetect=d=1.0" in command[command.index("-vf") + 1]
    assert "freezedetect=n=-60dB:d=2.0" in command[command.index("-vf") + 1]
    assert "-protocol_whitelist" in command and "file,pipe" in command
    assert "-nostdin" in command and "-an" in command


@pytest.mark.parametrize("stderr", [
    b"black_start:0 black_end:30 black_duration:30",
    b"black_start:3 black_end:2 black_duration:1",
    b"black_start:0 black_end:1 black_duration:8",
    b"lavfi.freezedetect.freeze_start:99",
])
def test_invalid_video_detector_events_fail_closed(tmp_path, monkeypatch, stderr):
    monkeypatch.setattr(diag.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stderr=stderr))
    with pytest.raises(ValueError, match="LOCAL_EDIT_DIAGNOSTIC_EVENT_INVALID"):
        diag._video_events("ffmpeg", tmp_path / "vid.mp4", 4.0)


def test_subtitle_speed_and_line_width_are_review_hints(tmp_path):
    source, final, original_sha = _case(tmp_path)
    subtitle = source.with_suffix(".srt")
    subtitle.write_bytes(
        ("1\n00:00:00,000 --> 00:00:00,500\n"
         + "ありがとうございます" * 11 + "\n").encode("utf-8")
    )
    checked, fast, overflow, speed = diag._subtitle_flags(
        base=source, caption_sha256=_hash(subtitle), duration=4.0,
    )
    assert checked and fast == 1 and overflow == 1
    assert speed > 20
    assert original_sha != _hash(subtitle)


def test_missing_subtitle_does_not_claim_verified_readability(tmp_path, monkeypatch):
    source, final, _ = _case(tmp_path, transcript=False)
    monkeypatch.setattr(diag, "_probe", lambda p: (4.0, True, True))
    monkeypatch.setattr(diag.shutil, "which", lambda p: p)
    monkeypatch.setattr(diag, "_video_events", lambda *args, **kwargs: (0, 0, []))
    monkeypatch.setattr(diag, "_measure", lambda *args: (-23.0, -3.0))
    report = _invoke(source, final, None)
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_PARTIAL"
    assert report["subtitle_checked"] is False
    assert report["ready"] is False and report["production_ready"] is False
    assert report["human_editorial_qa_completed"] is False


def test_measured_flags_not_automatically_fixed(tmp_path, monkeypatch):
    source, final, srt_sha = _case(tmp_path)
    monkeypatch.setattr(diag, "_probe", lambda p: (4.0, True, True))
    monkeypatch.setattr(diag.shutil, "which", lambda p: p)
    monkeypatch.setattr(diag, "_video_events", lambda *args, **kwargs: (2, 1, []))
    monkeypatch.setattr(diag, "_measure", lambda *args: (-42.0, -0.2))
    before = (source.read_bytes(), final.read_bytes())
    report = _invoke(source, final, srt_sha)
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS"
    assert report["black_interval_count"] == 2
    assert report["freeze_interval_count"] == 1
    assert report["flag_count"] >= 4
    assert report["audio_sample_peak_dbfs"] == -0.2
    assert report["subtitle_checked"] is True
    assert report["publication_performed"] is False
    assert (source.read_bytes(), final.read_bytes()) == before
    assert not list(tmp_path.glob("*.repaired.mp4"))


def test_no_flags_still_unqualified_editorially(tmp_path, monkeypatch):
    source, final, srt_sha = _case(tmp_path)
    monkeypatch.setattr(diag, "_probe", lambda p: (4.0, True, True))
    monkeypatch.setattr(diag.shutil, "which", lambda p: p)
    monkeypatch.setattr(diag, "_video_events", lambda *args, **kwargs: (0, 0, []))
    monkeypatch.setattr(diag, "_measure", lambda *args: (-22.0, -2.5))
    report = _invoke(source, final, srt_sha)
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS"
    assert report["flag_count"] == 0
    assert report["subtitle_checked"] is True
    assert report["ready"] is False


def test_untrusted_source_sha_blocks_ffmpeg_and_leaves_media(tmp_path, monkeypatch):
    source, final, srt_sha = _case(tmp_path)
    monkeypatch.setattr(diag, "_probe", lambda p: (4.0, True, True))
    monkeypatch.setattr(diag.shutil, "which", lambda p:
                        pytest.fail("Changed source must not be analyzed"))
    report = diag.evaluate_local_edit_quality(
        mp4_output=source, video_sha256="a" * 64,
        final_output=final, final_sha256=_hash(final),
        timeline=_timeline(), caption_sha256=srt_sha,
    )
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"
    assert source.read_bytes() == b"mock local MP4 bytes"


def test_unrelated_final_mp4_is_rejected(tmp_path, monkeypatch):
    source, final, srt_sha = _case(tmp_path)
    foreign = tmp_path / "unrelated.mp4"
    foreign.write_bytes(b"unknown path")
    monkeypatch.setattr(diag, "_probe", lambda p:
                        pytest.fail("Do not probe unrecognized final media"))
    report = diag.evaluate_local_edit_quality(
        mp4_output=source, video_sha256=_hash(source),
        final_output=foreign, final_sha256=_hash(foreign),
        timeline=_timeline(), caption_sha256=srt_sha,
    )
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"


def test_subtitle_change_after_receipt_never_treated_as_valid(tmp_path, monkeypatch):
    source, final, srt_sha = _case(tmp_path)
    source.with_suffix(".srt").write_text("altered operator text", encoding="utf-8")
    monkeypatch.setattr(diag, "_probe", lambda p: (4.0, True, True))
    monkeypatch.setattr(diag.shutil, "which", lambda p: p)
    monkeypatch.setattr(diag, "_video_events", lambda *args, **kwargs: (0, 0, []))
    monkeypatch.setattr(diag, "_measure", lambda *args: (-22.0, -2.0))
    assert _invoke(source, final, srt_sha)["status"] == "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg and ffprobe required")
def test_real_synthetic_static_black_video_and_readable_srt(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    source = tmp_path / "preview.mp4"
    result = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "mpeg4", "-q:v", "6", "-c:a", "aac",
        "-shortest", str(source),
    ], check=False, capture_output=True, timeout=60)
    assert result.returncode == 0
    srt = source.with_suffix(".srt")
    srt.write_bytes(
        "1\n00:00:00,500 --> 00:00:01,500\n見てください\n".encode("utf-8")
    )
    report = diag.evaluate_local_edit_quality(
        mp4_output=source, video_sha256=_hash(source),
        final_output=source, final_sha256=_hash(source),
        timeline=_timeline(), caption_sha256=_hash(srt),
    )
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS"
    assert report["black_interval_count"] >= 1
    assert report["subtitle_checked"] is True
    assert report["audio_sample_peak_dbfs"] is not None
    assert report["ready"] is False
    assert not list(tmp_path.glob("*.repaired.mp4"))


def test_scheduler_deadline_skips_diagnostics_without_touching_media(
    tmp_path, monkeypatch,
):
    source, final, srt_sha = _case(tmp_path)
    monkeypatch.setattr(
        diag, "_validate_media",
        lambda **kw: pytest.fail("Skipped diagnostics must not probe inputs"),
    )
    report = diag.evaluate_local_edit_quality(
        mp4_output=source, video_sha256=_hash(source),
        final_output=final, final_sha256=_hash(final),
        timeline=_timeline(), caption_sha256=srt_sha,
        scan_allowed=False,
    )
    assert report["status"] == "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE"
    assert report["ready"] is False and report["publication_performed"] is False
    assert source.read_bytes() == b"mock local MP4 bytes"


def test_video_detail_retains_positions_without_media_text(tmp_path, monkeypatch):
    stderr = (
        b"[blackdetect] black_start:0.5 black_end:1.7 black_duration:1.2\n"
        b"[freezedetect] lavfi.freezedetect.freeze_start:2.5\n"
    )
    monkeypatch.setattr(diag.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stderr=stderr))
    black, freeze, issues = diag._video_events(
        "ffmpeg", tmp_path / "base.mp4", 4.0, detailed=True,
    )
    assert black == freeze == 1
    assert issues == [
        {"kind": "blackout", "start_seconds": 0.5, "end_seconds": 1.7},
        {"kind": "freeze", "start_seconds": 2.5, "end_seconds": None},
    ]


def test_caption_details_have_timestamps_and_numbers_without_transcript(tmp_path):
    source, final, sha = _case(tmp_path)
    subtitle = source.with_suffix(".srt")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,500\n" + "ありがとう" * 20 + "\n",
        encoding="utf-8",
    )
    checked, fast, overflow, speed, issues = diag._subtitle_flags(
        base=source, caption_sha256=_hash(subtitle),
        duration=4.0, detailed=True,
    )
    assert checked and fast == 1 and overflow == 1 and speed > 20
    assert issues == [
        {"kind": "fast_subtitle", "start_seconds": 0.0,
         "end_seconds": 0.5, "cue_number": 1},
        {"kind": "wide_subtitle", "start_seconds": 0.0,
         "end_seconds": 0.5, "cue_number": 1},
    ]
    assert "ありがとう" not in str(issues)
