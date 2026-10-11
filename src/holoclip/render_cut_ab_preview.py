"""Local cut-seam A/B visual experiment without changing an edit decision.

A: the exact already-rendered join. B: the same output time window with a
fixed 0.16s semi-transparent dark cue around the join, *not* a recut.
Audio, cut clocks, full-length previews, source files and SRT stay untouched.
Neither variant may be adopted automatically or certified editorially.
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

from holoclip.render_plan import RenderPlan, _hash, _no_links, _probe
from holoclip.render_quality_review import _boundaries
from holoclip.render_review_triage import (
    TRIAGE_VERSION, _load_hash_verified, _validated_review,
)
from holoclip.render_visual_style import select_cut_styles

VERSION = "holoclip-cut-seam-ab-preview-v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_MAX_SECONDS = 3.0
_DARK_SECONDS = 0.16


def _pick_seam(plan: RenderPlan, triage_sha: str, media_sha: str) -> dict | None:
    base = _no_links(plan.output)
    triage = _load_hash_verified(
        base.with_name(f"{base.stem}.triage.json"), triage_sha,
    )
    if (triage.get("version") != TRIAGE_VERSION
            or triage.get("base_sha256_from_review") != media_sha
            or triage.get("ready") is not False
            or triage.get("production_ready") is not False
            or triage.get("auto_repair_allowed") is not False
            or triage.get("repair_execution_performed") is not False
            or triage.get("publication_performed") is not False
            or type(triage.get("candidate_count")) is not int
            or not isinstance(triage.get("candidate_queue"), list)
            or triage["candidate_count"] != len(triage["candidate_queue"])):
        raise ValueError("LOCAL_CUT_AB_TRIAGE_UNTRUSTED")
    review = _load_hash_verified(
        base.with_name(f"{base.stem}.review.json"),
        triage.get("review_sha256"),
    )
    checked = _validated_review(review)
    if (review["base_sha256"] != media_sha
            or review["final_sha256"] != triage.get("final_sha256_from_review")
            or len(checked) != triage["candidate_count"]):
        raise ValueError("LOCAL_CUT_AB_REVIEW_MISMATCH")
    order = set()
    boundary_candidates: list[dict] = []
    for record in triage["candidate_queue"]:
        if not isinstance(record, dict):
            raise ValueError("LOCAL_CUT_AB_TRIAGE_ROW_INVALID")
        index = record.get("review_index")
        if type(index) is not int or index in order or not 0 <= index < len(checked):
            raise ValueError("LOCAL_CUT_AB_TRIAGE_INDEX_INVALID")
        order.add(index)
        original = checked[index]
        if (record.get("auto_repair_allowed") is not False
                or any(record.get(k) != original[k] for k in (
                    "kind", "start_seconds", "end_seconds", "segment_ids",
                ))
                or not isinstance(record.get("repair_draft"), dict)
                or record["repair_draft"].get("operation_authorized") is not False):
            raise ValueError("LOCAL_CUT_AB_TRIAGE_CHANGED")
        if original["position"] == "cut_boundary":
            boundary_candidates.append(original)
    windows = _boundaries(plan.timeline)
    if len(windows) < 2:
        return None
    cuts = sorted(plan.timeline.segments, key=lambda s: s.output_order)
    styles = select_cut_styles(plan.timeline)
    feasible = []
    for i in range(len(windows) - 1):
        left, seam, left_id = windows[i]
        _, right, right_id = windows[i + 1]
        # Existing render already applied an artistically hinted dip.
        # Do not propose a second darkening over that intentionally styled cut.
        if (styles[i].fade_out or styles[i + 1].fade_in
                or seam - left < 0.85 or right - seam < 0.85):
            continue
        feasible.append({
            "seam_seconds": seam, "left_id": left_id,
            "right_id": right_id, "index": i,
            "flagged": any(
                left_id in item["segment_ids"]
                and right_id in item["segment_ids"]
                for item in boundary_candidates
            ),
        })
    if not feasible:
        return None
    feasible.sort(key=lambda row: (not row["flagged"], row["index"]))
    return feasible[0]


def _encode(ffmpeg: str, source: Path, target: Path, *,
            begin: float, seconds: float,
            relative_seam: float, experimental: bool) -> None:
    video_filter = "scale=480:-2:flags=bicubic,fps=15"
    if experimental:
        start = relative_seam - _DARK_SECONDS / 2
        stop = relative_seam + _DARK_SECONDS / 2
        video_filter += (
            ",drawbox=x=0:y=0:w=iw:h=ih:color=black@0.4:t=fill:"
            f"enable='between(t,{start:.3f},{stop:.3f})'"
        )
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-protocol_whitelist", "file,pipe",
        "-ss", f"{begin:.3f}", "-i", str(source),
        "-t", f"{seconds:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", video_filter,
        "-c:v", "mpeg4", "-q:v", "5",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-map_metadata", "-1", "-movflags", "+faststart",
        str(target),
    ]
    try:
        p = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=35.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_CUT_AB_ENCODER_UNAVAILABLE") from exc
    if p.returncode or not target.is_file():
        raise ValueError("LOCAL_CUT_AB_ENCODE_FAILED")


def _produce(*, plan: RenderPlan, video_sha256: str,
             triage_sha256: str) -> dict | None:
    plan = RenderPlan.model_validate(plan.model_dump(mode="json"))
    media = _no_links(plan.output)
    if (not _JOB.fullmatch(media.stem) or media.suffix.lower() != ".mp4"
            or not media.is_file()
            or not isinstance(video_sha256, str)
            or not _SHA.fullmatch(video_sha256)
            or not isinstance(triage_sha256, str)
            or not _SHA.fullmatch(triage_sha256)):
        raise ValueError("LOCAL_CUT_AB_MEDIA_UNTRUSTED")
    target = _no_links(media.with_name(f"{media.stem}.cut-ab"))
    if target.exists():
        raise ValueError("LOCAL_CUT_AB_ALREADY_EXISTS")
    seam = _pick_seam(plan, triage_sha256, video_sha256)
    if seam is None:
        return None
    duration, has_video, has_audio = _probe(media)
    if (not has_video or not has_audio or not math.isfinite(duration)
            or not 1.75 <= duration <= 180.5
            or abs(duration - plan.expected_duration) > 0.7):
        raise ValueError("LOCAL_CUT_AB_MEDIA_CLOCK_INVALID")
    pivot = seam["seam_seconds"]
    begin = max(0.0, pivot - 1.5)
    end = min(duration, pivot + 1.5)
    length = end - begin
    relative = pivot - begin
    if not (1.7 <= length <= _MAX_SECONDS
            and 0.8 <= relative <= length - 0.8):
        raise ValueError("LOCAL_CUT_AB_WINDOW_INVALID")
    if _hash(media)[0] != video_sha256:
        raise ValueError("LOCAL_CUT_AB_MEDIA_CHANGED")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_CUT_AB_FFMPEG_UNAVAILABLE")
    with tempfile.TemporaryDirectory(
        prefix=".holoclip-cut-ab-", dir=media.parent,
    ) as directory:
        tmp = Path(directory)
        before = tmp / "before.mp4"
        after = tmp / "after.mp4"
        _encode(ffmpeg, media, before, begin=begin, seconds=length,
                relative_seam=relative, experimental=False)
        _encode(ffmpeg, media, after, begin=begin, seconds=length,
                relative_seam=relative, experimental=True)
        for result in (before, after):
            seconds, video, audio = _probe(result)
            if not video or not audio or abs(seconds - length) > 0.5:
                raise ValueError("LOCAL_CUT_AB_SAMPLE_INVALID")
        bsha, _ = _hash(before)
        asha, _ = _hash(after)
        if bsha == asha:
            raise ValueError("LOCAL_CUT_AB_NO_VISUAL_DIFFERENCE")
        if (_hash(media)[0] != video_sha256
                or hashlib.sha256(
                    media.with_name(f"{media.stem}.triage.json").read_bytes(),
                ).hexdigest() != triage_sha256):
            raise ValueError("LOCAL_CUT_AB_LINEAGE_CHANGED")
        receipt = {
            "version": VERSION,
            "render_plan_id": plan.id,
            "triage_sha256": triage_sha256,
            "base_sha256": video_sha256,
            "left_segment_id": seam["left_id"],
            "right_segment_id": seam["right_id"],
            "seam_output_seconds": round(pivot, 3),
            "sample_start_seconds": round(begin, 3),
            "sample_duration_seconds": round(length, 3),
            "selection": (
                "REVIEW_FLAGGED_CUT_BOUNDARY" if seam["flagged"]
                else "FIRST_ELIGIBLE_HARD_CUT"
            ),
            "before": {"filename": "before.mp4", "sha256": bsha,
                       "transition": "ORIGINAL_RENDERED_CUT"},
            "after": {"filename": "after.mp4", "sha256": asha,
                      "transition": "EXPERIMENTAL_160MS_DARKENING"},
            "video_cut_times_unchanged": True,
            "audio_cut_times_unchanged": True,
            "audio_effect_applied": False,
            "full_length_render_replaced": False,
            "visual_improvement_confirmed": False,
            "editing_correction_performed": False,
            "preview_experiment_generated": True,
            "human_editorial_qa_completed": False,
            "auto_repair_allowed": False,
            "publication_performed": False,
            "ready": False, "production_ready": False,
            "baseline_recovery": {
                "kind": "USE_UNMODIFIED_ORIGINAL_RENDER",
                "sha256": video_sha256,
                "baseline_reverified_after_preview": True,
                "files_replaced_or_deleted": False,
            },
        }
        raw = (json.dumps(
            receipt, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(raw) > 65536:
            raise ValueError("LOCAL_CUT_AB_RECEIPT_TOO_LARGE")
        (tmp / "receipt.json").write_bytes(raw)
        # No overwrite, including an interrupted previous attempt.
        # Incomplete directories have no trustworthy receipt and are inert.
        target.mkdir()
        for name in ("before.mp4", "after.mp4", "receipt.json"):
            os.link(tmp / name, target / name)
        return {"sha256": hashlib.sha256(raw).hexdigest(),
                "seam_seconds": round(pivot, 3),
                "variant_count": 2, "baseline_recovery_verified": True}


def create_cut_seam_ab_preview(
    *, plan: RenderPlan, video_sha256: str,
    triage_sha256: str, scan_allowed: bool,
) -> dict[str, object]:
    report: dict[str, object] = {
        "status": "LOCAL_CUT_AB_UNAVAILABLE",
        "sha256": "", "seam_seconds": None,
        "variant_count": 0, "baseline_recovery_verified": False,
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False, "publication_performed": False,
    }
    if not scan_allowed:
        return report
    try:
        sample = _produce(
            plan=plan, video_sha256=video_sha256, triage_sha256=triage_sha256,
        )
        if sample is None:
            report["status"] = "LOCAL_CUT_AB_NOT_APPLICABLE"
        else:
            report.update({
                "status": "LOCAL_CUT_AB_CREATED_UNVERIFIED_EDITORIAL", **sample,
            })
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        pass
    return report
