"""Short, non-destructive visual A/B of subtitle *style*, not timing fixes.

Only existing SHA-verified local SRT cue and uncaptioned base/leveled video
are used. The experimental B style does not claim to correct fast reading,
transcription or an editorial problem. No full-length media is altered.
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

from holoclip.render_plan import _hash, _no_links, _probe
from holoclip.render_review_triage import TRIAGE_VERSION, _load_hash_verified, _validated_review
from holoclip.render_subtitle_burnin import (
    Caption, _ass, _japanese_font, _read_cues, _wrap,
)

VERSION = "holoclip-subtitle-style-ab-v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_MAX_WINDOW = 4.0


def _candidate(base: Path, triage_sha256: str) -> dict | None:
    triage = _load_hash_verified(
        base.with_name(f"{base.stem}.triage.json"), triage_sha256,
    )
    if (triage.get("version") != TRIAGE_VERSION
            or triage.get("ready") is not False
            or triage.get("production_ready") is not False
            or triage.get("auto_repair_allowed") is not False
            or triage.get("repair_execution_performed") is not False
            or triage.get("publication_performed") is not False
            or not isinstance(triage.get("candidate_queue"), list)
            or len(triage["candidate_queue"]) > 300):
        raise ValueError("LOCAL_SUBTITLE_AB_TRIAGE_UNTRUSTED")
    review_sha = triage.get("review_sha256")
    review = _load_hash_verified(
        base.with_name(f"{base.stem}.review.json"), review_sha,
    )
    reviewed = _validated_review(review)
    if (triage.get("base_sha256_from_review") != review["base_sha256"]
            or triage.get("final_sha256_from_review") != review["final_sha256"]
            or triage.get("candidate_count") != len(reviewed)
            or len(triage["candidate_queue"]) != len(reviewed)):
        raise ValueError("LOCAL_SUBTITLE_AB_REVIEW_MISMATCH")
    matched = []
    seen = set()
    for item in triage["candidate_queue"]:
        if (not isinstance(item, dict)
                or type(item.get("review_index")) is not int
                or not 0 <= item["review_index"] < len(reviewed)
                or item["review_index"] in seen
                or item.get("auto_repair_allowed") is not False):
            raise ValueError("LOCAL_SUBTITLE_AB_TRIAGE_ROW_UNTRUSTED")
        i = item["review_index"]
        seen.add(i)
        original = reviewed[i]
        if any(item.get(k) != original[k] for k in (
            "kind", "start_seconds", "end_seconds", "segment_ids",
        )):
            raise ValueError("LOCAL_SUBTITLE_AB_TRIAGE_ROW_MISMATCH")
        if item["kind"] == "fast_subtitle":
            draft = item.get("repair_draft")
            if (not isinstance(draft, dict)
                    or draft.get("kind") != "NON_EXECUTABLE_INSPECTION_PROPOSAL"
                    or draft.get("operation_authorized") is not False):
                raise ValueError("LOCAL_SUBTITLE_AB_DRAFT_UNTRUSTED")
            matched.append(original)
    return matched[0] if matched else None


def _style_ass(cue: Caption, *, font_name: str, enhanced: bool) -> str:
    baseline = _ass([cue], font_name=font_name)
    if not enhanced:
        return baseline
    old = f"Style: JP,{font_name},43,"
    if baseline.count(old) != 1:
        raise ValueError("LOCAL_SUBTITLE_AB_STYLE_INVALID")
    # The experimental appearance uses a slightly larger typeface and
    # higher contrast outline; it never modifies text or start/end timing.
    return baseline.replace(old, f"Style: JP,{font_name},48,", 1).replace(
        ",1,3,1,2,64,64,48,1\n",
        ",1,4,1,2,64,64,54,1\n", 1,
    )


def _encode(ffmpeg: str, input_video: Path, sample: Path, *,
            start: float, seconds: float, work: Path, enhanced: bool) -> None:
    ass = "enhanced.ass" if enhanced else "baseline.ass"
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-protocol_whitelist", "file,pipe",
        "-ss", f"{start:.3f}", "-i", str(input_video),
        "-t", f"{seconds:.3f}",
        "-vf", f"ass=filename={ass}:fontsdir=fonts,scale=480:-2:flags=bicubic,fps=15",
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-movflags", "+faststart", "-map_metadata", "-1",
        str(sample),
    ]
    try:
        proc = subprocess.run(
            argv, cwd=work, check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_SUBTITLE_AB_ENCODER_UNAVAILABLE") from exc
    if proc.returncode != 0 or not sample.is_file():
        raise ValueError("LOCAL_SUBTITLE_AB_ENCODE_FAILED")


def _produce(*, base: Path, base_sha: str, input_video: Path,
             input_sha: str, final_sha: str, srt_sha: str,
             triage_sha: str) -> dict | None:
    base = _no_links(base)
    input_video = _no_links(input_video)
    if (not _JOB.fullmatch(base.stem) or base.suffix.lower() != ".mp4"
            or not base.is_file() or not input_video.is_file()
            or input_video not in {
                base, base.with_name(f"{base.stem}.leveled.mp4")
            }
            or any(not isinstance(s, str) or not _SHA.fullmatch(s)
                   for s in (base_sha, input_sha, final_sha, srt_sha, triage_sha))):
        raise ValueError("LOCAL_SUBTITLE_AB_MEDIA_UNTRUSTED")
    target = _no_links(base.with_name(f"{base.stem}.subtitle-ab"))
    if target.exists():
        raise ValueError("LOCAL_SUBTITLE_AB_PREEXISTING")
    cue_info = _candidate(base, triage_sha)
    if cue_info is None:
        return None
    # Confirm the trial refers to the exact original/final render lineage.
    triage = _load_hash_verified(
        base.with_name(f"{base.stem}.triage.json"), triage_sha,
    )
    if (triage["base_sha256_from_review"] != base_sha
            or triage["final_sha256_from_review"] != final_sha):
        raise ValueError("LOCAL_SUBTITLE_AB_LINEAGE_CHANGED")
    sidecar = _no_links(base.with_suffix(".srt"))
    if not sidecar.is_file() or sidecar.stat().st_size > 131072:
        raise ValueError("LOCAL_SUBTITLE_AB_SRT_UNAVAILABLE")
    raw = sidecar.read_bytes()
    if hashlib.sha256(raw).hexdigest() != srt_sha:
        raise ValueError("LOCAL_SUBTITLE_AB_SRT_CHANGED")
    cues = _read_cues(raw)
    number = cue_info.get("cue_number")
    if type(number) is not int or not 1 <= number <= len(cues):
        raise ValueError("LOCAL_SUBTITLE_AB_CUE_INVALID")
    cue = cues[number - 1]
    if (abs(cue.start - cue_info["start_seconds"]) > 0.002
            or abs(cue.end - cue_info["end_seconds"]) > 0.002):
        raise ValueError("LOCAL_SUBTITLE_AB_CUE_CLOCK_MISMATCH")
    _wrap(cue.text)  # Long/wide captions cannot be shrunk into an illegible trial.
    duration, has_video, has_audio = _probe(input_video)
    if not has_video or not has_audio or not 1.5 <= duration <= 180.5:
        raise ValueError("LOCAL_SUBTITLE_AB_VIDEO_UNVERIFIED")
    start = max(0.0, cue.start - 0.4)
    seconds = min(_MAX_WINDOW, duration - start, cue.end - start + 0.4)
    if not 0.6 <= seconds <= _MAX_WINDOW:
        raise ValueError("LOCAL_SUBTITLE_AB_WINDOW_UNTRUSTED")
    if _hash(base)[0] != base_sha or _hash(input_video)[0] != input_sha:
        raise ValueError("LOCAL_SUBTITLE_AB_SOURCE_CHANGED")
    font = _japanese_font()
    ffmpeg = shutil.which("ffmpeg")
    if font is None or not ffmpeg:
        raise ValueError("LOCAL_SUBTITLE_AB_RENDERER_UNAVAILABLE")
    font_path, font_name = font
    relative = Caption(
        max(0.0, cue.start - start),
        min(seconds, cue.end - start), cue.text,
    )
    if relative.end <= relative.start:
        raise ValueError("LOCAL_SUBTITLE_AB_CLOCK_UNTRUSTED")
    with tempfile.TemporaryDirectory(
        prefix=".holoclip-subtitle-ab-", dir=base.parent,
    ) as name:
        work = Path(name)
        fontdir = work / "fonts"
        fontdir.mkdir()
        shutil.copyfile(font_path, fontdir / ("caption" + font_path.suffix.lower()))
        (work / "baseline.ass").write_text(
            _style_ass(relative, font_name=font_name, enhanced=False),
            encoding="utf-8",
        )
        (work / "enhanced.ass").write_text(
            _style_ass(relative, font_name=font_name, enhanced=True),
            encoding="utf-8",
        )
        baseline = work / "before.mp4"
        experimental = work / "after.mp4"
        _encode(ffmpeg, input_video, baseline, start=start, seconds=seconds,
                work=work, enhanced=False)
        _encode(ffmpeg, input_video, experimental, start=start, seconds=seconds,
                work=work, enhanced=True)
        for item in (baseline, experimental):
            elapsed, video, audio = _probe(item)
            if not video or not audio or abs(elapsed - seconds) > 0.5:
                raise ValueError("LOCAL_SUBTITLE_AB_RESULT_UNVERIFIED")
        bsha, _ = _hash(baseline)
        asha, _ = _hash(experimental)
        if bsha == asha:
            raise ValueError("LOCAL_SUBTITLE_AB_NO_VISUAL_VARIANT")
        if (_hash(base)[0] != base_sha or _hash(input_video)[0] != input_sha
                or hashlib.sha256(sidecar.read_bytes()).hexdigest() != srt_sha):
            raise ValueError("LOCAL_SUBTITLE_AB_SOURCE_CHANGED")
        receipt = {
            "version": VERSION,
            "triage_sha256": triage_sha,
            "base_sha256": base_sha,
            "input_sha256": input_sha,
            "final_sha256_from_review": final_sha,
            "subtitle_sha256": srt_sha,
            "cue_number": number,
            "candidate_kind": "fast_subtitle",
            "window_start_seconds": round(start, 3),
            "window_duration_seconds": round(seconds, 3),
            "cue_text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
            "text_and_timing_unchanged_between_variants": True,
            "before": {"filename": "before.mp4", "sha256": bsha,
                       "style": "ORIGINAL_BURNIN_STYLE_43"},
            "after": {"filename": "after.mp4", "sha256": asha,
                      "style": "EXPERIMENTAL_HIGH_CONTRAST_48"},
            "baseline_recovery": {
                "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
                "sha256": base_sha, "baseline_reverified_after_preview": True,
                "files_replaced_or_deleted": False,
            },
            "visual_readability_improvement_confirmed": False,
            "fast_reading_time_corrected": False,
            "human_editorial_qa_completed": False,
            "preview_experiment_generated": True,
            "repair_execution_performed": False,
            "auto_repair_allowed": False,
            "publication_performed": False,
            "ready": False, "production_ready": False,
        }
        encoded = (json.dumps(
            receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("LOCAL_SUBTITLE_AB_RECEIPT_TOO_LARGE")
        (work / "receipt.json").write_bytes(encoded)
        # Exclusive create-only reservation; commit receipt last. An interrupted
        # directory without receipt is never a valid comparison and not retried.
        target.mkdir()
        for item in ("before.mp4", "after.mp4", "receipt.json"):
            os.link(work / item, target / item)
        return {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "cue_number": number, "variant_count": 2,
            "baseline_recovery_verified": True,
        }


def create_subtitle_ab_preview(
    *, mp4_output: Path, video_sha256: str,
    input_video: Path, input_sha256: str,
    final_sha256: str, subtitle_sha256: str,
    triage_sha256: str, scan_allowed: bool,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "LOCAL_SUBTITLE_AB_UNAVAILABLE",
        "sha256": "", "variant_count": 0,
        "cue_number": 0, "baseline_recovery_verified": False,
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False, "publication_performed": False,
    }
    if not scan_allowed:
        return result
    try:
        measured = _produce(
            base=mp4_output, base_sha=video_sha256,
            input_video=input_video, input_sha=input_sha256,
            final_sha=final_sha256, srt_sha=subtitle_sha256,
            triage_sha=triage_sha256,
        )
        if measured is None:
            result["status"] = "LOCAL_SUBTITLE_AB_NOT_APPLICABLE"
        else:
            result.update({
                "status": "LOCAL_SUBTITLE_AB_CREATED_UNVERIFIED_EDITORIAL",
                **measured,
            })
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        pass
    return result
