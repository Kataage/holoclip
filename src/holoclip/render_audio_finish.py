"""Optional offline cut-aware audio leveling for previously QA-passed local previews.

Per-cut measured dBFS guards limit corrections and cap estimated peaks.
Original A/V, SRT and original technical QA remain immutable. This is a
conservative audio *baseline*, not perceptual LUFS or voice/music separation.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from holoclip.domain import ClipTimeline
from holoclip.render_plan import _hash, _no_links, _probe

_MEAN = re.compile(rb"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_PEAK = re.compile(rb"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TARGET_MEAN_DB = -22.0
_MIN_GAIN_DB = -6.0
_MAX_GAIN_DB = 6.0
_PEAK_CEILING_DB = -1.5
_MAX_SECONDS = 180.0
_MAX_CUTS = 32


def _safe_call(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_AUDIO_MEDIA_PROCESS_UNAVAILABLE") from exc


def _ordered_cuts(timeline: ClipTimeline) -> list[tuple[float, float]]:
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    cuts = sorted(timeline.segments, key=lambda s: s.output_order)
    if not 1 <= len(cuts) <= _MAX_CUTS:
        raise ValueError("LOCAL_AUDIO_CUT_COUNT_UNTRUSTED")
    ranges = []
    offset = 0.0
    for cut in cuts:
        duration = cut.source_end - cut.source_start
        if not math.isfinite(duration) or duration < 0.25:
            raise ValueError("LOCAL_AUDIO_CUT_DURATION_UNTRUSTED")
        ranges.append((offset, offset + duration))
        offset += duration
    if not 0 < offset <= _MAX_SECONDS or abs(offset - timeline.estimated_duration) > 0.05:
        raise ValueError("LOCAL_AUDIO_TIMELINE_UNTRUSTED")
    return ranges


def _measure(
    ffmpeg: str, video: Path, start: float, end: float,
) -> tuple[float, float]:
    # Only numeric local seek/range parameters, never strings from an LLM.
    result = _safe_call([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-nostdin",
        "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
        "-protocol_whitelist", "file,pipe", "-i", str(video),
        "-map", "0:a:0", "-vn", "-af", "volumedetect", "-f", "null", "-",
    ], timeout=min(60.0, max(15.0, (end - start) * 3.0)))
    if result.returncode != 0 or len(result.stderr) > 32768:
        raise ValueError("LOCAL_AUDIO_MEASUREMENT_UNAVAILABLE")
    mean_matches = _MEAN.findall(result.stderr)
    peak_matches = _PEAK.findall(result.stderr)
    if len(mean_matches) != 1 or len(peak_matches) != 1:
        raise ValueError("LOCAL_AUDIO_MEASUREMENT_UNTRUSTED")
    mean, peak = float(mean_matches[0]), float(peak_matches[0])
    if not all(math.isfinite(x) and -100.0 <= x <= 6.0 for x in (mean, peak)):
        raise ValueError("LOCAL_AUDIO_MEASUREMENT_UNTRUSTED")
    if peak < mean - 0.5:
        raise ValueError("LOCAL_AUDIO_MEASUREMENT_UNTRUSTED")
    return mean, peak


def _gain(mean: float, peak: float) -> float:
    # Never amplify near-silence: aggressive auto-gain turns background noise
    # into speech-like loudness, especially in stream transition gaps.
    if peak <= -48.0 or mean <= -50.0:
        return 0.0
    requested = max(_MIN_GAIN_DB, min(_MAX_GAIN_DB, _TARGET_MEAN_DB - mean))
    return round(min(requested, _PEAK_CEILING_DB - peak), 2)


def _filter_graph(ranges: list[tuple[float, float]], gains: list[float]) -> str:
    if len(ranges) != len(gains) or not ranges:
        raise ValueError("LOCAL_AUDIO_FILTER_INVALID")
    streams = "".join(f"[x{i}]" for i in range(len(ranges)))
    parts = [f"[0:a:0]asplit={len(ranges)}{streams}"]
    for i, ((start, end), gain) in enumerate(zip(ranges, gains)):
        if not all(math.isfinite(v) for v in (start, end, gain)):
            raise ValueError("LOCAL_AUDIO_FILTER_INVALID")
        parts.append(
            f"[x{i}]atrim=start={start:.6f}:end={end:.6f},"
            f"asetpts=PTS-STARTPTS,volume={gain:+.2f}dB,"
            f"aresample=48000,aformat=sample_fmts=fltp:"
            f"channel_layouts=stereo[a{i}]"
        )
    inputs = "".join(f"[a{i}]" for i in range(len(ranges)))
    # limiter has no automatic gain-up; cap final sample peak after concat.
    parts.append(
        f"{inputs}concat=n={len(ranges)}:v=0:a=1,"
        "alimiter=limit=0.84:attack=5:release=50:level=0[outa]"
    )
    return ";".join(parts)


def _finish(
    *, video: Path, video_sha256: str, timeline: ClipTimeline,
) -> tuple[str, list[dict[str, float]]]:
    if not _SHA.fullmatch(video_sha256):
        raise ValueError("LOCAL_AUDIO_VIDEO_HASH_UNTRUSTED")
    original = _no_links(video)
    target = _no_links(original.with_name(f"{original.stem}.leveled.mp4"))
    if (original.suffix.lower() != ".mp4" or not original.is_file()
            or target.exists() or not original.parent.is_dir()):
        raise ValueError("LOCAL_AUDIO_INPUT_UNTRUSTED")
    ranges = _ordered_cuts(timeline)
    duration, has_video, has_audio = _probe(original)
    if not has_video or not has_audio or abs(duration - ranges[-1][1]) > 0.5:
        raise ValueError("LOCAL_AUDIO_AV_UNVERIFIED")
    if _hash(original)[0] != video_sha256:
        raise ValueError("LOCAL_AUDIO_SOURCE_CHANGED")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_AUDIO_FFMPEG_UNAVAILABLE")
    before = [_measure(ffmpeg, original, *cut) for cut in ranges]
    gains = [_gain(mean, peak) for mean, peak in before]
    with tempfile.TemporaryDirectory(prefix=".holoclip-audio-", dir=original.parent) as work:
        staged = Path(work) / "leveled.mp4"
        argv = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-n", "-protocol_whitelist", "file,pipe", "-i", str(original),
            "-filter_complex", _filter_graph(ranges, gains),
            "-map", "0:v:0", "-map", "[outa]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-map_metadata", "-1", str(staged),
        ]
        proc = _safe_call(
            argv, timeout=min(420.0, max(90.0, duration * 4.0)),
        )
        if proc.returncode != 0 or not staged.is_file():
            raise ValueError("LOCAL_AUDIO_ENCODE_FAILED")
        out_duration, out_video, out_audio = _probe(staged)
        if not out_video or not out_audio or abs(out_duration - duration) > 0.5:
            raise ValueError("LOCAL_AUDIO_QA_FAILED")
        after = [_measure(ffmpeg, staged, *cut) for cut in ranges]
        evidence = []
        for ((mean, peak), (out_mean, out_peak), gain) in zip(before, after, gains):
            # Allow AAC and boundary-frame tolerance, but reject unexplained
            # major gain divergence and output sample clipping.
            if out_peak > 0.1:
                raise ValueError("LOCAL_AUDIO_OUTPUT_PEAK_UNSAFE")
            if peak > -48.0 and mean > -50.0 and abs((out_mean - mean) - gain) > 4.0:
                raise ValueError("LOCAL_AUDIO_GAIN_QA_FAILED")
            evidence.append({
                "before_mean_dbfs": mean, "before_peak_dbfs": peak,
                "gain_db": gain, "after_mean_dbfs": out_mean,
                "after_peak_dbfs": out_peak,
            })
        if _hash(original)[0] != video_sha256:
            raise ValueError("LOCAL_AUDIO_SOURCE_CHANGED_DURING_EXECUTION")
        digest, _ = _hash(staged)
        try:
            os.link(staged, target)
        except OSError as exc:
            raise ValueError("LOCAL_AUDIO_OUTPUT_COMMIT_FAILED") from exc
    return digest, evidence


def level_preview_audio(
    *, mp4_output: Path, video_sha256: str,
    timeline: ClipTimeline,
) -> dict[str, object]:
    """New-only audio-leveled MP4 with independently measured per-cut gain QA."""
    report: dict[str, object] = {
        "status": "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED",
        "output_sha256": "",
        "cut_count": 0,
        "max_abs_gain_db": 0.0,
        "before_mean_spread_db": None,
        "after_mean_spread_db": None,
        "ready": False,
        "production_ready": False,
    }
    try:
        digest, cuts = _finish(
            video=mp4_output, video_sha256=video_sha256, timeline=timeline,
        )
        # Spread is a diagnostic only, not a certified intelligibility score.
        before = [c["before_mean_dbfs"] for c in cuts if c["before_peak_dbfs"] > -48.0]
        after = [c["after_mean_dbfs"] for c in cuts if c["before_peak_dbfs"] > -48.0]
        report.update({
            "status": "LOCAL_AUDIO_LEVELED_AV_QA_UNVERIFIED_EDITORIAL",
            "output_sha256": digest,
            "cut_count": len(cuts),
            "max_abs_gain_db": max(abs(c["gain_db"]) for c in cuts),
            "before_mean_spread_db": round(max(before) - min(before), 2) if before else None,
            "after_mean_spread_db": round(max(after) - min(after), 2) if after else None,
        })
    except (ValueError, OSError, TypeError, UnicodeError):
        pass
    return report
