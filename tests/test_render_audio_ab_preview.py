"""Unmodified baseline, bounded audio-only A/B experiment and fail-closed receipts."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from holoclip import render_audio_ab_preview as ab
from holoclip.render_review_triage import TRIAGE_VERSION


def _case(tmp_path, *, with_peak=True):
    base = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    base.write_bytes(b"immutable original, not a real mp4")
    final = base.with_name(f"{base.stem}.captioned.mp4")
    final.write_bytes(b"some other previously produced version")
    base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
    final_sha = hashlib.sha256(final.read_bytes()).hexdigest()
    rows = [{
        "review_index": 1,
        "kind": "audio_peak",
        "start_seconds": 0.0,
        "end_seconds": 4.0,
        "auto_repair_allowed": False,
        "repair_draft": {
            "kind": "NON_EXECUTABLE_INSPECTION_PROPOSAL",
            "operation_authorized": False,
        },
    }] if with_peak else [{
        "review_index": 0,
        "kind": "blackout",
        "start_seconds": 1.0,
        "end_seconds": 2.5,
        "auto_repair_allowed": False,
    }]
    triage = {
        "version": TRIAGE_VERSION,
        "base_sha256_from_review": base_sha,
        "final_sha256_from_review": final_sha,
        "candidate_queue": rows,
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False, "repair_execution_performed": False,
        "publication_performed": False,
    }
    source = base.with_name(f"{base.stem}.triage.json")
    source.write_text(json.dumps(triage), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    params = {
        "mp4_output": base,
        "video_sha256": base_sha,
        "final_output": final,
        "final_sha256": final_sha,
        "triage_sha256": digest,
        "scan_allowed": True,
    }
    return params, source


def _successful_media(monkeypatch, *, changed=False):
    events = []

    def render(ffmpeg, source, target, seconds, *, change_audio):
        events.append(("after" if change_audio else "before", source, seconds))
        target.write_bytes(
            (b"encoded, validated sample after" if change_audio
             else b"encoded, validated sample before")
        )

    def measure(ffmpeg, path, start, end):
        if "after" in path.name:
            return (-21.5, -1.7)
        return (-20.0, -0.1)

    monkeypatch.setattr(ab, "_render_sample", render)
    monkeypatch.setattr(ab, "_measure", measure)
    monkeypatch.setattr(ab, "_probe", lambda path: (4.0, True, True))
    monkeypatch.setattr(ab.shutil, "which", lambda exe: exe)
    return events


def test_create_two_immutable_ab_clips_with_fallback_to_original(tmp_path, monkeypatch):
    params, source = _case(tmp_path)
    events = _successful_media(monkeypatch)
    unchanged = (
        params["mp4_output"].read_bytes(),
        params["final_output"].read_bytes(),
        source.read_bytes(),
    )
    outcome = ab.create_audio_ab_preview(**params)
    assert outcome["status"] == "LOCAL_AB_PREVIEW_CREATED_UNVERIFIED_EDITORIAL"
    assert outcome["sample_count"] == 1
    assert outcome["baseline_recovery_verified"] is True
    assert outcome["auto_repair_allowed"] is False
    assert events == [
        ("before", params["final_output"], 4.0),
        ("after", params["final_output"], 4.0),
    ]
    dirname = params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.ab-preview"
    )
    assert dirname.is_dir()
    receipt_bytes = (dirname / "receipt.json").read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest() == outcome["sha256"]
    data = json.loads(receipt_bytes)
    assert data["baseline_recovery"] == {
        "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
        "sha256": params["video_sha256"],
        "baseline_reverified_after_preview": True,
        "files_replaced_or_deleted": False,
    }
    assert data["alternative_is_test_only"] is True
    assert data["clipped_source_audio_restored"] is False
    assert data["repair_execution_performed"] is False
    assert data["publication_performed"] is False
    assert data["ready"] is False
    assert data["before"]["peak_dbfs"] == -0.1
    assert data["after"]["peak_dbfs"] == -1.7
    assert data["trial_audio_gain_db"] == -1.5
    assert hashlib.sha256((dirname / "before.mp4").read_bytes()).hexdigest() == (
        data["before"]["sha256"]
    )
    assert hashlib.sha256((dirname / "after.mp4").read_bytes()).hexdigest() == (
        data["after"]["sha256"]
    )
    assert unchanged == (
        params["mp4_output"].read_bytes(),
        params["final_output"].read_bytes(),
        source.read_bytes(),
    )
    repeat = ab.create_audio_ab_preview(**params)
    assert repeat["status"] == "LOCAL_AB_PREVIEW_UNAVAILABLE"
    assert (dirname / "receipt.json").read_bytes() == receipt_bytes


def test_unrelated_warnings_never_generate_a_fake_repair(tmp_path, monkeypatch):
    params, source = _case(tmp_path, with_peak=False)
    monkeypatch.setattr(ab, "_render_sample", lambda *args, **kw:
                        pytest.fail("No peak signal: A/B encoder forbidden"))
    report = ab.create_audio_ab_preview(**params)
    assert report["status"] == "LOCAL_AB_PREVIEW_NOT_APPLICABLE"
    assert report["sample_count"] == 0
    assert not params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.ab-preview"
    ).exists()


@pytest.mark.parametrize("flaw", [
    "bad_sha", "wrong_base_hash", "wrong_final_hash", "tamper_triage",
    "output_preexists", "scan_not_allowed", "unauthorized_repair",
    "invalid_signal_clock",
])
def test_all_unsafe_contexts_block_ab_generation(tmp_path, monkeypatch, flaw):
    params, source = _case(tmp_path)
    _successful_media(monkeypatch)
    if flaw == "bad_sha":
        params["video_sha256"] = "0" * 64
    elif flaw == "wrong_base_hash":
        params["mp4_output"].write_bytes(b"replaced baseline bytes")
    elif flaw == "wrong_final_hash":
        params["final_output"].write_bytes(b"replaced final bytes")
    elif flaw == "tamper_triage":
        source.write_text(source.read_text() + " garbage")
    elif flaw == "output_preexists":
        destination = params["mp4_output"].with_name(
            f"{params['mp4_output'].stem}.ab-preview"
        )
        destination.mkdir()
        (destination / "manual.txt").write_text("keep operator notes")
    elif flaw == "scan_not_allowed":
        params["scan_allowed"] = False
    elif flaw == "unauthorized_repair":
        doc = json.loads(source.read_text())
        doc["candidate_queue"][0]["repair_draft"]["operation_authorized"] = True
        source.write_text(json.dumps(doc))
        params["triage_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    elif flaw == "invalid_signal_clock":
        doc = json.loads(source.read_text())
        doc["candidate_queue"][0]["end_seconds"] = 50.0
        source.write_text(json.dumps(doc))
        params["triage_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    report = ab.create_audio_ab_preview(**params)
    assert report["status"] == "LOCAL_AB_PREVIEW_UNAVAILABLE"
    dest = params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.ab-preview"
    )
    if flaw == "output_preexists":
        assert (dest / "manual.txt").read_text() == "keep operator notes"
    else:
        assert not dest.exists()


def test_failed_audio_qa_never_produces_recovery_or_edited_full_mp4(tmp_path, monkeypatch):
    params, source = _case(tmp_path)
    _successful_media(monkeypatch)
    monkeypatch.setattr(
        ab, "_measure", lambda ffmpeg, path, start, end: (-20.0, -0.1),
    )
    report = ab.create_audio_ab_preview(**params)
    assert report["status"] == "LOCAL_AB_PREVIEW_UNAVAILABLE"
    assert report["baseline_recovery_verified"] is False
    assert not params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.ab-preview"
    ).exists()


def test_ffmpeg_sample_command_is_fixed_and_local_only(tmp_path, monkeypatch):
    captured = []
    sample = tmp_path / "test.mp4"
    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        sample.write_bytes(b"ffmpeg fake output")
        return type("Process", (), {"returncode": 0})()
    monkeypatch.setattr(ab.subprocess, "run", run)
    ab._render_sample(
        "ffmpeg", tmp_path / "original.mp4", sample, 3.75,
        change_audio=True,
    )
    cmd, kwargs = captured[0]
    assert "-nostdin" in cmd and "-n" in cmd
    assert "-protocol_whitelist" in cmd
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
    assert cmd[cmd.index("-af") + 1] == "volume=-1.5dB"
    assert cmd[cmd.index("-t") + 1] == "3.750"
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert kwargs["timeout"] == 35.0
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "https" not in " ".join(cmd)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe required",
)
def test_real_synthetic_audio_ab_measurement_keeps_baseline_hash(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    base = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=4",
        "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=4",
        "-c:v", "mpeg4", "-q:v", "7", "-c:a", "aac", "-shortest",
        str(base),
    ]
    proc = subprocess.run(
        cmd, check=False, capture_output=True, timeout=40,
    )
    assert proc.returncode == 0
    digest = hashlib.sha256(base.read_bytes()).hexdigest()
    triage_doc = {
        "version": TRIAGE_VERSION,
        "base_sha256_from_review": digest,
        "final_sha256_from_review": digest,
        "candidate_queue": [{
            "review_index": 0, "kind": "audio_peak",
            "start_seconds": 0, "end_seconds": 4.0,
            "auto_repair_allowed": False,
            "repair_draft": {
                "kind": "NON_EXECUTABLE_INSPECTION_PROPOSAL",
                "operation_authorized": False,
            },
        }],
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False,
        "repair_execution_performed": False,
        "publication_performed": False,
    }
    sidecar = base.with_name(f"{base.stem}.triage.json")
    sidecar.write_text(json.dumps(triage_doc), encoding="utf-8")
    report = ab.create_audio_ab_preview(
        mp4_output=base, video_sha256=digest,
        final_output=base, final_sha256=digest,
        triage_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        scan_allowed=True,
    )
    assert report["status"] == "LOCAL_AB_PREVIEW_CREATED_UNVERIFIED_EDITORIAL"
    assert hashlib.sha256(base.read_bytes()).hexdigest() == digest
    clips = base.with_name(f"{base.stem}.ab-preview")
    assert (clips / "before.mp4").exists()
    assert (clips / "after.mp4").exists()
    proof = json.loads((clips / "receipt.json").read_text())
    assert proof["after"]["peak_dbfs"] < proof["before"]["peak_dbfs"] - 0.7
    assert proof["alternative_is_test_only"] is True
