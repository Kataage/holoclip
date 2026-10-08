"""Deterministic, CPU-only audio anchors from an edited clip to its original stream.

This creates *partial, approximate* audio-supported ranges. It does not infer
visual edits, silent cuts, frame-accurate cut boundaries or reviewed gold.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from holoclip.video_measurement import checksum_file

VERSION = "audio-band-anchor-v1"
RATE = 4000
HOP = 0.25
SAMPLES = int(RATE * HOP)


def fingerprint_media(media: Path, cache_root: Path):
    """Decode PCM in small streaming chunks. A multi-hour source stays memory-bounded."""
    import numpy as np

    sha = checksum_file(media)
    cache_root.mkdir(parents=True, exist_ok=True)
    path = cache_root / f"{VERSION}-{sha}.npz"
    if path.exists():
        try:
            with np.load(path, allow_pickle=False) as saved:
                data = saved["features"]
                if (str(saved["version"]) == VERSION and data.ndim == 2
                        and data.shape[1] == 12 and np.isfinite(data).all()
                        and data.dtype == np.float32):
                    return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for audio anchor measurements")
    command = [ffmpeg, "-nostdin", "-v", "error", "-i", str(media), "-map", "0:a:0",
               "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bands = np.geomspace(80, 1500, 13)
    bin_hz = RATE / SAMPLES
    indexes = [(max(1, int(lo / bin_hz)), min(SAMPLES // 2, int(hi / bin_hz) + 1))
               for lo, hi in zip(bands[:-1], bands[1:])]
    features = []
    remaining = b""
    try:
        assert process.stdout is not None
        while block := process.stdout.read(1024 * 1024):
            raw = remaining + block
            count = len(raw) // (SAMPLES * 4)
            if not count:
                remaining = raw
                continue
            chunk = np.frombuffer(raw[:count * SAMPLES * 4], dtype="<f4")
            remaining = raw[count * SAMPLES * 4:]
            samples = chunk.reshape(count, SAMPLES)
            if not np.isfinite(samples).all():
                raise ValueError("Non-finite decoded audio samples")
            samples = samples - samples.mean(axis=1, keepdims=True)
            spectrum = np.abs(np.fft.rfft(samples, axis=1))
            values = np.stack(
                [np.log1p((spectrum[:, left:right] ** 2).mean(axis=1))
                 for left, right in indexes],
                axis=1,
            )
            values -= values.mean(axis=1, keepdims=True)
            norms = np.linalg.norm(values, axis=1, keepdims=True)
            values = np.where(norms > 0.1, values / np.maximum(norms, 1e-8), 0)
            features.append(values.astype(np.float32))
        if process.wait() != 0:
            raise RuntimeError("ffmpeg audio extraction failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if not features:
        raise ValueError("No decodable audio features")
    data = np.concatenate(features, axis=0)
    # Do not reuse a corrupt or partially written cache.
    temporary = path.with_suffix(".tmp.npz")
    np.savez_compressed(temporary, features=data, version=VERSION)
    temporary.replace(path)
    return data


def match_features(clip, source, *, anchor_seconds=6.0, max_anchors=24,
                   min_score=0.79, min_margin=0.065) -> dict:
    """Global normalized multiband correlation, with non-overlap ambiguity checks.

    The FFT computes every possible offset in the original stream; no timestamps
    or source URL start hints are trusted as exact alignments.
    """
    import numpy as np

    clip = np.asarray(clip, dtype=np.float32)
    source = np.asarray(source, dtype=np.float32)
    if (clip.ndim != 2 or source.ndim != 2 or clip.shape[1] != 12
            or source.shape[1] != 12 or not np.isfinite(clip).all()
            or not np.isfinite(source).all()):
        raise ValueError("Expected finite 12-band clip and source fingerprints")
    width = int(round(anchor_seconds / HOP))
    if width < 12 or clip.shape[0] < width or source.shape[0] < width:
        return {"status": "INSUFFICIENT_AUDIO", "anchors": [], "ranges": [],
                "unresolved": ["audio shorter than minimum verified anchor"]}
    count = min(max_anchors, max(1, math.ceil(clip.shape[0] / (20 / HOP))))
    starts = np.unique(np.rint(np.linspace(0, clip.shape[0] - width, count)).astype(int))
    n = source.shape[0]
    fft_size = 1 << (n + width - 2).bit_length()
    source_fft = np.fft.rfft(source, n=fft_size, axis=0)
    accepted = []
    rejected = 0
    for start in starts:
        fragment = clip[start:start + width]
        # Speech/silence absent: per-frame unit norm near zero, no good anchor.
        voiced = np.linalg.norm(fragment, axis=1) > 0.5
        if voiced.mean() < 0.75:
            rejected += 1
            continue
        reverse_fft = np.fft.rfft(fragment[::-1], n=fft_size, axis=0)
        corr = np.fft.irfft((source_fft * reverse_fft).sum(axis=1), n=fft_size)
        scores = corr[width - 1:n] / width
        index = int(scores.argmax())
        score = float(scores[index])
        competitors = scores.copy()
        radius = width * 2
        competitors[max(0, index - radius):min(scores.size, index + radius + 1)] = -1
        runner_up = float(competitors.max()) if competitors.size else -1
        margin = score - runner_up
        if score < min_score or margin < min_margin:
            rejected += 1
            continue
        accepted.append({"clip_start": round(int(start) * HOP, 3),
                         "clip_end": round((int(start) + width) * HOP, 3),
                         "source_start": round(index * HOP, 3),
                         "source_end": round((index + width) * HOP, 3),
                         "correlation": round(score, 5),
                         "uniqueness_margin": round(margin, 5)})
    # These ranges are *supported spans*, not complete editorial cut positions.
    # Only join close adjacent anchors which exhibit stable clip-to-source offset.
    ranges = []
    for anchor in accepted:
        offset = anchor["source_start"] - anchor["clip_start"]
        if (ranges and anchor["clip_start"] <= ranges[-1]["clip_end"] + 25
                and abs(offset - ranges[-1]["offset_seconds"]) <= HOP * 2):
            item = ranges[-1]
            item["clip_end"] = max(item["clip_end"], anchor["clip_end"])
            item["source_end"] = max(item["source_end"], anchor["source_end"])
            item["anchors"] += 1
            item["min_correlation"] = min(item["min_correlation"], anchor["correlation"])
        else:
            ranges.append({"clip_start": anchor["clip_start"], "clip_end": anchor["clip_end"],
                           "source_start": anchor["source_start"], "source_end": anchor["source_end"],
                           "offset_seconds": round(offset, 3), "anchors": 1,
                           "min_correlation": anchor["correlation"]})
    # A single anchor can establish a measured audio match but not a cut map.
    supported = len(accepted) >= 3 and len(ranges) >= 1
    source_monotonic = all(
        right["source_start"] >= left["source_start"]
        for left, right in zip(ranges, ranges[1:])
    )
    unresolved = [
        "Anchor spacing, captions, BGM, fades and silent edits prevent frame-accurate cut claims",
        "Independent original-event and multi-POV leakage checks remain required",
    ]
    if not source_monotonic:
        unresolved.append("Non-monotonic source ordering; could be intentional or ambiguous")
    return {"version": VERSION, "status": ("PARTIAL_AUDIO_MATCH" if supported
                                        else "INSUFFICIENT_UNIQUE_ANCHORS"),
            "anchors": accepted, "ranges": ranges, "rejected_anchors": rejected,
            "attempted_anchors": len(starts), "source_monotonic": source_monotonic,
            "unresolved": unresolved}


def measured_audio_alignment(clip_media: Path, source_media: Path, cache_root: Path,
                             *, fingerprint: Callable = fingerprint_media) -> dict:
    clip_features = fingerprint(clip_media, cache_root)
    source_features = fingerprint(source_media, cache_root)
    result = match_features(clip_features, source_features)
    return {**result, "clip_media_sha256": checksum_file(clip_media),
            "source_media_sha256": checksum_file(source_media),
            "fingerprint_version": VERSION}
