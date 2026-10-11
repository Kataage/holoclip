"""Bounded, source-clock-preserving origin comparison remains unlabelled."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from holoclip.domain import ClipTimeline, TimelineSegment
from holoclip.render_plan import RenderPlan, RenderSourceSnapshot
from holoclip import render_source_context as sc


def _case(tmp_path, *, two_cuts=False, event_kind="blackout"):
    src_a = tmp_path / "source-a.mp4"
    src_a.write_bytes(b"cached source A")
    sources = [
        RenderSourceSnapshot(
            asset_id="a", path=src_a,
            sha256=hashlib.sha256(src_a.read_bytes()).hexdigest(),
            size_bytes=src_a.stat().st_size, duration_seconds=30,
            has_video=True, has_audio=True,
        )
    ]
    cuts = [
        TimelineSegment(
            id="first", source_asset_id="a", source_start=10,
            source_end=14 if two_cuts else 15, output_order=0,
        )
    ]
    if two_cuts:
        src_b = tmp_path / "source-b.mp4"
        src_b.write_bytes(b"cached source B")
        sources.append(RenderSourceSnapshot(
            asset_id="b", path=src_b,
            sha256=hashlib.sha256(src_b.read_bytes()).hexdigest(),
            size_bytes=src_b.stat().st_size, duration_seconds=30,
            has_video=True, has_audio=True,
        ))
        cuts.append(TimelineSegment(
            id="second", source_asset_id="b", source_start=20,
            source_end=23, output_order=1,
        ))
    duration = 7 if two_cuts else 5
    timeline = ClipTimeline(
        id="source-event-context", segments=cuts,
        estimated_duration=duration,
    )
    output = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    output.write_bytes(b"previously rendered verified local MP4")
    plan = RenderPlan(
        id="test-plan", timeline=timeline, sources=sources,
        output=output, expected_duration=duration,
    )
    when, end = (1.0, 3.0) if event_kind == "blackout" else (1.0, None)
    observations = [
        {"kind": event_kind, "start_seconds": when, "end_seconds": end},
    ]
    review = output.with_suffix(".review.json")
    review_doc = {
        "version": "holoclip-review-ledger-v1",
        "base_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "final_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "candidates": [
            {"kind": event_kind, "start_seconds": when, "end_seconds": end,
             "segment_ids": ["first"], "auto_repair_allowed": False},
        ],
        "human_editorial_qa_completed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False,
    }
    review.write_text(json.dumps(review_doc), encoding="utf-8")
    return plan, observations, hashlib.sha256(review.read_bytes()).hexdigest()


def _inspect(plan, observations, review_sha, **kwargs):
    return sc.compare_local_source_context(
        plan=plan, observed=observations, review_sha256=review_sha,
        video_sha256=hashlib.sha256(Path(plan.output).read_bytes()).hexdigest(),
        scan_allowed=True, **kwargs,
    )


def test_source_window_output_clock_maps_to_precise_original_source(tmp_path, monkeypatch):
    plan, events, sha = _case(tmp_path)
    positions = []

    def record(executable, path, *, begin, seconds, kind):
        positions.append((path, begin, seconds, kind))
        return True

    monkeypatch.setattr(sc, "_source_signal", record)
    monkeypatch.setattr(sc.shutil, "which", lambda name: name)
    result = _inspect(plan, events, sha)
    assert result["status"] == "LOCAL_SOURCE_CONTEXT_SAMPLED_UNLABELLED"
    assert result["matched_count"] == 1
    assert result["no_match_count"] == 0
    assert positions[0][0] == plan.sources[0].path
    assert positions[0][1] == pytest.approx(10.7)
    assert positions[0][2] == pytest.approx(2.6)
    context_file = plan.output.with_name(f"{plan.output.stem}.source-context.json")
    data = context_file.read_bytes()
    assert hashlib.sha256(data).hexdigest() == result["sha256"]
    saved = json.loads(data)
    assert saved["comparisons"][0]["status"] == (
        "SIMILAR_SIGNAL_OBSERVED_IN_SOURCE_WINDOW"
    )
    assert saved["source_bytes_independently_rehashed"] is False
    assert saved["auto_repair_allowed"] is False
    assert saved["ready"] is False
    assert str(tmp_path) not in data.decode()
    assert not list(tmp_path.glob("*.repaired.mp4"))


def test_no_matching_source_signal_is_still_only_review_hint(tmp_path, monkeypatch):
    plan, events, sha = _case(tmp_path)
    monkeypatch.setattr(sc, "_source_signal", lambda *args, **kwargs: False)
    monkeypatch.setattr(sc.shutil, "which", lambda name: name)
    result = _inspect(plan, events, sha)
    assert result["no_match_count"] == 1
    assert not result["auto_repair_allowed"] and not result["ready"]
    saved = json.loads(
        plan.output.with_name(f"{plan.output.stem}.source-context.json").read_text()
    )
    assert saved["comparisons"][0]["status"] == (
        "NO_SIMILAR_SIGNAL_IN_SAMPLED_SOURCE_WINDOW"
    )
    assert saved["human_editorial_qa_completed"] is False


def test_cross_cut_event_skips_both_sources_instead_of_claiming_origin(
    tmp_path, monkeypatch,
):
    plan, events, sha = _case(tmp_path, two_cuts=True)
    events[:] = [{"kind": "blackout", "start_seconds": 3, "end_seconds": 5}]
    ledger = plan.output.with_suffix(".review.json")
    doc = json.loads(ledger.read_text())
    doc["candidates"][0]["start_seconds"] = 3
    ledger.write_text(json.dumps(doc), encoding="utf-8")
    sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    monkeypatch.setattr(sc, "_source_signal", lambda *args, **kwargs:
                        pytest.fail("Cross-cut event cannot be compared to one source"))
    monkeypatch.setattr(sc.shutil, "which", lambda name: name)
    report = _inspect(plan, events, sha)
    assert report["status"] == "LOCAL_SOURCE_CONTEXT_SAMPLED_UNLABELLED"
    assert report["matched_count"] == report["no_match_count"] == 0
    saved = json.loads(
        plan.output.with_name(f"{plan.output.stem}.source-context.json").read_text()
    )
    assert saved["comparisons"][0]["status"] == (
        "SKIPPED_BOUNDARY_OR_SHORT_OBSERVATION"
    )


@pytest.mark.parametrize("reason", ["review_mismatch", "video_sha_mismatch", "scan_skipped"])
def test_missing_or_tampered_evidence_never_produces_source_claim(
    tmp_path, monkeypatch, reason,
):
    plan, events, sha = _case(tmp_path)
    monkeypatch.setattr(sc, "_source_signal", lambda *args, **kwargs:
                        pytest.fail("Untrusted comparison must not scan media"))
    params = dict(
        plan=plan, review_sha256=sha, video_sha256=hashlib.sha256(
            plan.output.read_bytes()
        ).hexdigest(),
        observed=events, scan_allowed=True,
    )
    if reason == "review_mismatch":
        params["review_sha256"] = "0" * 64
    if reason == "video_sha_mismatch":
        params["video_sha256"] = "f" * 64
    if reason == "scan_skipped":
        params["scan_allowed"] = False
    report = sc.compare_local_source_context(**params)
    assert report["status"] == "LOCAL_SOURCE_CONTEXT_UNAVAILABLE"
    assert not plan.output.with_name(f"{plan.output.stem}.source-context.json").exists()


def test_source_window_failure_is_recorded_as_unavailable_not_no_match(tmp_path, monkeypatch):
    plan, events, sha = _case(tmp_path)
    monkeypatch.setattr(sc.shutil, "which", lambda name: name)
    monkeypatch.setattr(sc, "_source_signal", lambda *args, **kwargs:
                        (_ for _ in ()).throw(ValueError("simulated FFmpeg timeout")))
    result = _inspect(plan, events, sha)
    assert result["status"] == "LOCAL_SOURCE_CONTEXT_SAMPLED_UNLABELLED"
    assert result["matched_count"] == result["no_match_count"] == 0
    saved = json.loads(
        plan.output.with_name(f"{plan.output.stem}.source-context.json").read_text()
    )
    assert saved["summary"]["unavailable"] == 1
    assert saved["comparisons"][0]["status"] == "SOURCE_WINDOW_UNAVAILABLE"


def test_does_not_overwrite_existing_source_context(tmp_path, monkeypatch):
    plan, events, sha = _case(tmp_path)
    dest = plan.output.with_name(f"{plan.output.stem}.source-context.json")
    dest.write_bytes(b"previous independent operator review")
    monkeypatch.setattr(sc, "_source_signal", lambda *args, **kwargs:
                        pytest.fail("Existing sidecar must not trigger a scan"))
    result = _inspect(plan, events, sha)
    assert result["status"] == "LOCAL_SOURCE_CONTEXT_UNAVAILABLE"
    assert dest.read_bytes() == b"previous independent operator review"


def test_ffmpeg_command_scans_only_local_bounded_window(tmp_path, monkeypatch):
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0, stderr=(
                b"[blackdetect] black_start:0 black_end:2.0 "
                b"black_duration:2.0"
            )
        )
    monkeypatch.setattr(sc.subprocess, "run", run)
    assert sc._source_signal(
        "ffmpeg", tmp_path / "original.mp4",
        begin=105.25, seconds=3.4, kind="blackout",
    ) is True
    argv, opts = seen[0]
    assert "-protocol_whitelist" in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    assert argv[argv.index("-ss") + 1] == "105.250"
    assert argv[argv.index("-t") + 1] == "3.400"
    assert "blackdetect=" in argv[argv.index("-vf") + 1]
    assert "freezedetect=" in argv[argv.index("-vf") + 1]
    assert opts["timeout"] == 15.0 and opts["stdin"] == subprocess.DEVNULL
    assert "-nostdin" in argv and "-an" in argv
    assert "-y" not in argv


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("color,expected", [("black", True), ("white", False)])
def test_synthetic_real_ffmpeg_black_signal_in_source(tmp_path, color, expected):
    ffmpeg = shutil.which("ffmpeg")
    video = tmp_path / f"{color}.mp4"
    result = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=30:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "mpeg4", "-q:v", "6", "-c:a", "aac",
        "-shortest", str(video),
    ], capture_output=True, check=False, timeout=50)
    assert result.returncode == 0
    observed = sc._source_signal(
        ffmpeg, video, begin=0.4, seconds=3.0, kind="blackout",
    )
    assert observed is expected
