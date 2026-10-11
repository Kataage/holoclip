"""Offline technical edit diagnostics for an existing local render.

Signals are REVIEW HINTS, never proof of editorial quality or grounds to
remove a scene. Original media and subtitles are read-only. No network, model,
OCR, upload, recovery, automatic retry, or production qualification.
"""
from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
from pathlib import Path

from holoclip.domain import ClipTimeline
from holoclip.render_audio_finish import _measure
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_subtitle_burnin import _read_cues, _wrap

DIAGNOSTIC_VERSION = "holoclip-edit-diagnostic-v1"
_MAX_SECONDS = 180.0
_MAX_LOG = 65536
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_BLACK = re.compile(
    rb"black_start:\s*(\d+(?:\.\d+)?)\s+"
    rb"black_end:\s*(\d+(?:\.\d+)?)\s+"
    rb"black_duration:\s*(\d+(?:\.\d+)?)"
)
_FREEZE = re.compile(rb"lavfi\.freezedetect\.freeze_start:\s*(\d+(?:\.\d+)?)")
_MAX_FLAGS = 100
_SUBTITLE_MAX_CPS = 20.0


def _video_events(
    ffmpeg: str, video: Path, duration: float, *, detailed: bool = False,
) -> tuple:
    """Scan scaled local frames at 2fps for likely blackout/freeze intervals."""
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
        "-nostdin", "-protocol_whitelist", "file,pipe",
        "-i", str(video), "-map", "0:v:0", "-an",
        "-vf", (
            "fps=2,scale=160:90,format=yuv420p,"
            "blackdetect=d=1.0:pix_th=0.10:pic_th=0.98,"
            "freezedetect=n=-60dB:d=2.0"
        ),
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=min(60.0, max(15.0, duration * 0.5)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_VIDEO_UNAVAILABLE") from exc
    if proc.returncode != 0 or len(proc.stderr) > _MAX_LOG:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_VIDEO_UNAVAILABLE")
    black = _BLACK.findall(proc.stderr)
    freeze = _FREEZE.findall(proc.stderr)
    if len(black) > _MAX_FLAGS or len(freeze) > _MAX_FLAGS:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_EVENT_LIMIT")
    observations: list[dict[str, object]] = []
    for first, last, span in black:
        start, end, length = float(first), float(last), float(span)
        if not all(math.isfinite(n) for n in (start, end, length)):
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_EVENT_INVALID")
        if start < -0.01 or end < start or end > duration + 1.1:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_EVENT_INVALID")
        if abs((end - start) - length) > 0.1:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_EVENT_INVALID")
        observations.append({
            "kind": "blackout", "start_seconds": start, "end_seconds": end,
        })
    for moment in freeze:
        when = float(moment)
        if not math.isfinite(when) or when < -0.01 or when > duration + 1.1:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_EVENT_INVALID")
        observations.append({
            "kind": "freeze", "start_seconds": when, "end_seconds": None,
        })
    if detailed:
        return len(black), len(freeze), observations
    return len(black), len(freeze)


def _subtitle_flags(
    *, base: Path, caption_sha256: str | None,
    duration: float, detailed: bool = False,
) -> tuple:
    """Check only the SRT generated and SHA-verified by our existing stage."""
    if not caption_sha256:
        return (False, 0, 0, None, []) if detailed else (False, 0, 0, None)
    if not _SHA.fullmatch(caption_sha256):
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_CAPTION_HASH_INVALID")
    subtitle = _no_links(base.with_suffix(".srt"))
    if not subtitle.is_file() or subtitle.stat().st_size > 131072:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_CAPTION_UNVERIFIED")
    data = subtitle.read_bytes()
    if hashlib.sha256(data).hexdigest() != caption_sha256:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_CAPTION_CHANGED")
    cues = _read_cues(data)
    fast = 0
    overflow = 0
    max_cps = 0.0
    observations: list[dict[str, object]] = []
    for index, cue in enumerate(cues, start=1):
        if cue.end > duration + 0.1:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_CAPTION_CLOCK_INVALID")
        chars = len(cue.text)
        cps = chars / (cue.end - cue.start)
        if cps > _SUBTITLE_MAX_CPS:
            fast += 1
            observations.append({
                "kind": "fast_subtitle",
                "start_seconds": cue.start,
                "end_seconds": cue.end,
                "cue_number": index,
            })
        try:
            _wrap(cue.text)
        except ValueError:
            overflow += 1
            observations.append({
                "kind": "wide_subtitle",
                "start_seconds": cue.start,
                "end_seconds": cue.end,
                "cue_number": index,
            })
        max_cps = max(max_cps, cps)
    # A zero-cue generated SRT must never be claimed as checked evidence.
    if detailed:
        return True, fast, overflow, round(max_cps, 2), observations
    return True, fast, overflow, round(max_cps, 2)


def _validate_media(
    *, base: Path, base_sha256: str, final: Path, final_sha256: str,
    timeline: ClipTimeline,
) -> float:
    if not _SHA.fullmatch(base_sha256) or not _SHA.fullmatch(final_sha256):
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_HASH_INVALID")
    base = _no_links(base)
    final = _no_links(final)
    if (base.suffix.lower() != ".mp4" or final.suffix.lower() != ".mp4"
            or not base.is_file() or not final.is_file()
            or base.parent != final.parent):
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_MEDIA_UNTRUSTED")
    # Only original, previously verified leveled or captioned siblings.
    if final not in {
        base, base.with_name(f"{base.stem}.leveled.mp4"),
        base.with_name(f"{base.stem}.captioned.mp4"),
    }:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_FINAL_ID_INVALID")
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    expected = timeline.estimated_duration
    if not 0 < expected <= _MAX_SECONDS:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_DURATION_LIMIT")
    base_duration, bv, ba = _probe(base)
    final_duration, fv, fa = _probe(final)
    if (not all((bv, ba, fv, fa))
            or abs(base_duration - expected) > 0.5
            or abs(final_duration - base_duration) > 0.5):
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_AV_UNVERIFIED")
    if _hash(base)[0] != base_sha256 or _hash(final)[0] != final_sha256:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_MEDIA_CHANGED")
    return base_duration


def _run(
    *, base: Path, base_sha256: str, final: Path, final_sha256: str,
    timeline: ClipTimeline, caption_sha256: str | None,
) -> dict[str, object]:
    duration = _validate_media(
        base=base, base_sha256=base_sha256,
        final=final, final_sha256=final_sha256, timeline=timeline,
    )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_FFMPEG_UNAVAILABLE")
    black, freeze, video_observations = _video_events(
        ffmpeg, base, duration, detailed=True,
    )
    mean, peak = _measure(ffmpeg, final, 0.0, min(duration, 10.0))
    checked, fast, overflow, cps, subtitle_observations = _subtitle_flags(
        base=base, caption_sha256=caption_sha256, duration=duration,
        detailed=True,
    )
    if (_hash(base)[0] != base_sha256 or _hash(final)[0] != final_sha256):
        raise ValueError("LOCAL_EDIT_DIAGNOSTIC_MEDIA_CHANGED_DURING_SCAN")
    if caption_sha256:
        subtitle = _no_links(base.with_suffix(".srt"))
        if hashlib.sha256(subtitle.read_bytes()).hexdigest() != caption_sha256:
            raise ValueError("LOCAL_EDIT_DIAGNOSTIC_CAPTION_CHANGED_DURING_SCAN")
    # These are warnings only. Static graphics, blank transitions and loud
    # reactions may all be intentional; never auto-fix or discard the source.
    flags = black + freeze + fast + overflow
    flags += int(peak > -0.5 or mean < -38.0)
    observations = video_observations + subtitle_observations
    if peak > -0.5 or mean < -38.0:
        observations.append({
            "kind": "audio_peak" if peak > -0.5 else "audio_low_mean",
            "start_seconds": 0.0,
            "end_seconds": min(duration, 10.0),
        })
    status = (
        "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS" if flags
        else ("LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS" if checked
              else "LOCAL_EDIT_DIAGNOSTIC_MEASURED_PARTIAL")
    )
    return {
        "status": status,
        "version": DIAGNOSTIC_VERSION,
        "base_sha256": base_sha256,
        "final_sha256": final_sha256,
        "black_interval_count": black,
        "freeze_interval_count": freeze,
        "subtitle_checked": checked,
        "subtitle_fast_cue_count": fast,
        "subtitle_overflow_count": overflow,
        "subtitle_max_cps": cps,
        "audio_sample_mean_dbfs": mean,
        "audio_sample_peak_dbfs": peak,
        "flag_count": flags,
        # Time-coded, content-free candidates. Never an actual repair.
        "review_observations": observations,
    }


def evaluate_local_edit_quality(
    *, mp4_output: Path, video_sha256: str,
    final_output: Path, final_sha256: str,
    timeline: ClipTimeline, caption_sha256: str | None,
    scan_allowed: bool = True,
) -> dict[str, object]:
    """Fail-safe diagnostic. No returned status makes a video production ready."""
    result: dict[str, object] = {
        "status": "LOCAL_EDIT_DIAGNOSTIC_UNAVAILABLE",
        "version": DIAGNOSTIC_VERSION,
        "base_sha256": video_sha256,
        "final_sha256": final_sha256,
        "black_interval_count": 0,
        "freeze_interval_count": 0,
        "subtitle_checked": False,
        "subtitle_fast_cue_count": 0,
        "subtitle_overflow_count": 0,
        "subtitle_max_cps": None,
        "audio_sample_mean_dbfs": None,
        "audio_sample_peak_dbfs": None,
        "flag_count": 0,
        "review_observations": [],
        "publication_performed": False,
        "human_editorial_qa_completed": False,
        "ready": False,
        "production_ready": False,
    }
    if not scan_allowed:
        return result
    try:
        result.update(_run(
            base=mp4_output, base_sha256=video_sha256,
            final=final_output, final_sha256=final_sha256,
            timeline=timeline, caption_sha256=caption_sha256,
        ))
    except (OSError, ValueError, TypeError, UnicodeError):
        # Do not leak user paths, transcript contents or raw ffmpeg output.
        pass
    return result
