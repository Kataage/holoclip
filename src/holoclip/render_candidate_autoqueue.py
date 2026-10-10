"""One-time opt-in bridge from evaluated clip candidates to the durable source queue.

No media/network is accessed here. Only in-process candidate runs whose
canonical YouTube stream ID resolves to an eligible repository SourceAsset may
be enrolled. An enrollment requests *local technical preview acquisition*,
never publication or editorial/production approval.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from holoclip.domain import (
    AccessClass, SourceAvailability, SourcePlatform, SourceType,
    make_source_asset_id,
)
from holoclip.domain_adapters import candidate_to_clip_timeline
from holoclip.evaluation import EvaluationRun, make_candidate_id
from holoclip.render_plan import _no_links
from holoclip.render_source_queue import DEFAULT_QUEUE, enqueue_render_timeline
from holoclip.render_source_resolver import _YOUTUBE_ID, _canonical_assets

DEFAULT_AUTOQUEUE_CONFIG = Path("data/runtime/render-candidate-autoqueue.json")
_VERSION = "holoclip-candidate-autoqueue-v1"
_MAX_BYTES = 4096
_EXPECTED_KEYS = {
    "version", "queue", "database", "cache_root", "policy_file",
    "min_confidence", "max_candidates_per_run", "approval",
}


def _config_path(path: Path) -> Path:
    checked = _no_links(path)
    if checked.suffix.lower() != ".json":
        raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_PATH_INVALID")
    return checked


def _validated(data: object) -> dict:
    if not isinstance(data, dict) or set(data) != _EXPECTED_KEYS:
        raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED")
    if (data["version"] != _VERSION
            or data["approval"] != "local-preview-only"
            or type(data["min_confidence"]) not in (float, int)
            or not math.isfinite(data["min_confidence"])
            or not 0.75 <= data["min_confidence"] <= 1
            or type(data["max_candidates_per_run"]) is not int
            or not 1 <= data["max_candidates_per_run"] <= 3):
        raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED")
    for name in ("queue", "database", "cache_root", "policy_file"):
        raw = data[name]
        if (not isinstance(raw, str) or not raw or len(raw) > 1024
                or any(ord(char) < 32 for char in raw)):
            raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED")
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_PATH_INVALID")
        _no_links(path)
        if name == "queue" and path.suffix.lower() not in {
            ".db", ".sqlite", ".sqlite3",
        }:
            raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_PATH_INVALID")
    return data


def read_candidate_autoqueue_config(
    path: Path = DEFAULT_AUTOQUEUE_CONFIG,
) -> dict | None:
    path = _config_path(path)
    if not path.exists():
        return None
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_BYTES:
        raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED")
    try:
        return _validated(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeError, OSError) as exc:
        raise ValueError("LOCAL_RENDER_AUTOQUEUE_CONFIG_UNTRUSTED") from exc


def setup_candidate_autoqueue(
    *, config: Path = DEFAULT_AUTOQUEUE_CONFIG,
    queue: Path = DEFAULT_QUEUE,
    database: Path = Path("data/holoclip.db"),
    cache_root: Path = Path("data/sources"),
    policy_file: Path = Path("data/policy/clip_rules.jsonl"),
    min_confidence: float = 0.85,
    max_candidates_per_run: int = 1,
    apply: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Preview by default. Explicit apply stores only nonsecret local settings."""
    config = _config_path(config)
    data = _validated({
        "version": _VERSION,
        "queue": str(_no_links(queue)),
        "database": str(_no_links(database)),
        "cache_root": str(_no_links(cache_root)),
        "policy_file": str(_no_links(policy_file)),
        "min_confidence": min_confidence,
        "max_candidates_per_run": max_candidates_per_run,
        "approval": "local-preview-only",
    })
    report = {
        "status": "LOCAL_RENDER_AUTOQUEUE_SETUP_PREVIEW",
        "configured": False, "changes_applied": False,
        "min_confidence": data["min_confidence"],
        "max_candidates_per_run": data["max_candidates_per_run"],
        "youtube_contacted": False,
        "ready": False, "production_ready": False,
    }
    if read_candidate_autoqueue_config(config) is not None:
        report["status"] = "LOCAL_RENDER_AUTOQUEUE_ALREADY_CONFIGURED"
        report["configured"] = True
        return report
    if not apply:
        return report
    if approve_local_preview is not True:
        raise ValueError("LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    config.parent.mkdir(parents=True, exist_ok=True)
    # Create-only hardlink commit. Never overwrite active operator consent.
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config.parent,
            prefix=".candidate-autoqueue-", suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = Path(tmp.name)
            json.dump(data, tmp, ensure_ascii=False, sort_keys=True)
        try:
            os.link(tmp_name, config)
        except FileExistsError as exc:
            raise ValueError("LOCAL_RENDER_AUTOQUEUE_ALREADY_CONFIGURED") from exc
    finally:
        if tmp_name is not None:
            tmp_name.unlink(missing_ok=True)
    report["status"] = "LOCAL_RENDER_AUTOQUEUE_CONFIGURED"
    report["configured"] = True
    report["changes_applied"] = True
    return report


def enroll_candidate_run(
    run: EvaluationRun, *, config: Path = DEFAULT_AUTOQUEUE_CONFIG,
) -> dict:
    """Enroll bounded, unflagged top candidates without a handwritten timeline.

    Runs come from Holoclip's actual analyze/discover process after inference.
    A stream ID without an exact canonical video record cannot trigger
    external media acquisition. No raw transcript or candidate prose is logged.
    """
    configured = read_candidate_autoqueue_config(config)
    report = {
        "status": "LOCAL_RENDER_AUTOQUEUE_DISABLED" if configured is None
                  else "LOCAL_RENDER_AUTOQUEUE_NO_ELIGIBLE_CANDIDATES",
        "enrolled_count": 0, "already_enrolled_count": 0,
        "skipped_count": len(run.candidates),
        "youtube_contacted": False, "media_downloaded": False,
        "ready": False, "production_ready": False,
    }
    if configured is None:
        return report
    if (not _YOUTUBE_ID.fullmatch(run.stream_id)
            or len(run.candidates) > 20
            or len(run.candidates) == 0):
        report["status"] = "LOCAL_RENDER_AUTOQUEUE_SOURCE_UNVERIFIED"
        return report
    # A malformed EvaluationRun is not a safe origin for any unattended
    # acquisition, even when later candidates in it look plausible.
    if (sorted(item.rank for item in run.candidates)
            != list(range(1, len(run.candidates) + 1))
            or len({item.id for item in run.candidates}) != len(run.candidates)
            or any(item.id != make_candidate_id(run.stream_id, item.candidate)
                   for item in run.candidates)):
        report["status"] = "LOCAL_RENDER_AUTOQUEUE_RUN_UNTRUSTED"
        return report
    asset_id = make_source_asset_id("youtube", run.stream_id)
    try:
        asset = _canonical_assets(Path(configured["database"]), {asset_id})[asset_id]
    except ValueError:
        report["status"] = "LOCAL_RENDER_AUTOQUEUE_SOURCE_UNVERIFIED"
        return report
    if (
        asset.platform != SourcePlatform.YOUTUBE
        or asset.external_id != run.stream_id
        or asset.source_type not in {SourceType.STREAM, SourceType.VIDEO}
        or asset.availability != SourceAvailability.AVAILABLE
        or asset.access_class not in {AccessClass.PUBLIC, AccessClass.AUTHORIZED}
    ):
        report["status"] = "LOCAL_RENDER_AUTOQUEUE_SOURCE_UNVERIFIED"
        return report
    used_ids: set[str] = set()
    count = 0
    for item in sorted(run.candidates, key=lambda c: c.rank):
        candidate = item.candidate
        if (
            count >= configured["max_candidates_per_run"]
            or item.rank < 1
            or item.id != make_candidate_id(run.stream_id, candidate)
            or item.id in used_ids
            or candidate.warnings
            or candidate.confidence < configured["min_confidence"]
            or candidate.end - candidate.start > 600
            or (asset.duration is not None and
                candidate.end > asset.duration + 0.001)
        ):
            continue
        used_ids.add(item.id)
        try:
            timeline = candidate_to_clip_timeline(asset_id, candidate)
            if (len(timeline.segments) > 32
                    or not 0 < timeline.estimated_duration <= 600):
                continue
            receipt = enqueue_render_timeline(
                timeline,
                queue=Path(configured["queue"]),
                database=Path(configured["database"]),
                cache_root=Path(configured["cache_root"]),
                policy_file=Path(configured["policy_file"]),
                apply=True, approve_local_preview=True,
            )
        except ValueError:
            # Never enqueue a corrupted cut plan or ambiguous local source.
            continue
        if receipt["status"] == "LOCAL_RENDER_QUEUE_ENROLLED":
            report["enrolled_count"] += 1
            count += 1
        elif receipt["status"] == "LOCAL_RENDER_QUEUE_ALREADY_ENROLLED":
            report["already_enrolled_count"] += 1
            count += 1
        elif receipt["status"] == "LOCAL_RENDER_QUEUE_ALREADY_CACHED":
            count += 1
    report["skipped_count"] = len(run.candidates) - count
    report["status"] = (
        "LOCAL_RENDER_AUTOQUEUE_ENROLLED"
        if report["enrolled_count"] else
        "LOCAL_RENDER_AUTOQUEUE_ALREADY_PROCESSED"
        if count else "LOCAL_RENDER_AUTOQUEUE_NO_ELIGIBLE_CANDIDATES"
    )
    return report
