"""Small offline A/B *listening* previews, not automatic edit corrections.

Only the sampled first-10-second audio-peak candidate may create two short
clips with identical video parameters and a fixed -1.5 dB difference.
Original render, source, SRT, leveled and captioned media are NEVER changed.
A reversible fallback is the SHA-checked unchanged original render, not an
operation that replaces or deletes files. Every receipt remains unqualified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from holoclip.render_audio_finish import _measure
from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_review_triage import TRIAGE_VERSION

AB_VERSION = "holoclip-audio-ab-comparison-v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_MAX_RECEIPT = 65536
_MAX_SECONDS = 4.0
_GAIN_DB = -1.5
_MAX_TIMEOUT = 35.0


def _verified_triage(video: Path, expected_sha: str,
                     base_sha: str, final_sha: str) -> dict | None:
    if not _SHA.fullmatch(expected_sha):
        return None
    path = _no_links(video.with_name(f"{video.stem}.triage.json"))
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_RECEIPT:
        raise ValueError("LOCAL_AB_TRIAGE_UNAVAILABLE")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("LOCAL_AB_TRIAGE_CHANGED")
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("version") != TRIAGE_VERSION
            or data.get("base_sha256_from_review") != base_sha
            or data.get("final_sha256_from_review") != final_sha
            or data.get("ready") is not False
            or data.get("production_ready") is not False
            or data.get("auto_repair_allowed") is not False
            or data.get("repair_execution_performed") is not False
            or data.get("publication_performed") is not False
            or not isinstance(data.get("candidate_queue"), list)
            or len(data["candidate_queue"]) > 300):
        raise ValueError("LOCAL_AB_TRIAGE_UNTRUSTED")
    found = []
    for item in data["candidate_queue"]:
        if not isinstance(item, dict) or item.get("auto_repair_allowed") is not False:
            raise ValueError("LOCAL_AB_CANDIDATE_UNTRUSTED")
        if item.get("kind") != "audio_peak":
            continue
        draft = item.get("repair_draft")
        if (not isinstance(draft, dict)
                or draft.get("kind") != "NON_EXECUTABLE_INSPECTION_PROPOSAL"
                or draft.get("operation_authorized") is not False
                or type(item.get("review_index")) is not int
                or item["review_index"] < 0
                or type(item.get("start_seconds")) not in (int, float)
                or item["start_seconds"] != 0.0
                or type(item.get("end_seconds")) not in (int, float)
                or not 1.5 <= item["end_seconds"] <= 10.0):
            raise ValueError("LOCAL_AB_AUDIO_CANDIDATE_INVALID")
        found.append(item)
    if not found:
        return None
    # One bounded demonstration only, never repeated over every candidate.
    return found[0]


def _render_sample(ffmpeg: str, source: Path, target: Path, seconds: float,
                   *, change_audio: bool) -> None:
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-n", "-protocol_whitelist", "file,pipe",
        "-i", str(source), "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", "scale=480:-2:flags=bicubic,fps=15",
        "-af", f"volume={_GAIN_DB:+.1f}dB" if change_audio else "anull",
        "-c:v", "mpeg4", "-q:v", "5",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-map_metadata", "-1", "-movflags", "+faststart",
        str(target),
    ]
    try:
        process = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=_MAX_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_AB_ENCODER_UNAVAILABLE") from exc
    if process.returncode or not target.is_file():
        raise ValueError("LOCAL_AB_ENCODE_FAILED")


def _produce(*, base: Path, base_sha: str,
             final: Path, final_sha: str, triage_sha: str) -> dict | None:
    base = _no_links(base)
    final = _no_links(final)
    if (not base.is_file() or not final.is_file()
            or base.suffix != ".mp4" or final.suffix != ".mp4"
            or base.parent != final.parent
            or not _JOB.fullmatch(base.stem)
            or final not in {
                base, base.with_name(f"{base.stem}.leveled.mp4"),
                base.with_name(f"{base.stem}.captioned.mp4"),
            } or not _SHA.fullmatch(base_sha) or not _SHA.fullmatch(final_sha)):
        raise ValueError("LOCAL_AB_MEDIA_UNTRUSTED")
    destination = _no_links(base.with_name(f"{base.stem}.ab-preview"))
    if destination.exists():
        raise ValueError("LOCAL_AB_PREVIEW_ALREADY_EXISTS")
    candidate = _verified_triage(base, triage_sha, base_sha, final_sha)
    if candidate is None:
        return None
    duration, video, audio = _probe(final)
    if not video or not audio or not 1.5 <= duration <= 180.5:
        raise ValueError("LOCAL_AB_AV_UNVERIFIED")
    seconds = min(_MAX_SECONDS, duration, candidate["end_seconds"])
    if not 1.5 <= seconds <= _MAX_SECONDS:
        raise ValueError("LOCAL_AB_WINDOW_INVALID")
    # Both input artifacts are checked; when final == base only one hash.
    if (_hash(base)[0] != base_sha
            or _hash(final)[0] != final_sha):
        raise ValueError("LOCAL_AB_MEDIA_HASH_CHANGED")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_AB_FFMPEG_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix=".holoclip-ab-", dir=base.parent) as work:
        temp = Path(work)
        before = temp / "before.mp4"
        after = temp / "after.mp4"
        _render_sample(ffmpeg, final, before, seconds, change_audio=False)
        _render_sample(ffmpeg, final, after, seconds, change_audio=True)
        for sample in (before, after):
            measured, av, aa = _probe(sample)
            if not av or not aa or abs(measured - seconds) > 0.6:
                raise ValueError("LOCAL_AB_SAMPLE_AV_UNVERIFIED")
        before_mean, before_peak = _measure(ffmpeg, before, 0, seconds)
        after_mean, after_peak = _measure(ffmpeg, after, 0, seconds)
        # An attenuation preview can make signal level quieter, but cannot
        # restore already clipped information. Do not claim actual repair.
        if (after_peak > before_peak - 0.7 or after_peak > 0.1
                or after_mean > before_mean - 0.7
                or after_mean < before_mean - 3.0):
            raise ValueError("LOCAL_AB_DIFFERENCE_UNVERIFIED")
        before_sha, _ = _hash(before)
        after_sha, _ = _hash(after)
        if (_hash(base)[0] != base_sha
                or _hash(final)[0] != final_sha):
            raise ValueError("LOCAL_AB_BASELINE_CHANGED")
        receipt = {
            "version": AB_VERSION,
            "triage_sha256": triage_sha,
            "base_sha256": base_sha,
            "final_sha256": final_sha,
            "review_index": candidate["review_index"],
            "candidate_kind": "audio_peak",
            "sample_start_seconds": 0.0,
            "sample_duration_seconds": round(seconds, 3),
            "trial_audio_gain_db": _GAIN_DB,
            "before": {
                "filename": "before.mp4", "sha256": before_sha,
                "mean_dbfs": before_mean, "peak_dbfs": before_peak,
            },
            "after": {
                "filename": "after.mp4", "sha256": after_sha,
                "mean_dbfs": after_mean, "peak_dbfs": after_peak,
            },
            "baseline_recovery": {
                "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
                "sha256": base_sha,
                "baseline_reverified_after_preview": True,
                "files_replaced_or_deleted": False,
            },
            "alternative_is_test_only": True,
            "clipped_source_audio_restored": False,
            "editorial_quality_metrics_available": False,
            "repair_execution_performed": False,
            "preview_experiment_generated": True,
            "auto_repair_allowed": False,
            "publication_performed": False,
            "ready": False,
            "production_ready": False,
        }
        encoded = (json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECEIPT:
            raise ValueError("LOCAL_AB_RECEIPT_TOO_LARGE")
        (temp / "receipt.json").write_bytes(encoded)
        # Reserve a fresh directory using mkdir(exist_ok=False), then
        # hardlink only our own temporary samples. Existing directories
        # cannot be clobbered on POSIX or Windows even under a race.
        # Receipt is linked LAST: absent receipt means incomplete/unqualified.
        try:
            destination.mkdir()
            for name in ("before.mp4", "after.mp4", "receipt.json"):
                os.link(temp / name, destination / name)
        except OSError as exc:
            raise ValueError("LOCAL_AB_COMMIT_UNAVAILABLE") from exc
        return {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "review_index": candidate["review_index"],
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "baseline_sha256": base_sha,
        }


def create_audio_ab_preview(
    *, mp4_output: Path, video_sha256: str, final_output: Path,
    final_sha256: str, triage_sha256: str, scan_allowed: bool,
) -> dict[str, object]:
    """Optional locally generated A/B preview, no original-media changes."""
    result: dict[str, object] = {
        "status": "LOCAL_AB_PREVIEW_UNAVAILABLE",
        "sha256": "", "sample_count": 0,
        "baseline_recovery_verified": False,
        "repair_execution_performed": False,
        "publication_performed": False,
        "auto_repair_allowed": False,
        "ready": False,
        "production_ready": False,
    }
    if not scan_allowed:
        return result
    try:
        proof = _produce(
            base=mp4_output, base_sha=video_sha256,
            final=final_output, final_sha=final_sha256,
            triage_sha=triage_sha256,
        )
        if proof is None:
            result["status"] = "LOCAL_AB_PREVIEW_NOT_APPLICABLE"
        else:
            result.update({
                "status": "LOCAL_AB_PREVIEW_CREATED_UNVERIFIED_EDITORIAL",
                "sha256": proof["sha256"],
                "sample_count": 1,
                "baseline_recovery_verified": True,
            })
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        pass
    return result
