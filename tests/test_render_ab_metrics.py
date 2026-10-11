"""Independent A/B signal metrics never qualify creative or publishing quality."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from holoclip import render_ab_metrics as metrics


def _case(tmp_path, *, kinds=("audio", "subtitle", "cut")):
    base = tmp_path / ("renderqueue_" + "a" * 32 + ".mp4")
    base.write_bytes(b"existing full-length original preview")
    digest = hashlib.sha256(base.read_bytes()).hexdigest()
    triage = base.with_name(f"{base.stem}.triage.json")
    triage.write_text(json.dumps({
        "base_sha256_from_review": digest,
        "ready": False, "auto_repair_allowed": False,
    }), encoding="utf-8")
    triage_sha = hashlib.sha256(triage.read_bytes()).hexdigest()
    supplied = {kind: "" for kind in metrics._KINDS}
    originals = [base, triage]
    for kind in kinds:
        extension, version, seconds = metrics._KINDS[kind]
        folder = base.with_name(f"{base.stem}{extension}")
        folder.mkdir()
        clips = []
        for name in ("before", "after"):
            clip = folder / f"{name}.mp4"
            clip.write_bytes(f"{kind}: {name}".encode())
            clips.append(clip)
        receipt = {
            "version": version,
            "base_sha256": digest, "triage_sha256": triage_sha,
            "baseline_recovery": {
                "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
                "sha256": digest,
                "baseline_reverified_after_preview": True,
                "files_replaced_or_deleted": False,
            },
            "ready": False, "production_ready": False,
            "auto_repair_allowed": False, "publication_performed": False,
            "preview_experiment_generated": True,
            "before": {
                "filename": "before.mp4",
                "sha256": hashlib.sha256(clips[0].read_bytes()).hexdigest(),
            },
            "after": {
                "filename": "after.mp4",
                "sha256": hashlib.sha256(clips[1].read_bytes()).hexdigest(),
            },
        }
        if kind == "audio":
            receipt.update({
                "sample_duration_seconds": seconds,
                "trial_audio_gain_db": -1.5,
                "repair_execution_performed": False,
            })
            receipt["before"].update({"mean_dbfs": -20.0, "peak_dbfs": -0.2})
            receipt["after"].update({"mean_dbfs": -21.5, "peak_dbfs": -1.6})
        elif kind == "subtitle":
            receipt.update({
                "window_duration_seconds": seconds,
                "text_and_timing_unchanged_between_variants": True,
                "repair_execution_performed": False,
                "human_editorial_qa_completed": False,
            })
        else:
            receipt.update({
                "window_duration_seconds": seconds,
                "video_cut_times_unchanged": True,
                "editing_correction_performed": False,
                "human_editorial_qa_completed": False,
            })
        path = folder / "receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        supplied[kind] = hashlib.sha256(path.read_bytes()).hexdigest()
        originals.extend([*clips, path])
    params = {
        "mp4_output": base, "video_sha256": digest,
        "triage_sha256": triage_sha,
        "audio_sha256": supplied["audio"],
        "subtitle_sha256": supplied["subtitle"],
        "cut_sha256": supplied["cut"],
        "scan_allowed": True,
    }
    return params, originals


def _measured(monkeypatch):
    calls = []

    def probe(path):
        return 4.0 if path.parent.name.endswith(".ab-preview") else (
            3.0 if path.parent.name.endswith(".cut-ab") else 4.0
        ), True, True

    def measure(ffmpeg, media, start, end):
        calls.append((media.parent.name, media.name))
        if media.parent.name.endswith(".ab-preview"):
            return (-21.5, -1.6) if media.name == "after.mp4" else (-20, -0.2)
        return (-20.0, -1.0)

    monkeypatch.setattr(metrics, "_probe", probe)
    monkeypatch.setattr(metrics, "_ssim", lambda *args: 0.91)
    monkeypatch.setattr(metrics, "_measure", measure)
    monkeypatch.setattr(metrics.shutil, "which", lambda name: name)
    return calls


def test_all_three_types_produce_objective_non_editorial_evidence(tmp_path, monkeypatch):
    params, files = _case(tmp_path)
    original_bytes = [p.read_bytes() for p in files]
    calls = _measured(monkeypatch)
    status = metrics.evaluate_independent_ab_signals(**params)
    assert status["status"] == "LOCAL_AB_AUDIT_MEASURED_TECHNICAL_ONLY"
    assert status["measured_pair_count"] == 3
    assert status["human_quality_qualified"] is False
    assert status["auto_adoption_allowed"] is False
    dest = params["mp4_output"].with_name(f"{params['mp4_output'].stem}.ab-metrics.json")
    contents = dest.read_bytes()
    assert hashlib.sha256(contents).hexdigest() == status["sha256"]
    document = json.loads(contents)
    assert [x["kind"] for x in document["comparisons"]] == [
        "audio", "subtitle", "cut",
    ]
    assert document["all_sample_shas_reverified"] is True
    assert document["original_mp4_independently_rehashed"] is False
    assert document["creative_quality_ground_truth_available"] is False
    assert document["human_quality_qualified"] is False
    assert document["auto_adoption_allowed"] is False
    assert document["ready"] is False and document["production_ready"] is False
    assert document["comparisons"][0]["after_minus_before_mean_db"] == -1.5
    assert document["comparisons"][0]["decoded_video_ssim"] == 0.91
    assert len(calls) == 6
    assert [p.read_bytes() for p in files] == original_bytes
    assert str(tmp_path) not in contents.decode()
    assert metrics.evaluate_independent_ab_signals(**params)["status"] == (
        "LOCAL_AB_AUDIT_UNAVAILABLE"
    )
    assert dest.read_bytes() == contents


def test_missing_pairs_are_explicitly_not_applicable(tmp_path, monkeypatch):
    params, files = _case(tmp_path, kinds=())
    monkeypatch.setattr(metrics, "_ssim", lambda *a:
                        pytest.fail("No existing A/B: no SSIM scan"))
    result = metrics.evaluate_independent_ab_signals(**params)
    assert result["status"] == "LOCAL_AB_AUDIT_NOT_APPLICABLE"
    assert result["measured_pair_count"] == 0


@pytest.mark.parametrize("alter", [
    "base_hash", "triage", "receipt", "sample", "wrong_meta",
    "unsafe_publish", "auto_repair", "wrong_version",
    "same_variant", "existing_output", "unallowed",
])
def test_modified_evidence_is_not_measured_or_published(tmp_path, monkeypatch, alter):
    params, files = _case(tmp_path, kinds=("audio",))
    _measured(monkeypatch)
    folder = params["mp4_output"].with_name(f"{params['mp4_output'].stem}.ab-preview")
    proof = folder / "receipt.json"
    if alter == "base_hash":
        params["video_sha256"] = "f" * 64
    elif alter == "triage":
        files[1].write_text(files[1].read_text(encoding="utf-8") + "bad",
                            encoding="utf-8")
    elif alter == "receipt":
        proof.write_text(proof.read_text(encoding="utf-8") + "bad", encoding="utf-8")
    elif alter == "sample":
        (folder / "before.mp4").write_bytes(b"replaced samples")
    elif alter in ("wrong_meta", "unsafe_publish", "auto_repair",
                   "wrong_version", "same_variant"):
        obj = json.loads(proof.read_text(encoding="utf-8"))
        if alter == "wrong_meta":
            obj["triage_sha256"] = "1" * 64
        elif alter == "unsafe_publish":
            obj["publication_performed"] = True
        elif alter == "auto_repair":
            obj["repair_execution_performed"] = True
        elif alter == "wrong_version":
            obj["version"] = "unknown"
        elif alter == "same_variant":
            obj["after"]["sha256"] = obj["before"]["sha256"]
        proof.write_text(json.dumps(obj), encoding="utf-8")
        params["audio_sha256"] = hashlib.sha256(proof.read_bytes()).hexdigest()
    elif alter == "existing_output":
        dest = params["mp4_output"].with_name(f"{params['mp4_output'].stem}.ab-metrics.json")
        dest.write_bytes(b"operator existing notes")
    else:
        params["scan_allowed"] = False
    status = metrics.evaluate_independent_ab_signals(**params)
    assert status["status"] == "LOCAL_AB_AUDIT_UNAVAILABLE"
    dest = params["mp4_output"].with_name(f"{params['mp4_output'].stem}.ab-metrics.json")
    if alter == "existing_output":
        assert dest.read_bytes() == b"operator existing notes"
    else:
        assert not dest.exists()


def test_audio_change_must_be_independently_confirmed(tmp_path, monkeypatch):
    params, files = _case(tmp_path, kinds=("audio",))
    _measured(monkeypatch)
    monkeypatch.setattr(metrics, "_measure", lambda *a, **k: (-20.0, -0.2))
    report = metrics.evaluate_independent_ab_signals(**params)
    assert report["status"] == "LOCAL_AB_AUDIT_UNAVAILABLE"
    assert not params["mp4_output"].with_name(
        f"{params['mp4_output'].stem}.ab-metrics.json"
    ).exists()


def test_video_ssim_scan_fixed_local_protocol_with_timeout(tmp_path, monkeypatch):
    intercepted = []
    def simulate(cmd, **kwargs):
        intercepted.append((cmd, kwargs))
        return type("Result", (), {
            "returncode": 0,
            "stderr": b"[Parsed_ssim_0] SSIM Y:0.90 U:1.0 V:0.90 All:0.937500 (12.0)",
        })()
    monkeypatch.setattr(metrics.subprocess, "run", simulate)
    measured = metrics._ssim("ffmpeg", tmp_path / "before.mp4", tmp_path / "after.mp4")
    assert measured == 0.9375
    argv, opts = intercepted[0]
    assert argv.count("-protocol_whitelist") == 2
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    assert argv[argv.index("-filter_complex") + 1] == "[0:v:0][1:v:0]ssim[out]"
    assert "-nostdin" in argv and "-y" not in argv
    assert opts["timeout"] == 25


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe required",
)
def test_real_two_synthetic_mp4s_have_measurable_decoded_ssim(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    clips = []
    for color in ("blue", "red"):
        target = tmp_path / f"{color}.mp4"
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=15:d=2",
            "-f", "lavfi", "-i", "sine=frequency=400:duration=2",
            "-c:v", "mpeg4", "-q:v", "7", "-c:a", "aac",
            "-shortest", str(target),
        ]
        p = subprocess.run(args, capture_output=True, timeout=30, check=False)
        assert p.returncode == 0
        clips.append(target)
    value = metrics._ssim(ffmpeg, clips[0], clips[1])
    assert 0 <= value < 1
