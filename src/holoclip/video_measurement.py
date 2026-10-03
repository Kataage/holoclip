"""Deterministic bounded extraction/sampling, without local AI inference."""
from __future__ import annotations

import bisect
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from holoclip.domain import stable_id
from holoclip.video_semantics import MeasuredFrame, MeasuredVideoWindow

VERSION = "ffprobe-pyscenedetect-frames-v2"


def checksum_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_video(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for authoritative media measurements")
    raw = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                          str(path)], check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(raw.stdout)


def frame_pts(path: Path) -> list[float]:
    raw = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                          "-of", "json", str(path)], check=True, capture_output=True,
                          text=True, encoding="utf-8")
    values = [float(f["best_effort_timestamp_time"]) for f in json.loads(raw.stdout)["frames"]
              if "best_effort_timestamp_time" in f]
    if not values or values != sorted(values):
        raise ValueError("Missing/non-monotonic measured frame PTS")
    return values


def select_frame_indices(pts: list[float], shots: list[tuple[int, int]], dense_times: list[float]) -> list[int]:
    selected = set()
    for start, end in shots:
        if not 0 <= start < end <= len(pts):
            raise ValueError("Scene detector frame index outside ffprobe measurements")
        selected.add((start + end - 1) // 2)
    for time in dense_times:
        for offset in (-0.5, -0.25, 0, 0.25, 0.5):
            index = bisect.bisect_left(pts, max(pts[0], time + offset))
            if index < len(pts):
                selected.add(index)
    # Sampling is bounded by supplied shots/events, never silent random frame truncation.
    return sorted(selected)


def contact_sheets(frames: list[MeasuredFrame], root: Path, *, tiles_per_sheet=6) -> tuple[str, ...]:
    from PIL import Image, ImageDraw
    outputs = []
    for index in range(0, len(frames), tiles_per_sheet):
        group = frames[index:index + tiles_per_sheet]
        tile_width, tile_height, label_height = 640, 360, 40
        sheet = Image.new("RGB", (tile_width * 2, (tile_height + label_height) * ((len(group) + 1) // 2)))
        draw = ImageDraw.Draw(sheet)
        mapping = []
        for tile, frame in enumerate(group):
            x, y = tile % 2 * tile_width, tile // 2 * (tile_height + label_height)
            with Image.open(frame.locator) as image:
                image.thumbnail((tile_width, tile_height))
                sheet.paste(image, (x, y + label_height))
            draw.text((x + 4, y + 8), frame.id, fill="white")
            mapping.append({"tile": tile, "frame_id": frame.id, "shot_id": frame.shot_id,
                            "pts_seconds": frame.pts_seconds})
        path = root / f"contact-{index // tiles_per_sheet:03d}.png"
        sheet.save(path)
        path.with_suffix(".json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        outputs.append(str(path))
    return tuple(outputs)


def measure_window(media: Path, root: Path, *, asset_id: str, start: float, end: float,
                   transcript=None, measured_events=None, dense_times=None) -> MeasuredVideoWindow:
    if not media.is_file() or not 0 <= start < end or end - start > 120:
        raise ValueError("Need existing media and a bounded 0..120-second range")
    probe = probe_video(media)
    if not any(s["codec_type"] == "video" for s in probe["streams"]):
        raise ValueError("Audio-only acquisition cannot qualify video semantics")
    if end > float(probe["format"]["duration"]) + 0.05:
        raise ValueError("Requested window exceeds measured media")
    original_sha = checksum_file(media)
    window_id = stable_id("window", asset_id, original_sha, start, end, VERSION)
    output = root / window_id
    manifest = output / "measurement.json"
    identity = {"input_sha256": original_sha, "range": [start, end], "version": VERSION,
                "transcript": transcript or {}, "events": measured_events or {},
                "dense_times": dense_times or []}
    if manifest.exists():
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if raw["input"] == identity:
            cached = MeasuredVideoWindow.model_validate(raw["window"])
            if checksum_file(Path(cached.video_path)) == cached.media_sha256 and all(
                Path(f.locator).exists() and checksum_file(Path(f.locator)) == f.checksum for f in cached.frames
            ) and raw.get("sheet_checksums") and all(Path(p).exists() and checksum_file(Path(p)) == sha
                    for p, sha in raw["sheet_checksums"].items()):
                return cached
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")
    video = output / "bounded.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(start), "-i", str(media),
                    "-t", str(end - start), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
                    "-crf", "18", "-preset", "fast", "-threads", "1", "-c:a", "aac",
                    "-map_metadata", "-1", "-movflags", "+faststart", str(video)], check=True)
    pts = frame_pts(video)
    from scenedetect import detect
    from scenedetect.detectors import AdaptiveDetector
    scene_list = detect(str(video), AdaptiveDetector(), show_progress=False, start_in_scene=True)
    shots = [(a.frame_num, b.frame_num) for a, b in scene_list] or [(0, len(pts))]
    # PySceneDetect supplies frame indexes; ffprobe supplies time. Keep all shots.
    if shots[-1][1] > len(pts):
        raise ValueError("Scene/PTS frame-count disagreement")
    indices = select_frame_indices(pts, shots, dense_times or [])
    frames = []
    for index in indices:
        shot_index = next(i for i, (a, b) in enumerate(shots) if a <= index < b)
        frame_id = f"{window_id}:f{index:06d}"
        path = output / f"frame-{index:06d}.png"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(video), "-vf",
                        f"select=eq(n\\,{index})", "-frames:v", "1", "-threads", "1", str(path)], check=True)
        frames.append(MeasuredFrame(id=frame_id, shot_id=f"{window_id}:s{shot_index:04d}",
                      pts_seconds=pts[index], locator=str(path), checksum=checksum_file(path)))
    window = MeasuredVideoWindow(id=window_id, source_asset_id=asset_id, source_start=start,
        source_end=end, video_path=str(video), media_sha256=checksum_file(video), frames=tuple(frames),
        contact_sheet_paths=contact_sheets(frames, output), transcript=transcript or {},
        measured_events=measured_events or {}, extraction_version=VERSION)
    manifest.write_text(json.dumps({"input": identity, "window": window.model_dump(mode="json"),
        "sheet_checksums": {p: checksum_file(Path(p)) for sheet in window.contact_sheet_paths
                            for p in (sheet, str(Path(sheet).with_suffix(".json")))},
        "ffmpeg_version": subprocess.run([ffmpeg, "-version"], capture_output=True,
                                          text=True).stdout.splitlines()[0],
        "shots": shots, "pts": pts}, indent=2, ensure_ascii=False), encoding="utf-8")
    return window
