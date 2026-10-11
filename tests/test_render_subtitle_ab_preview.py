"""Offline subtitle A/B is create-only, hash-bound, and never a timing repair."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from holoclip import render_subtitle_ab_preview as ab
from holoclip.render_review_triage import TRIAGE_VERSION
from holoclip.render_quality_review import REVIEW_VERSION
from holoclip.render_subtitle_burnin import _japanese_font


def _case(tmp_path, *, kind="fast_subtitle"):
    base = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    base.write_bytes(b"original QA-verified local preview, unchanged")
    leveled = base.with_name(f"{base.stem}.leveled.mp4")
    leveled.write_bytes(b"separate existing audio preview, unchanged")
    sha_base = hashlib.sha256(base.read_bytes()).hexdigest()
    sha_input = hashlib.sha256(leveled.read_bytes()).hexdigest()
    text = "とても読みづらいほど表示時間の短い字幕です" if kind == "fast_subtitle" else "普通の字幕"
    srt = base.with_suffix(".srt")
    srt.write_text(
        f"1\n00:00:00,500 --> 00:00:01,000\n{text}\n", encoding="utf-8",
    )
    srt_sha = hashlib.sha256(srt.read_bytes()).hexdigest()
    candidate = {
        "kind": kind, "start_seconds": 0.5, "end_seconds": 1.0,
        "segment_ids": ["cut1"], "position": "inside_cut",
        "cue_number": 1, "auto_repair_allowed": False,
        "human_editorial_qa_completed": False,
    }
    review = {
        "version": REVIEW_VERSION,
        "candidate_count": 1, "candidates": [candidate],
        "base_sha256": sha_base, "final_sha256": sha_input,
        "ready": False, "production_ready": False,
        "human_editorial_qa_completed": False,
        "publication_performed": False,
    }
    review_path = base.with_name(f"{base.stem}.review.json")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
    triage = {
        "version": TRIAGE_VERSION,
        "review_sha256": review_sha,
        "base_sha256_from_review": sha_base,
        "final_sha256_from_review": sha_input,
        "candidate_count": 1,
        "candidate_queue": [{
            "review_index": 0, "kind": kind,
            "start_seconds": 0.5, "end_seconds": 1.0,
            "segment_ids": ["cut1"],
            "auto_repair_allowed": False,
            "repair_draft": {
                "kind": "NON_EXECUTABLE_INSPECTION_PROPOSAL",
                "operation_authorized": False,
            },
        }],
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False, "publication_performed": False,
        "repair_execution_performed": False,
    }
    triage_path = base.with_name(f"{base.stem}.triage.json")
    triage_path.write_text(json.dumps(triage), encoding="utf-8")
    triage_sha = hashlib.sha256(triage_path.read_bytes()).hexdigest()
    params = dict(
        mp4_output=base, video_sha256=sha_base,
        input_video=leveled, input_sha256=sha_input,
        final_sha256=sha_input, subtitle_sha256=srt_sha,
        triage_sha256=triage_sha, scan_allowed=True,
    )
    return params, (base, leveled, srt, review_path, triage_path)


def _mock_video(monkeypatch, tmp_path):
    calls = []
    font = tmp_path / "font.ttf"
    font.write_bytes(b"test-font-does-not-get-read-by-mocked-encoder")
    monkeypatch.setattr(ab, "_japanese_font", lambda: (font, "Meiryo"))

    def encoder(ffmpeg, video, target, *, start, seconds, work, enhanced):
        calls.append((enhanced, video, start, seconds))
        assert (work / "baseline.ass").is_file()
        assert (work / "enhanced.ass").is_file()
        target.write_bytes(b"EXPERIMENT" if enhanced else b"BASELINE")

    monkeypatch.setattr(ab, "_encode", encoder)
    monkeypatch.setattr(ab, "_probe", lambda video: (
        (1.3 if video.name in {"before.mp4", "after.mp4"} else 4.0),
        True, True,
    ))
    monkeypatch.setattr(ab.shutil, "which", lambda app: app)
    return calls


def test_a_b_visual_trial_pins_original_and_caption_bytes_without_autoedit(
    tmp_path, monkeypatch,
):
    params, files = _case(tmp_path)
    calls = _mock_video(monkeypatch, tmp_path)
    originals = [path.read_bytes() for path in files]
    result = ab.create_subtitle_ab_preview(**params)
    assert result["status"] == "LOCAL_SUBTITLE_AB_CREATED_UNVERIFIED_EDITORIAL"
    assert result["variant_count"] == 2
    assert result["cue_number"] == 1
    assert result["baseline_recovery_verified"] is True
    assert calls == [
        (False, params["input_video"], 0.1, pytest.approx(1.3)),
        (True, params["input_video"], 0.1, pytest.approx(1.3)),
    ]
    out = params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.subtitle-ab"
    )
    receipt_bytes = (out / "receipt.json").read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest() == result["sha256"]
    document = json.loads(receipt_bytes)
    assert document["cue_number"] == 1
    assert document["text_and_timing_unchanged_between_variants"] is True
    assert document["visual_readability_improvement_confirmed"] is False
    assert document["fast_reading_time_corrected"] is False
    assert document["repair_execution_performed"] is False
    assert document["ready"] is False
    assert document["baseline_recovery"] == {
        "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
        "sha256": params["video_sha256"],
        "baseline_reverified_after_preview": True,
        "files_replaced_or_deleted": False,
    }
    assert document["before"]["sha256"] != document["after"]["sha256"]
    assert [f.read_bytes() for f in files] == originals
    assert str(tmp_path) not in receipt_bytes.decode("utf-8")
    assert ab.create_subtitle_ab_preview(**params)["status"] == (
        "LOCAL_SUBTITLE_AB_UNAVAILABLE"
    )
    assert (out / "receipt.json").read_bytes() == receipt_bytes


def test_non_fast_warning_never_produces_experimental_subtitle(
    tmp_path, monkeypatch,
):
    params, files = _case(tmp_path, kind="blackout")
    monkeypatch.setattr(ab, "_encode", lambda *a, **k:
                        pytest.fail("No matching fast subtitle; do not encode"))
    result = ab.create_subtitle_ab_preview(**params)
    assert result["status"] == "LOCAL_SUBTITLE_AB_NOT_APPLICABLE"
    assert not params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.subtitle-ab"
    ).exists()


@pytest.mark.parametrize("corrupt", [
    "base", "selected_input", "srt", "triage", "review", "wrong_final",
    "wrong_cue_number", "rewritten_start", "repair_allowed", "output_exists",
    "scan_forbidden", "oversized_subtitle",
])
def test_source_changes_tampering_and_existing_output_fail_closed(
    tmp_path, monkeypatch, corrupt,
):
    params, (base, leveled, srt, review, triage) = _case(tmp_path)
    _mock_video(monkeypatch, tmp_path)
    destination = base.with_name(f"{base.stem}.subtitle-ab")
    if corrupt == "base":
        base.write_bytes(b"altered original")
    elif corrupt == "selected_input":
        leveled.write_bytes(b"altered input")
    elif corrupt == "srt":
        srt.write_text(srt.read_text() + " new content", encoding="utf-8")
    elif corrupt == "triage":
        triage.write_text(triage.read_text() + "tamper", encoding="utf-8")
    elif corrupt == "review":
        review.write_text(review.read_text() + "tamper", encoding="utf-8")
    elif corrupt == "wrong_final":
        params["final_sha256"] = "0" * 64
    elif corrupt == "wrong_cue_number":
        data = json.loads(review.read_text())
        data["candidates"][0]["cue_number"] = 2
        review.write_text(json.dumps(data), encoding="utf-8")
        # Even a matching new triage receipt cannot bypass old review hash.
    elif corrupt == "rewritten_start":
        data = json.loads(triage.read_text())
        data["candidate_queue"][0]["start_seconds"] = 8.0
        triage.write_text(json.dumps(data), encoding="utf-8")
        params["triage_sha256"] = hashlib.sha256(triage.read_bytes()).hexdigest()
    elif corrupt == "repair_allowed":
        data = json.loads(triage.read_text())
        data["candidate_queue"][0]["repair_draft"]["operation_authorized"] = True
        triage.write_text(json.dumps(data), encoding="utf-8")
        params["triage_sha256"] = hashlib.sha256(triage.read_bytes()).hexdigest()
    elif corrupt == "output_exists":
        destination.mkdir()
        (destination / "operator.txt").write_text("protect")
    elif corrupt == "scan_forbidden":
        params["scan_allowed"] = False
    elif corrupt == "oversized_subtitle":
        srt.write_bytes(b"a" * 131073)
    outcome = ab.create_subtitle_ab_preview(**params)
    assert outcome["status"] == "LOCAL_SUBTITLE_AB_UNAVAILABLE"
    if corrupt == "output_exists":
        assert (destination / "operator.txt").read_text() == "protect"
    else:
        assert not destination.exists()


def test_style_variant_changes_only_visual_style_and_not_caption_clock():
    from holoclip.render_subtitle_burnin import Caption
    c = Caption(0.4, 1.2, "聞き取りにくい字幕")
    before = ab._style_ass(c, font_name="Meiryo", enhanced=False)
    after = ab._style_ass(c, font_name="Meiryo", enhanced=True)
    assert "Style: JP,Meiryo,43" in before
    assert "Style: JP,Meiryo,48" in after
    assert "0:00:00.40,0:00:01.20" in before
    assert "0:00:00.40,0:00:01.20" in after
    assert "聞き取りにくい字幕" in before and "聞き取りにくい字幕" in after
    assert before != after


def test_subtitle_ab_encoder_uses_local_fixed_filter_no_user_shell(
    tmp_path, monkeypatch,
):
    saved = []
    def run(argv, **kwargs):
        saved.append((argv, kwargs))
        (tmp_path / "sample.mp4").write_bytes(b"simulated video")
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(ab.subprocess, "run", run)
    ab._encode(
        "ffmpeg", tmp_path / "in.mp4", tmp_path / "sample.mp4",
        start=7.25, seconds=2.5, work=tmp_path, enhanced=True,
    )
    command, opts = saved[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert command[command.index("-ss") + 1] == "7.250"
    assert command[command.index("-t") + 1] == "2.500"
    assert "enhanced.ass" in command[command.index("-vf") + 1]
    assert "-n" in command and "-nostdin" in command
    assert opts["timeout"] == 35.0 and opts["cwd"] == tmp_path
    assert "-map_metadata" in command and "-y" not in command


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe")
    or _japanese_font() is None,
    reason="FFmpeg + installed CJK font required",
)
def test_real_synthetic_subtitle_ab_ffmpeg_and_unmodified_original(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    params, files = _case(tmp_path)
    # Both baseline and leveled input must be real synthetic MP4s.
    source = tmp_path / "seed.mp4"
    res = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=4",
        "-f", "lavfi", "-i", "sine=frequency=430:sample_rate=48000:duration=4",
        "-c:v", "mpeg4", "-q:v", "7", "-c:a", "aac", "-shortest",
        str(source),
    ], check=False, capture_output=True, timeout=50)
    assert res.returncode == 0
    params["mp4_output"].write_bytes(source.read_bytes())
    params["input_video"].write_bytes(source.read_bytes())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    params["video_sha256"] = params["input_sha256"] = params["final_sha256"] = digest
    review = files[3]
    body = json.loads(review.read_text())
    body["base_sha256"] = body["final_sha256"] = digest
    review.write_text(json.dumps(body), encoding="utf-8")
    triage = files[4]
    meta = json.loads(triage.read_text())
    meta["base_sha256_from_review"] = meta["final_sha256_from_review"] = digest
    meta["review_sha256"] = hashlib.sha256(review.read_bytes()).hexdigest()
    triage.write_text(json.dumps(meta), encoding="utf-8")
    params["triage_sha256"] = hashlib.sha256(triage.read_bytes()).hexdigest()
    result = ab.create_subtitle_ab_preview(**params)
    assert result["status"] == "LOCAL_SUBTITLE_AB_CREATED_UNVERIFIED_EDITORIAL"
    out = params["mp4_output"].with_name(f"{params['mp4_output'].stem}.subtitle-ab")
    assert (out / "before.mp4").is_file()
    assert (out / "after.mp4").is_file()
    assert (out / "receipt.json").is_file()
    assert hashlib.sha256(params["mp4_output"].read_bytes()).hexdigest() == digest
