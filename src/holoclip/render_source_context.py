"""Bounded source-window context for observed render black/freeze signals.

Review only: a matching source effect may be an intentional artistic scene, and
a missing measured source effect is not proof the renderer caused a defect.
Uses cached media already source-hash-verified around the completed render.
Does NOT independently rehash huge original streams at comparison time.
No downloads, model/ASR inference, transcript access, edits or publishing.
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

from holoclip.render_plan import RenderPlan, _no_links, _source_file
from holoclip.render_quality_review import _boundaries

SOURCE_CONTEXT_VERSION = "holoclip-source-event-context-v1"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_LOG_LIMIT = 65536
_MAX_EVENTS = 3
_WINDOW_LIMIT_SECONDS = 6.0
_MAX_SCAN_SECONDS = 15.0
_DETECT_FILTER = (
    "fps=2,scale=160:90,format=yuv420p,"
    "blackdetect=d=0.5:pix_th=0.10:pic_th=0.98,"
    "freezedetect=n=-60dB:d=1.5"
)
_BLACK_EVENT = re.compile(rb"black_start:\s*\d+(?:\.\d+)?\s+black_end:")
_FREEZE_EVENT = re.compile(rb"lavfi\.freezedetect\.freeze_start:\s*\d+(?:\.\d+)?")


def _source_signal(
    ffmpeg: str, path: Path, *, begin: float, seconds: float, kind: str,
) -> bool:
    """Fixed filter and local-files-only FFmpeg subprocess, no shell."""
    if (kind not in {"blackout", "freeze"}
            or not math.isfinite(begin) or not math.isfinite(seconds)
            or not 0 <= begin or not 1.5 <= seconds <= _WINDOW_LIMIT_SECONDS):
        raise ValueError("LOCAL_SOURCE_CONTEXT_WINDOW_INVALID")
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
        "-nostdin", "-protocol_whitelist", "file,pipe",
        "-ss", f"{begin:.3f}", "-i", str(path),
        "-map", "0:v:0", "-an", "-t", f"{seconds:.3f}",
        "-vf", _DETECT_FILTER, "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=_MAX_SCAN_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("LOCAL_SOURCE_CONTEXT_SCAN_FAILED") from exc
    if result.returncode or len(result.stderr) > _LOG_LIMIT:
        raise ValueError("LOCAL_SOURCE_CONTEXT_SCAN_FAILED")
    found = _BLACK_EVENT if kind == "blackout" else _FREEZE_EVENT
    return bool(found.search(result.stderr))


def _selected_windows(plan: RenderPlan, observed: list[dict]) -> list[dict]:
    windows = _boundaries(plan.timeline)
    cuts = sorted(plan.timeline.segments, key=lambda s: s.output_order)
    by_asset = {item.asset_id: item for item in plan.sources}
    if len(by_asset) != len(plan.sources):
        raise ValueError("LOCAL_SOURCE_CONTEXT_DUPLICATE_SOURCE")
    if len(observed) > 300:
        raise ValueError("LOCAL_SOURCE_CONTEXT_TOO_MANY_OBSERVATIONS")
    selected: list[dict] = []
    for position, item in enumerate(observed):
        if not isinstance(item, dict):
            raise ValueError("LOCAL_SOURCE_CONTEXT_OBSERVATION_INVALID")
        kind = item.get("kind")
        if kind not in {"blackout", "freeze"}:
            continue
        start, end = item.get("start_seconds"), item.get("end_seconds")
        if (type(start) not in (int, float) or not math.isfinite(start)
                or not 0 <= start <= windows[-1][1] + 0.1
                or (kind == "blackout" and (
                    type(end) not in (int, float) or not math.isfinite(end)
                    or not start < end <= windows[-1][1] + 1.1
                ))
                or (kind == "freeze" and end is not None)):
            raise ValueError("LOCAL_SOURCE_CONTEXT_OBSERVATION_INVALID")
        if len(selected) >= _MAX_EVENTS:
            break
        idx = next(
            (i for i, (left, right, _) in enumerate(windows)
             if left <= start < right), None,
        )
        if idx is None:
            raise ValueError("LOCAL_SOURCE_CONTEXT_OBSERVATION_OUTSIDE_CUT")
        left, right, segment_id = windows[idx]
        cut = cuts[idx]
        source = by_asset.get(cut.source_asset_id)
        if source is None:
            raise ValueError("LOCAL_SOURCE_CONTEXT_UNMAPPED_SOURCE")
        # Black spanning two cuts, or a freeze without sufficient observed
        # remaining window, gives no defensible one-to-one source comparison.
        if (kind == "blackout" and end > right - 0.05
                or kind == "blackout" and end - start < 1.0
                or kind == "freeze" and start + 3.0 > right - 0.05):
            selected.append({
                "observation_index": position,
                "kind": kind,
                "start_seconds": round(float(start), 3),
                "segment_id": segment_id,
                "status": "SKIPPED_BOUNDARY_OR_SHORT_OBSERVATION",
            })
            continue
        window_left = max(left + 0.01, start - 0.3)
        desired_end = (
            end + 0.3 if kind == "blackout" else start + 3.0
        )
        window_right = min(right - 0.01, desired_end)
        duration = window_right - window_left
        if not 1.5 <= duration <= _WINDOW_LIMIT_SECONDS:
            selected.append({
                "observation_index": position,
                "kind": kind,
                "start_seconds": round(float(start), 3),
                "segment_id": segment_id,
                "status": "SKIPPED_BOUNDARY_OR_SHORT_OBSERVATION",
            })
            continue
        source_begin = cut.source_start + window_left - left
        if source_begin + duration > cut.source_end + 0.01:
            raise ValueError("LOCAL_SOURCE_CONTEXT_SOURCE_CLOCK_INVALID")
        selected.append({
            "observation_index": position,
            "kind": kind,
            "start_seconds": round(float(start), 3),
            "segment_id": segment_id,
            "source_id": source.asset_id,
            "source_sha256": source.sha256,
            "source_path": source.path,
            "source_size_bytes": source.size_bytes,
            "window_begin": source_begin,
            "window_seconds": duration,
            "status": "PENDING",
        })
    return selected


def _compare(
    *, plan: RenderPlan, observations: list[dict],
    ffmpeg: str,
) -> dict:
    selected = _selected_windows(plan, observations)
    counts = {
        "matched": 0, "not_observed_in_source_window": 0,
        "skipped": 0, "unavailable": 0,
    }
    results = []
    for item in selected:
        # source paths are internal IO and must not go into the receipt.
        public = {name: item[name] for name in (
            "observation_index", "kind", "start_seconds", "segment_id",
        )}
        if item["status"] != "PENDING":
            public["status"] = item["status"]
            counts["skipped"] += 1
        else:
            public["source_asset_id"] = item["source_id"]
            public["source_sha256_from_render_plan"] = item["source_sha256"]
            public["source_window_start_seconds"] = round(
                item["window_begin"], 3,
            )
            public["source_window_duration_seconds"] = round(
                item["window_seconds"], 3,
            )
            try:
                source = _source_file(Path(item["source_path"]))
                before = source.stat()
                if before.st_size != item["source_size_bytes"]:
                    raise ValueError("LOCAL_SOURCE_CONTEXT_SOURCE_SIZE_CHANGED")
                detected = _source_signal(
                    ffmpeg, source, begin=item["window_begin"],
                    seconds=item["window_seconds"], kind=item["kind"],
                )
                after = source.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                    getattr(before, "st_ino", None)) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                    getattr(after, "st_ino", None)
                ):
                    raise ValueError("LOCAL_SOURCE_CONTEXT_SOURCE_CHANGED")
            except (ValueError, OSError, TypeError):
                public["status"] = "SOURCE_WINDOW_UNAVAILABLE"
                counts["unavailable"] += 1
            else:
                public["status"] = (
                    "SIMILAR_SIGNAL_OBSERVED_IN_SOURCE_WINDOW" if detected
                    else "NO_SIMILAR_SIGNAL_IN_SAMPLED_SOURCE_WINDOW"
                )
                counts[
                    "matched" if detected else "not_observed_in_source_window"
                ] += 1
        public["human_editorial_qa_completed"] = False
        public["auto_repair_allowed"] = False
        results.append(public)
    return {
        "requested_video_event_count": sum(
            item.get("kind") in {"blackout", "freeze"}
            for item in observations
        ),
        "sampled_event_count": len(selected),
        "summary": counts,
        "comparisons": results,
    }


def compare_local_source_context(
    *, plan: RenderPlan, review_sha256: str, video_sha256: str,
    observed: list[dict], scan_allowed: bool,
) -> dict[str, object]:
    """Optional create-only context; no context result approves an edit."""
    result: dict[str, object] = {
        "status": "LOCAL_SOURCE_CONTEXT_UNAVAILABLE",
        "sha256": "",
        "sampled_event_count": 0,
        "matched_count": 0,
        "no_match_count": 0,
        "ready": False,
        "production_ready": False,
        "publication_performed": False,
        "auto_repair_allowed": False,
    }
    if not scan_allowed:
        return result
    if (not isinstance(review_sha256, str) or not _HASH.fullmatch(review_sha256)
            or not isinstance(video_sha256, str)
            or not _HASH.fullmatch(video_sha256)):
        return result
    try:
        plan = RenderPlan.model_validate(plan.model_dump(mode="json"))
        video = _no_links(plan.output)
        if not video.is_file():
            raise ValueError("LOCAL_SOURCE_CONTEXT_BASE_MISSING")
        ledger = _no_links(video.with_name(f"{video.stem}.review.json"))
        target = _no_links(video.with_name(f"{video.stem}.source-context.json"))
        if not ledger.is_file() or target.exists() or ledger.stat().st_size > 65536:
            raise ValueError("LOCAL_SOURCE_CONTEXT_LEDGER_UNTRUSTED")
        raw_ledger = ledger.read_bytes()
        if hashlib.sha256(raw_ledger).hexdigest() != review_sha256:
            raise ValueError("LOCAL_SOURCE_CONTEXT_REVIEW_CHANGED")
        saved = json.loads(raw_ledger)
        if (saved.get("base_sha256") != video_sha256
                or not isinstance(saved.get("candidates"), list)
                or len(saved["candidates"]) != len(observed)
                or any(
                    not isinstance(a, dict)
                    or not isinstance(b, dict)
                    or a.get("kind") != b.get("kind")
                    or not isinstance(b.get("start_seconds"), (int, float))
                    or not isinstance(a.get("start_seconds"), (int, float))
                    or not math.isfinite(float(b["start_seconds"]))
                    or abs(a["start_seconds"] - b["start_seconds"]) > 0.002
                    for a, b in zip(saved["candidates"], observed, strict=True)
                )
                or saved.get("human_editorial_qa_completed") is not False
                or saved.get("ready") is not False
                or saved.get("production_ready") is not False
                or saved.get("publication_performed") is not False):
            raise ValueError("LOCAL_SOURCE_CONTEXT_REVIEW_INVALID")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("LOCAL_SOURCE_CONTEXT_FFMPEG_UNAVAILABLE")
        evidence = _compare(plan=plan, observations=observed, ffmpeg=ffmpeg)
        if not evidence["sampled_event_count"]:
            result["status"] = "LOCAL_SOURCE_CONTEXT_NO_VIDEO_EVENTS"
            return result
        evidence.update({
            "version": SOURCE_CONTEXT_VERSION,
            "render_plan_id": plan.id,
            "review_sha256": review_sha256,
            "base_sha256_from_review": saved["base_sha256"],
            "source_bytes_independently_rehashed": False,
            "source_bytes_verified_during_original_render": True,
            "interpretation": "UNLABELLED_SOURCE_WINDOW_SIGNAL_ONLY",
            "ready": False,
            "production_ready": False,
            "publication_performed": False,
            "auto_repair_allowed": False,
            "human_editorial_qa_completed": False,
        })
        encoded = (
            json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > 32768:
            raise ValueError("LOCAL_SOURCE_CONTEXT_TOO_LARGE")
        with tempfile.TemporaryDirectory(
            prefix=".holoclip-source-context-", dir=video.parent,
        ) as work:
            stage = Path(work) / "context.json"
            stage.write_bytes(encoded)
            if hashlib.sha256(ledger.read_bytes()).hexdigest() != review_sha256:
                raise ValueError("LOCAL_SOURCE_CONTEXT_REVIEW_CHANGED")
            try:
                os.link(stage, target)
            except OSError as exc:
                raise ValueError("LOCAL_SOURCE_CONTEXT_COMMIT_FAILED") from exc
        result.update({
            "status": "LOCAL_SOURCE_CONTEXT_SAMPLED_UNLABELLED",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sampled_event_count": evidence["sampled_event_count"],
            "matched_count": evidence["summary"]["matched"],
            "no_match_count": evidence["summary"]["not_observed_in_source_window"],
        })
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        pass
    return result
