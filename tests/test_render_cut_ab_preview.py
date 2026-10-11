"""Cut seam A/B experiments cannot recut a clip or replace original media."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from holoclip import render_cut_ab_preview as ab
from holoclip.domain import ClipTimeline, TimelineSegment
from holoclip.render_plan import RenderPlan, RenderSourceSnapshot
from holoclip.render_quality_review import REVIEW_VERSION
from holoclip.render_review_triage import TRIAGE_VERSION


def _case(tmp_path, *, one_cut=False, existing_dip=False, flagged=False):
    base = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    base.write_bytes(b"immutable input, not real MP4")
    sha = hashlib.sha256(base.read_bytes()).hexdigest()
    source = tmp_path / "local-source.mp4"
    source.write_bytes(b"local source not used by A/B")
    snapshot = RenderSourceSnapshot(
        asset_id="a", path=source,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        size_bytes=source.stat().st_size, duration_seconds=10,
        has_video=True, has_audio=True,
    )
    segments = [TimelineSegment(
        id="left", source_asset_id="a", source_start=0,
        source_end=2 if not one_cut else 4,
        output_order=0,
    )]
    if not one_cut:
        segments.append(TimelineSegment(
            id="right", source_asset_id="a", source_start=2,
            source_end=4, output_order=1,
        ))
    # Existing dip needs two different sources to be activated. Tests use
    # a separate verified source snapshot and high-confidence hint.
    sources = [snapshot]
    if existing_dip:
        segments[0].confidence = 0.96
        segments[1].source_asset_id = "b"
        segments[1].confidence = 0.96
        segments[1].transition_hint = "dip_to_black"
        sources.append(RenderSourceSnapshot(
            asset_id="b", path=source,
            sha256=snapshot.sha256, size_bytes=snapshot.size_bytes,
            duration_seconds=10, has_video=True, has_audio=True,
        ))
    timeline = ClipTimeline(
        id="test-seam", segments=segments, estimated_duration=4,
    )
    plan = RenderPlan(
        id="test-plan", timeline=timeline, sources=sources,
        output=base, expected_duration=4,
    )
    candidates = []
    if flagged and not one_cut:
        candidates = [{
            "kind": "freeze", "start_seconds": 1.95,
            "end_seconds": None, "segment_ids": ["left", "right"],
            "position": "cut_boundary", "auto_repair_allowed": False,
            "human_editorial_qa_completed": False,
        }]
    review = {
        "version": REVIEW_VERSION,
        "base_sha256": sha, "final_sha256": sha,
        "candidate_count": len(candidates), "candidates": candidates,
        "ready": False, "production_ready": False,
        "human_editorial_qa_completed": False, "publication_performed": False,
    }
    review_path = base.with_name(f"{base.stem}.review.json")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
    triage = {
        "version": TRIAGE_VERSION,
        "review_sha256": review_sha,
        "base_sha256_from_review": sha,
        "final_sha256_from_review": sha,
        "candidate_count": len(candidates),
        "candidate_queue": [
            {
                "review_index": i, "kind": c["kind"],
                "start_seconds": c["start_seconds"],
                "end_seconds": c["end_seconds"],
                "segment_ids": c["segment_ids"],
                "auto_repair_allowed": False,
                "repair_draft": {"operation_authorized": False},
            }
            for i, c in enumerate(candidates)
        ],
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False,
        "repair_execution_performed": False,
        "publication_performed": False,
    }
    triage_path = base.with_name(f"{base.stem}.triage.json")
    triage_path.write_text(json.dumps(triage), encoding="utf-8")
    params = {
        "plan": plan,
        "video_sha256": sha,
        "triage_sha256": hashlib.sha256(triage_path.read_bytes()).hexdigest(),
        "scan_allowed": True,
    }
    return params, (base, source, review_path, triage_path)


def _fake_encoder(monkeypatch):
    encoded = []
    def encode(ffmpeg, original, target, *,
               begin, seconds, relative_seam, experimental):
        encoded.append((experimental, begin, seconds, relative_seam))
        target.write_bytes(
            b"after experimental trial" if experimental else b"before original"
        )
    monkeypatch.setattr(ab, "_encode", encode)
    monkeypatch.setattr(ab, "_probe", lambda path: (
        3.0 if path.name in ("before.mp4", "after.mp4") else 4.0,
        True, True,
    ))
    monkeypatch.setattr(ab.shutil, "which", lambda name: name)
    return encoded


def test_flagged_boundary_is_sampled_without_changing_original(tmp_path, monkeypatch):
    params, files = _case(tmp_path, flagged=True)
    encoded = _fake_encoder(monkeypatch)
    original_bytes = [p.read_bytes() for p in files]
    result = ab.create_cut_seam_ab_preview(**params)
    assert result["status"] == "LOCAL_CUT_AB_CREATED_UNVERIFIED_EDITORIAL"
    assert result["variant_count"] == 2
    assert result["seam_seconds"] == 2
    assert result["baseline_recovery_verified"] is True
    assert encoded == [(False, 0.5, 3, 1.5), (True, 0.5, 3, 1.5)]
    out = files[0].with_name(f"{files[0].stem}.cut-ab")
    receipt = (out / "receipt.json").read_bytes()
    assert hashlib.sha256(receipt).hexdigest() == result["sha256"]
    proof = json.loads(receipt)
    assert proof["left_segment_id"] == "left"
    assert proof["right_segment_id"] == "right"
    assert proof["selection"] == "REVIEW_FLAGGED_CUT_BOUNDARY"
    assert proof["video_cut_times_unchanged"] is True
    assert proof["audio_cut_times_unchanged"] is True
    assert proof["audio_effect_applied"] is False
    assert proof["visual_improvement_confirmed"] is False
    assert proof["editing_correction_performed"] is False
    assert proof["ready"] is False
    assert proof["baseline_recovery"]["sha256"] == params["video_sha256"]
    assert proof["before"]["sha256"] != proof["after"]["sha256"]
    for sample in ("before", "after"):
        assert hashlib.sha256(
            (out / f"{sample}.mp4").read_bytes()
        ).hexdigest() == proof[sample]["sha256"]
    assert [p.read_bytes() for p in files] == original_bytes
    assert str(tmp_path) not in receipt.decode("utf-8")
    assert ab.create_cut_seam_ab_preview(**params)["status"] == (
        "LOCAL_CUT_AB_UNAVAILABLE"
    )
    assert (out / "receipt.json").read_bytes() == receipt


@pytest.mark.parametrize("kwargs", [
    {"one_cut": True},
    {"existing_dip": True},
])
def test_no_eligible_seam_is_explicitly_not_applicable(
    tmp_path, monkeypatch, kwargs,
):
    params, files = _case(tmp_path, **kwargs)
    monkeypatch.setattr(ab, "_encode", lambda *a, **k:
                        pytest.fail("No eligible seam: do not encode"))
    r = ab.create_cut_seam_ab_preview(**params)
    assert r["status"] == "LOCAL_CUT_AB_NOT_APPLICABLE"
    assert not files[0].with_name(f"{files[0].stem}.cut-ab").exists()


@pytest.mark.parametrize("change", [
    "base", "triage", "review", "sha", "preexisting", "disabled",
    "fake_ready", "fake_auto_repair", "malformed_index",
])
def test_unsafe_inputs_never_publish_a_cut_preview(
    tmp_path, monkeypatch, change,
):
    params, (base, source, review, triage) = _case(tmp_path, flagged=True)
    _fake_encoder(monkeypatch)
    dest = base.with_name(f"{base.stem}.cut-ab")
    if change == "base":
        base.write_bytes(b"changed bytes")
    elif change == "triage":
        triage.write_text(triage.read_text(encoding="utf-8") + "tampered",
                          encoding="utf-8")
    elif change == "review":
        review.write_text(review.read_text(encoding="utf-8") + "tampered",
                          encoding="utf-8")
    elif change == "sha":
        params["video_sha256"] = "f" * 64
    elif change == "preexisting":
        dest.mkdir()
        (dest / "operator.txt").write_text("do not erase", encoding="utf-8")
    elif change == "disabled":
        params["scan_allowed"] = False
    elif change in {"fake_ready", "fake_auto_repair", "malformed_index"}:
        doc = json.loads(triage.read_text(encoding="utf-8"))
        if change == "fake_ready":
            doc["ready"] = True
        elif change == "fake_auto_repair":
            doc["candidate_queue"][0]["repair_draft"]["operation_authorized"] = True
        else:
            doc["candidate_queue"][0]["review_index"] = 10000
        triage.write_text(json.dumps(doc), encoding="utf-8")
        params["triage_sha256"] = hashlib.sha256(triage.read_bytes()).hexdigest()
    r = ab.create_cut_seam_ab_preview(**params)
    assert r["status"] == "LOCAL_CUT_AB_UNAVAILABLE"
    if change == "preexisting":
        assert (dest / "operator.txt").read_text(encoding="utf-8") == "do not erase"
    else:
        assert not dest.exists()


def test_cut_seam_uses_fixed_ffmpeg_video_filter_and_no_audio_edits(
    tmp_path, monkeypatch,
):
    captured = []
    out = tmp_path / "experiment.mp4"
    def run(args, **kw):
        captured.append((args, kw))
        out.write_bytes(b"synthetic ffmpeg output")
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(ab.subprocess, "run", run)
    ab._encode(
        "ffmpeg", tmp_path / "source.mp4", out,
        begin=10.5, seconds=3.0, relative_seam=1.5, experimental=True,
    )
    argv, kw = captured[0]
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    assert argv[argv.index("-ss") + 1] == "10.500"
    assert argv[argv.index("-t") + 1] == "3.000"
    vf = argv[argv.index("-vf") + 1]
    assert "drawbox" in vf and "between(t,1.420,1.580)" in vf
    assert not any(x in argv for x in ("-af", "-filter_complex", "-y"))
    assert "-n" in argv and "-nostdin" in argv
    assert kw["timeout"] == 35.0 and kw["stdin"] == subprocess.DEVNULL


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="real FFmpeg and ffprobe required",
)
def test_real_ffmpeg_two_distinct_scenes_comparison_keeps_baseline_hash(tmp_path):
    args, files = _case(tmp_path)
    ffmpeg = shutil.which("ffmpeg")
    source = tmp_path / "synthetic-scenes.mp4"
    run = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=24:d=2",
        "-f", "lavfi", "-i", "sine=frequency=430:duration=2",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=2",
        "-f", "lavfi", "-i", "sine=frequency=700:duration=2",
        "-filter_complex",
        "[0:v:0][1:a:0][2:v:0][3:a:0]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], capture_output=True, timeout=40, check=False)
    assert run.returncode == 0
    files[0].write_bytes(source.read_bytes())
    digest = hashlib.sha256(files[0].read_bytes()).hexdigest()
    params = args
    params["video_sha256"] = digest
    review = json.loads(files[2].read_text(encoding="utf-8"))
    review["base_sha256"] = review["final_sha256"] = digest
    files[2].write_text(json.dumps(review), encoding="utf-8")
    triage = json.loads(files[3].read_text(encoding="utf-8"))
    triage["base_sha256_from_review"] = digest
    triage["final_sha256_from_review"] = digest
    triage["review_sha256"] = hashlib.sha256(files[2].read_bytes()).hexdigest()
    files[3].write_text(json.dumps(triage), encoding="utf-8")
    params["triage_sha256"] = hashlib.sha256(files[3].read_bytes()).hexdigest()
    result = ab.create_cut_seam_ab_preview(**params)
    assert result["status"] == "LOCAL_CUT_AB_CREATED_UNVERIFIED_EDITORIAL"
    assert hashlib.sha256(files[0].read_bytes()).hexdigest() == digest
    dest = files[0].with_name(f"{files[0].stem}.cut-ab")
    assert (dest / "before.mp4").is_file()
    assert (dest / "after.mp4").is_file()
    assert (dest / "receipt.json").is_file()
