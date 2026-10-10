"""Optional offline Japanese caption burn-in for a QA-checked local MP4.

Keep the original technical preview and SRT intact. Never overwrite an output,
contact a network provider, or use this as evidence of editorial quality.
An untrusted or absent subtitle/font/renderer is a recorded optional failure.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from holoclip.render_plan import _hash, _no_links, _probe

_MAX_SRT_BYTES = 131072
_MAX_CUES = 400
_TIME = re.compile(r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})\Z")
_RANGE = re.compile(
    r"(\d{2}:[0-5]\d:[0-5]\d,\d{3}) --> "
    r"(\d{2}:[0-5]\d:[0-5]\d,\d{3})\Z"
)
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


def _seconds(raw: str) -> float:
    match = _TIME.fullmatch(raw)
    if match is None:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_TIME_UNTRUSTED")
    hours, minutes, seconds, millis = (int(n) for n in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _read_cues(data: bytes) -> list[Caption]:
    if not 0 < len(data) <= _MAX_SRT_BYTES:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_UNTRUSTED")
    try:
        raw = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_UNTRUSTED") from exc
    if "\r" in raw or "\x00" in raw:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_UNTRUSTED")
    blocks = raw.strip().split("\n\n")
    if not 1 <= len(blocks) <= _MAX_CUES:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_CUE_LIMIT")
    result: list[Caption] = []
    previous_end = 0.0
    for idx, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) != 3 or lines[0] != str(idx):
            raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_UNTRUSTED")
        match = _RANGE.fullmatch(lines[1])
        if match is None:
            raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_UNTRUSTED")
        start, end = (_seconds(value) for value in match.groups())
        if start + 0.001 < previous_end or not 0.25 <= end - start <= 8.001:
            raise ValueError("LOCAL_SUBTITLE_BURNIN_TIMING_UNTRUSTED")
        text = lines[2]
        if (not text or len(text) > 120 or any(
            c in r"\{}" or unicodedata.category(c) in {"Cc", "Cf", "Cs"}
            for c in text
        )):
            raise ValueError("LOCAL_SUBTITLE_BURNIN_TEXT_UNTRUSTED")
        result.append(Caption(start, end, text))
        previous_end = end
    return result


def _wrap(text: str) -> str:
    """At most two readable lines; never shrink illegible captions to fit."""
    lines = [""]
    width = 0
    for char in text:
        cell = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if width + cell > 38:
            if len(lines) >= 2:
                raise ValueError("LOCAL_SUBTITLE_BURNIN_TEXT_TOO_LONG")
            lines.append("")
            width = 0
        lines[-1] += char
        width += cell
    return r"\N".join(lines)


def _ass_time(seconds: float) -> str:
    centis = round(seconds * 100)
    hours, rem = divmod(centis, 360000)
    mins, rem = divmod(rem, 6000)
    sec, cs = divmod(rem, 100)
    return f"{hours}:{mins:02d}:{sec:02d}.{cs:02d}"


def _ass(cues: list[Caption], *, font_name: str) -> str:
    if font_name not in {"Meiryo", "Yu Gothic", "Noto Sans CJK JP", "TakaoGothic"}:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_FONT_UNTRUSTED")
    preamble = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\n"
        "PlayResY: 720\nScaledBorderAndShadow: yes\nWrapStyle: 0\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: JP,{font_name},43,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,64,64,48,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    body = "".join(
        f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},"
        f"JP,,0,0,0,,{_wrap(c.text)}\n"
        for c in cues
    )
    return preamble + body


def _japanese_font() -> tuple[Path, str] | None:
    windows = Path(os.environ.get("WINDIR", os.environ.get("SystemRoot", "C:/Windows")))
    candidates = [
        (windows / "Fonts" / "meiryo.ttc", "Meiryo"),
        (windows / "Fonts" / "YuGothM.ttc", "Yu Gothic"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
         "Noto Sans CJK JP"),
        (Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
         "Noto Sans CJK JP"),
        (Path("/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf"),
         "TakaoGothic"),
    ]
    for candidate, name in candidates:
        try:
            path = _no_links(candidate)
            if path.is_file() and 0 < path.stat().st_size <= 100 * 1024 * 1024:
                return path, name
        except (OSError, ValueError):
            continue
    return None


def _burn(
    video: Path, subtitle: Path, target: Path, *,
    video_sha: str, srt_sha: str,
) -> tuple[str, int]:
    if not _SHA256.fullmatch(video_sha) or not _SHA256.fullmatch(srt_sha):
        raise ValueError("LOCAL_SUBTITLE_BURNIN_HASH_UNTRUSTED")
    if (_no_links(video) != video.absolute()
            or video.suffix.lower() != ".mp4"
            or not video.is_file() or target.exists()
            or subtitle != video.with_suffix(".srt")
            or not subtitle.is_file()):
        raise ValueError("LOCAL_SUBTITLE_BURNIN_INPUT_UNTRUSTED")
    srt = subtitle.read_bytes()
    if hashlib.sha256(srt).hexdigest() != srt_sha:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_SRT_CHANGED")
    cues = _read_cues(srt)
    measured, has_video, has_audio = _probe(video)
    if not has_video or not has_audio or not 0 < measured <= 1800.0:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_AV_UNVERIFIED")
    if _hash(video)[0] != video_sha:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_VIDEO_CHANGED")
    font = _japanese_font()
    if font is None:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_FONT_UNAVAILABLE")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_SUBTITLE_BURNIN_FFMPEG_UNAVAILABLE")
    font_path, font_name = font
    with tempfile.TemporaryDirectory(prefix=".holoclip-burnin-", dir=video.parent) as work:
        tmp = Path(work)
        fonts = tmp / "fonts"
        fonts.mkdir()
        shutil.copyfile(font_path, fonts / ("caption" + font_path.suffix.lower()))
        (tmp / "captions.ass").write_text(
            _ass(cues, font_name=font_name), encoding="utf-8",
        )
        staged = tmp / "captioned.mp4"
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-protocol_whitelist", "file,pipe", "-i", str(video),
            "-vf", "ass=filename=captions.ass:fontsdir=fonts",
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
            "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", "-map_metadata", "-1",
            str(staged),
        ]
        try:
            proc = subprocess.run(
                args, cwd=tmp, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # The first MP4 encode can consume 36 minutes; cap this
                # second encode to 10 minutes under the 55-minute task budget.
                timeout=min(600.0, max(90.0, measured * 4.0)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("LOCAL_SUBTITLE_BURNIN_FFMPEG_UNAVAILABLE") from exc
        if proc.returncode != 0 or not staged.is_file():
            raise ValueError("LOCAL_SUBTITLE_BURNIN_FFMPEG_FAILED")
        duration, out_video, out_audio = _probe(staged)
        if not (out_video and out_audio) or abs(duration - measured) > 0.5:
            raise ValueError("LOCAL_SUBTITLE_BURNIN_AV_QA_FAILED")
        if (_hash(video)[0] != video_sha
                or hashlib.sha256(subtitle.read_bytes()).hexdigest() != srt_sha):
            raise ValueError("LOCAL_SUBTITLE_BURNIN_INPUT_CHANGED")
        digest, size = _hash(staged)
        try:
            os.link(staged, target)
        except OSError as exc:
            raise ValueError("LOCAL_SUBTITLE_BURNIN_COMMIT_FAILED") from exc
    return digest, size


def burn_captioned_preview(
    *, mp4_output: Path, video_sha256: str,
    subtitle_sha256: str | None,
) -> dict[str, object]:
    """Produce new-only .captioned.mp4; optional burn failure never alters MP4 QA."""
    report: dict[str, object] = {
        "status": "LOCAL_SUBTITLE_BURNIN_NOT_AVAILABLE",
        "output_sha256": "",
        "subtitle_sha256": subtitle_sha256 or "",
        "ready": False,
        "production_ready": False,
    }
    if not subtitle_sha256:
        return report
    try:
        video = _no_links(mp4_output)
        subtitle = _no_links(video.with_suffix(".srt"))
        target = _no_links(video.with_name(f"{video.stem}.captioned.mp4"))
        digest, _ = _burn(
            video, subtitle, target,
            video_sha=video_sha256, srt_sha=subtitle_sha256,
        )
        report["status"] = "LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL"
        report["output_sha256"] = digest
    except (ValueError, OSError, TypeError, UnicodeError):
        report["status"] = "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"
    return report
