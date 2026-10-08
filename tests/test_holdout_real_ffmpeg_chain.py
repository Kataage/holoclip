"""One full nine-stage offline test using actual FFmpeg-encoded media bytes.

This is synthetic authored test media, NOT a real Hololive-video quality study.
Unlike fake runner tests, every decoder/FFT/manifest hash/measurement is real.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.holdout_e2e_trial import run_reserved_holdout_e2e, STAGES
from holoclip.holdout_diagnosis import diagnose_holdout_e2e
from holoclip.scripted_cut_metrics import score_scripted_cut_report, REFERENCE_VERSION
from holoclip.providers.source.base import (
    AcquiredSource, AcquiredVideoRange, AcquisitionProfile,
    SourceCandidate, SourceMetadata,
)

CLIP_ID = "aaaaaaaaaaa"
SOURCE_ID = "bbbbbbbbbbb"
CLIP_URL = f"https://www.youtube.com/watch?v={CLIP_ID}"
SOURCE_URL = f"https://www.youtube.com/watch?v={SOURCE_ID}"


def _synthetic_audio(wav: Path, *, seconds: int = 78, rate: int = 16000) -> None:
    """Generate nonrepeating broad-band energy fingerprints without sample fixture mocks."""
    rng = np.random.default_rng(20261008)
    block_size = int(rate * .25)
    samples = np.empty((seconds * rate,), dtype=np.int16)
    frequencies = np.geomspace(92., 1430., 12)
    cursor = 0
    for _ in range(math.ceil(seconds * rate / block_size)):
        positions = np.arange(block_size, dtype=np.float64) / rate
        # Different random frequency mixtures per acoustic fingerprint hop.
        amps = rng.uniform(.045, .32, size=12)
        phases = rng.uniform(0, 2 * np.pi, size=12)
        signal = np.zeros_like(positions)
        for frequency, amp, phase in zip(frequencies, amps, phases):
            signal += amp * np.sin(2 * np.pi * frequency * positions + phase)
        signal = np.clip(signal / 3.6, -.95, .95)
        n = min(block_size, len(samples) - cursor)
        if not n:
            break
        samples[cursor:cursor + n] = np.rint(signal[:n] * 32767).astype(np.int16)
        cursor += n
    with wave.open(str(wav), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, check=True, timeout=120,
    )


def _generated_media(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    wav = root / "original.wav"
    original = root / "original.mkv"
    finished = root / "finished.mp4"
    _synthetic_audio(wav)
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=192x108:rate=8:duration=78",
        "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-c:a", "flac", "-shortest", str(original),
    )
    _ffmpeg(
        "-ss", "18", "-i", str(original), "-t", "45",
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-c:a", "aac", "-b:a", "128k", str(finished),
    )
    return wav, original, finished


class GeneratedVideoProvider:
    """Fully local, deterministic synthetic source with true bounded FFmpeg seeks."""

    def __init__(self, wav: Path, original: Path, finished: Path):
        self.wav = wav
        self.original = original
        self.finished = finished
        self.video_range_requests: list[tuple[float, float]] = []

    def probe(self, url: str) -> SourceMetadata:
        if url not in (CLIP_URL, SOURCE_URL):
            raise ValueError("Unknown generated media URL")
        source = url == SOURCE_URL
        return SourceMetadata(
            provider="synthetic-ffmpeg-test", url=url,
            external_id=SOURCE_ID if source else CLIP_ID,
            title="generated media for tests", channel="test",
            availability="public", live_status="was_live" if source else "not_live",
            duration=78. if source else 45.,
        )

    def source_candidates(self, metadata: SourceMetadata) -> list[SourceCandidate]:
        if metadata.url != CLIP_URL:
            return []
        return [SourceCandidate(
            url=SOURCE_URL, score=.99, reason="description_explicit_source_label",
        )]

    def acquire(
        self, url: str, output_root: Path, *,
        profile: AcquisitionProfile = AcquisitionProfile.TRAINING_AUDIO,
    ) -> AcquiredSource:
        if url == SOURCE_URL and profile == AcquisitionProfile.TRAINING_AUDIO:
            media = self.wav
        elif url == CLIP_URL and profile == AcquisitionProfile.EDITING_VIDEO:
            media = self.finished
        else:
            raise ValueError("Test provider disallows other acquisition paths")
        return AcquiredSource(
            metadata=self.probe(url), profile=profile, media_path=media,
        )

    def acquire_video_range(
        self, url: str, output_root: Path, *, start: float, end: float,
    ) -> AcquiredVideoRange:
        if url != SOURCE_URL or not 0 <= start < end <= 78 or end - start > 120:
            raise ValueError("Unsafe or non-bounded source range")
        self.video_range_requests.append((start, end))
        target = output_root / f"range-{start:.3f}-{end:.3f}.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg(
            "-ss", f"{start:.3f}", "-i", str(self.original),
            "-t", f"{end - start:.3f}",
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-c:a", "aac", "-b:a", "128k", str(target),
        )
        return AcquiredVideoRange(
            metadata=self.probe(url), media_path=target,
            requested_start=start, requested_end=end,
        )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="Actual FFmpeg and ffprobe required",
)
def test_generated_ffmpeg_source_and_clip_through_all_nine_real_stages(tmp_path):
    wav, original, finished = _generated_media(tmp_path / "generated")
    provider = GeneratedVideoProvider(wav, original, finished)
    store = tmp_path / "learning.jsonl"
    state_path = tmp_path / "growth.json"
    report_root = tmp_path / "heldout"
    state = CorpusGrowthState(
        holdout_stream_ids=[SOURCE_ID],
        items={"generated": CorpusGrowthItem(
            clip_url=CLIP_URL, source_url=SOURCE_URL,
            source_stream_id=SOURCE_ID, query="generated media only",
            status="holdout_reserved",
        )},
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    log = []
    trial = run_reserved_holdout_e2e(
        provider, store=store, state_path=state_path, root=report_root,
        policy_rules=[], max_clips=1, progress=log.append,
    )
    assert trial["status"] == "UNREVIEWED_MEASUREMENTS_COMPLETE", (
        trial["status"], trial.get("blocked_stage"), trial.get("error_class"),
        trial.get("observations", {}).get("stage_status_counts"), log,
    )
    assert trial["real_media_run_completed"] is True
    assert trial["ready"] is False and trial["qualification_allowed"] is False
    assert len(trial["stages"]) == len(STAGES) == 9
    assert provider.video_range_requests
    assert all(0 < end - start <= 120 for start, end in provider.video_range_requests)
    observed = trial["observations"]
    assert observed["clips_with_unique_original_audio_matches"] == 1
    assert observed["all_nine_stages_measured_clips"] == 1
    assert observed["editor_cut_precision"] is None
    assert observed["editor_cut_recall"] is None
    assert observed["native_timestamp_error_seconds"] is None
    assert observed["event_holdout_accuracy"] is None
    # A continuous 45-second reencoded source excerpt has ZERO constructed
    # hard-cut boundaries; test the actual scored false-positive count.
    cut_doc = json.loads(
        (report_root / "cut_brackets" / "holdout-cut-brackets.json").read_text(
            encoding="utf-8"
        )
    )
    control_score = score_scripted_cut_report({
        "version": REFERENCE_VERSION,
        "reference_kind": "SCRIPTED_FFMPEG_ONLY",
        "clip_id": human_clip_identity(CLIP_URL), "duration_seconds": 45.,
        "cut_seconds": [],
    }, cut_doc)
    assert control_score["scripted_cut_count"] == 0
    assert control_score["false_positive_candidates"] == 0
    assert control_score["scripted_interval_recall"] is None
    assert control_score["editor_cut_precision"] is None
    assert control_score["ready"] is False
    # This control is a simple unedited excerpt. Do not call missing image
    # links/candidate maps a verified cut or a successful editor learning task.
    for field in ("reviewed_gold_available", "benchmark_held_out",
                  "event_split_verified", "frame_accurate_cuts_verified",
                  "dense_output_source_map_verified"):
        assert trial[field] is False
    assert json.loads((report_root / "holdout-e2e-audit.json").read_text())["status"] == (
        "UNREVIEWED_MEASUREMENTS_COMPLETE"
    )
    diagnosis = diagnose_holdout_e2e(
        store=store, state_path=state_path, root=report_root,
    )
    assert diagnosis["status"] == "UNREVIEWED_TRIAL_RECORDED"
    assert diagnosis["audit_kind"] == "SINGLE_BATCH"
    assert diagnosis["attempted_clips"] == 1
    assert diagnosis["unattempted_clip_ids"] == []
    assert diagnosis["first_blocker"] is None
    assert diagnosis["real_media_run_completed"] is True
    assert diagnosis["network_or_media_examined"] is False
    assert diagnosis["ready"] is False
    assert diagnosis["reviewed_gold_available"] is False
    assert diagnosis["editor_cut_precision"] is None
