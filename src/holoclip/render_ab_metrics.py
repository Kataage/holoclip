"""Independent, bounded FFmpeg signal audit of already rendered A/B snippets.

Measures signal changes, NOT editorial preference, transcript correctness,
funny moments, human consensus, or proof of successful automatic repair.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from holoclip.render_audio_finish import _measure
from holoclip.render_audio_ab_preview import AB_VERSION
from holoclip.render_subtitle_ab_preview import VERSION as SUBTITLE_AB_VERSION
from holoclip.render_cut_ab_preview import VERSION as CUT_AB_VERSION
from holoclip.render_plan import _hash, _no_links, _probe

VERSION = "holoclip-independent-ab-signal-audit-v1"
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SSIM = re.compile(rb"SSIM Y:[0-9.]+ U:[0-9.]+ V:[0-9.]+ All:([0-9.]+)")
_RECEIPT_LIMIT = 65536
_MAX_SAMPLE_BYTES = 80 * 1024 * 1024
_KINDS = {
    "audio": (".ab-preview", AB_VERSION, 4.0),
    "subtitle": (".subtitle-ab", SUBTITLE_AB_VERSION, 4.0),
    "cut": (".cut-ab", CUT_AB_VERSION, 3.0),
}


def _load_verified(path: Path, digest: str) -> dict:
    path = _no_links(path)
    if (not isinstance(digest, str) or not _SHA.fullmatch(digest)
            or not path.is_file()
            or not 0 < path.stat().st_size <= _RECEIPT_LIMIT):
        raise ValueError("LOCAL_AB_AUDIT_RECEIPT_UNAVAILABLE")
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError("LOCAL_AB_AUDIT_RECEIPT_CHANGED")
    data = json.loads(encoded)
    if not isinstance(data, dict):
        raise ValueError("LOCAL_AB_AUDIT_RECEIPT_INVALID")
    return data


def _ssim(ffmpeg: str, before: Path, after: Path) -> float:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-nostdin",
        "-protocol_whitelist", "file,pipe", "-i", str(before),
        "-protocol_whitelist", "file,pipe", "-i", str(after),
        "-filter_complex", "[0:v:0][1:v:0]ssim[out]",
        "-map", "[out]", "-an", "-f", "null", "-",
    ]
    try:
        output = subprocess.run(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=25.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_AB_AUDIT_VIDEO_MEASURE_FAILED") from exc
    if output.returncode or len(output.stderr) > 65536:
        raise ValueError("LOCAL_AB_AUDIT_VIDEO_MEASURE_FAILED")
    matches = _SSIM.findall(output.stderr)
    if len(matches) != 1:
        raise ValueError("LOCAL_AB_AUDIT_VIDEO_SIGNAL_UNTRUSTED")
    value = float(matches[0])
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("LOCAL_AB_AUDIT_VIDEO_SIGNAL_UNTRUSTED")
    return round(value, 6)


def _audit_one(
    *, base: Path, base_sha: str, triage_sha: str,
    kind: str, expected_sha: str, ffmpeg: str,
) -> dict:
    extension, version, limit = _KINDS[kind]
    directory = _no_links(base.with_name(f"{base.stem}{extension}"))
    receipt = _load_verified(directory / "receipt.json", expected_sha)
    fallback = receipt.get("baseline_recovery")
    if (receipt.get("version") != version
            or receipt.get("base_sha256") != base_sha
            or receipt.get("triage_sha256") != triage_sha
            or not isinstance(fallback, dict)
            or fallback.get("kind") != "USE_UNMODIFIED_ORIGINAL_RENDER"
            or fallback.get("sha256") != base_sha
            or fallback.get("baseline_reverified_after_preview") is not True
            or fallback.get("files_replaced_or_deleted") is not False
            or receipt.get("ready") is not False
            or receipt.get("production_ready") is not False
            or receipt.get("auto_repair_allowed") is not False
            or receipt.get("publication_performed") is not False
            or receipt.get("preview_experiment_generated") is not True
            or kind == "audio" and receipt.get("repair_execution_performed") is not False
            or kind == "subtitle" and receipt.get("repair_execution_performed") is not False
            or kind == "cut" and receipt.get("editing_correction_performed") is not False):
        raise ValueError("LOCAL_AB_AUDIT_LINEAGE_UNTRUSTED")
    if kind == "audio":
        duration = receipt.get("sample_duration_seconds")
        if receipt.get("trial_audio_gain_db") != -1.5:
            raise ValueError("LOCAL_AB_AUDIT_TRIAL_DIFFERENCE_UNTRUSTED")
    else:
        duration = receipt.get("window_duration_seconds")
        if (kind == "cut" and receipt.get("video_cut_times_unchanged") is not True
                or kind == "subtitle" and
                receipt.get("text_and_timing_unchanged_between_variants") is not True):
            raise ValueError("LOCAL_AB_AUDIT_CLOCK_UNTRUSTED")
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0.6 <= duration <= limit):
        raise ValueError("LOCAL_AB_AUDIT_SAMPLE_DURATION_UNTRUSTED")
    media = []
    for name in ("before", "after"):
        sample_meta = receipt.get(name)
        if not isinstance(sample_meta, dict) or sample_meta.get("filename") != f"{name}.mp4":
            raise ValueError("LOCAL_AB_AUDIT_FILE_NAME_UNTRUSTED")
        path = _no_links(directory / f"{name}.mp4")
        if (not path.is_file() or not 0 < path.stat().st_size <= _MAX_SAMPLE_BYTES
                or not isinstance(sample_meta.get("sha256"), str)
                or not _SHA.fullmatch(sample_meta["sha256"])
                or _hash(path)[0] != sample_meta["sha256"]):
            raise ValueError("LOCAL_AB_AUDIT_SAMPLE_CHANGED")
        length, video, audio = _probe(path)
        if not all((video, audio)) or abs(length - duration) > 0.5:
            raise ValueError("LOCAL_AB_AUDIT_SAMPLE_AV_UNTRUSTED")
        media.append(path)
    if receipt["before"]["sha256"] == receipt["after"]["sha256"]:
        raise ValueError("LOCAL_AB_AUDIT_VARIANTS_IDENTICAL")
    similarity = _ssim(ffmpeg, media[0], media[1])
    bmean, bpeak = _measure(ffmpeg, media[0], 0, duration)
    amean, apeak = _measure(ffmpeg, media[1], 0, duration)
    difference = round(amean - bmean, 2)
    # Independently verify the candidate result, but never infer "better".
    if kind == "audio":
        if not -3.0 <= difference <= -0.7 or apeak > bpeak + 0.5:
            raise ValueError("LOCAL_AB_AUDIT_AUDIO_ATTENUATION_UNVERIFIED")
        for name, measured_mean, measured_peak in (
            ("before", bmean, bpeak), ("after", amean, apeak)
        ):
            data = receipt[name]
            if (abs(data.get("mean_dbfs", 999) - measured_mean) > 0.51
                    or abs(data.get("peak_dbfs", 999) - measured_peak) > 0.51):
                raise ValueError("LOCAL_AB_AUDIT_AUDIO_RECEIPT_CONFLICT")
    elif similarity >= 1.0:
        raise ValueError("LOCAL_AB_AUDIT_VIDEO_VARIATION_NOT_OBSERVED")
    # Make sure the samples stayed the same throughout the measurement.
    if any(_hash(p)[0] != receipt[n]["sha256"] for p, n in zip(
        media, ("before", "after"), strict=True
    )):
        raise ValueError("LOCAL_AB_AUDIT_SAMPLE_CHANGED_DURING_MEASURE")
    return {
        "kind": kind,
        "ab_receipt_sha256": expected_sha,
        "before_sha256": receipt["before"]["sha256"],
        "after_sha256": receipt["after"]["sha256"],
        "sample_duration_seconds": round(duration, 3),
        "decoded_video_ssim": similarity,
        "before_audio_mean_dbfs": bmean,
        "after_audio_mean_dbfs": amean,
        "after_minus_before_mean_db": difference,
        "before_audio_peak_dbfs": bpeak,
        "after_audio_peak_dbfs": apeak,
        "technical_signal_observed": True,
        "editorial_improvement_confirmed": False,
        "human_reference_labels_available": False,
    }


def evaluate_independent_ab_signals(
    *, mp4_output: Path, video_sha256: str,
    triage_sha256: str, audio_sha256: str,
    subtitle_sha256: str, cut_sha256: str, scan_allowed: bool,
) -> dict[str, object]:
    """Read-only A/B comparison, save only a new immutable JSON receipt."""
    result: dict[str, object] = {
        "status": "LOCAL_AB_AUDIT_UNAVAILABLE",
        "sha256": "", "measured_pair_count": 0,
        "human_quality_qualified": False,
        "auto_adoption_allowed": False,
        "ready": False, "production_ready": False,
        "publication_performed": False,
    }
    if not scan_allowed:
        return result
    supplied = {
        "audio": audio_sha256,
        "subtitle": subtitle_sha256,
        "cut": cut_sha256,
    }
    try:
        base = _no_links(mp4_output)
        if (not base.is_file() or base.suffix.lower() != ".mp4"
                or not _JOB.fullmatch(base.stem)
                or not isinstance(video_sha256, str)
                or not _SHA.fullmatch(video_sha256)
                or not isinstance(triage_sha256, str)
                or not _SHA.fullmatch(triage_sha256)):
            raise ValueError("LOCAL_AB_AUDIT_BASE_UNTRUSTED")
        chosen = [(kind, sha) for kind, sha in supplied.items() if sha]
        if not chosen:
            result["status"] = "LOCAL_AB_AUDIT_NOT_APPLICABLE"
            return result
        if any(not isinstance(sha, str) or not _SHA.fullmatch(sha)
               for _, sha in chosen):
            raise ValueError("LOCAL_AB_AUDIT_SHA_UNTRUSTED")
        destination = _no_links(base.with_name(f"{base.stem}.ab-metrics.json"))
        if destination.exists():
            raise ValueError("LOCAL_AB_AUDIT_ALREADY_EXISTS")
        # Verify the *small* triage ledger first. Large baseline was verified
        # during rendering and each source trial; do not rehash it again here.
        triage = _load_verified(
            base.with_name(f"{base.stem}.triage.json"), triage_sha256,
        )
        if (triage.get("base_sha256_from_review") != video_sha256
                or triage.get("ready") is not False
                or triage.get("auto_repair_allowed") is not False):
            raise ValueError("LOCAL_AB_AUDIT_TRIAGE_INVALID")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("LOCAL_AB_AUDIT_FFMPEG_UNAVAILABLE")
        rows = [
            _audit_one(
                base=base, base_sha=video_sha256, triage_sha=triage_sha256,
                kind=kind, expected_sha=sha, ffmpeg=ffmpeg,
            ) for kind, sha in chosen
        ]
        payload = {
            "version": VERSION,
            "base_sha256_from_existing_qa": video_sha256,
            "triage_sha256": triage_sha256,
            "original_mp4_independently_rehashed": False,
            "all_sample_shas_reverified": True,
            "measured_pair_count": len(rows),
            "comparisons": rows,
            "objective_technical_metrics_only": True,
            "creative_quality_ground_truth_available": False,
            "human_quality_qualified": False,
            "auto_adoption_allowed": False,
            "repair_execution_performed": False,
            "publication_performed": False,
            "ready": False,
            "production_ready": False,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("LOCAL_AB_AUDIT_OUTPUT_TOO_LARGE")
        with tempfile.TemporaryDirectory(
            prefix=".holoclip-ab-metrics-", dir=base.parent,
        ) as td:
            temp = Path(td) / "metrics.json"
            temp.write_bytes(encoded)
            if hashlib.sha256(
                base.with_name(f"{base.stem}.triage.json").read_bytes()
            ).hexdigest() != triage_sha256:
                raise ValueError("LOCAL_AB_AUDIT_TRIAGE_CHANGED")
            try:
                os.link(temp, destination)
            except OSError as exc:
                raise ValueError("LOCAL_AB_AUDIT_CREATE_ONLY_FAILED") from exc
        result.update({
            "status": "LOCAL_AB_AUDIT_MEASURED_TECHNICAL_ONLY",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "measured_pair_count": len(rows),
        })
    except (OSError, ValueError, TypeError, KeyError, UnicodeError,
            AttributeError, json.JSONDecodeError):
        pass
    return result
