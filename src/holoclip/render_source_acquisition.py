"""Preview-first missing original-video acquisition via existing paced yt-dlp.

Full original-stream time coordinates are required by RenderPlan. Never use
bounded sections or audio-only training media as source-timeline substitutes.
No yt-dlp calls in inspection mode; real acquisition must reserve existing
shared-LAN and local pacers inside YtDlpSourceProvider, one source per command.
"""
from __future__ import annotations

import math
from pathlib import Path

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata,
    evaluate_source_ranges, load_policy_rules,
)
from holoclip.domain import AccessClass, ClipTimeline, SourceAvailability, SourcePlatform
from holoclip.providers.source.base import AcquisitionProfile
from holoclip.providers.source.yt_dlp import YtDlpSourceProvider
from holoclip.render_plan import _no_links
from holoclip.render_source_resolver import (
    _YOUTUBE_ID, _canonical_assets, _find_complete_cache,
)

_DEFAULT_DB = Path("data/holoclip.db")
_DEFAULT_CACHE = Path("data/sources")
_DEFAULT_POLICY = Path("data/policy/clip_rules.jsonl")
MAX_DURATION_SECONDS = 14400
MAX_FILESIZE_MB = 512
_MAX_SOURCES = 32
_MISSING = "LOCAL_RENDER_FULL_VIDEO_MISSING"
_PRESENT = "LOCAL_RENDER_FULL_VIDEO_VERIFIED_LOCALLY"


def _inventory(
    *, timeline: ClipTimeline, database: Path,
    cache_root: Path, max_duration_seconds: int,
    max_filesize_mb: int,
) -> tuple[list[tuple[str, str]], dict]:
    if (
        type(max_duration_seconds) is not int or not 60 <= max_duration_seconds <= 14400
        or type(max_filesize_mb) is not int or not 64 <= max_filesize_mb <= 1024
    ):
        raise ValueError("LOCAL_RENDER_INVALID_ACQUISITION_BUDGET")
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    ordered_ids = list(dict.fromkeys(
        segment.source_asset_id
        for segment in sorted(timeline.segments, key=lambda s: s.output_order)
    ))
    if not 1 <= len(ordered_ids) <= _MAX_SOURCES:
        raise ValueError("LOCAL_RENDER_SOURCE_MAP_INCOMPLETE")
    root = _no_links(cache_root)
    assets = _canonical_assets(database, set(ordered_ids))
    missing: list[tuple[str, str]] = []
    counts = {"cached": 0, "missing": 0}
    for aid in ordered_ids:
        asset = assets[aid]
        if (
            asset.platform != SourcePlatform.YOUTUBE
            or not _YOUTUBE_ID.fullmatch(asset.external_id)
            or asset.availability != SourceAvailability.AVAILABLE
            or asset.access_class not in {AccessClass.PUBLIC, AccessClass.AUTHORIZED}
        ):
            raise ValueError("LOCAL_RENDER_SOURCE_NOT_ELIGIBLE_FOR_ACQUISITION")
        try:
            _find_complete_cache(root, asset.external_id)
        except ValueError as exc:
            # A missing cache can be downloaded once. A malformed, tampered,
            # ambiguous, or linked cache must first be investigated offline.
            if str(exc) != "LOCAL_RENDER_COMPLETE_VIDEO_CACHE_MISSING":
                raise
            missing.append((aid, asset.external_id))
        else:
            counts["cached"] += 1
    counts["missing"] = len(missing)
    return missing, counts


def acquire_one_missing_render_source(
    timeline: ClipTimeline,
    *,
    database: Path = _DEFAULT_DB,
    cache_root: Path = _DEFAULT_CACHE,
    policy_file: Path = _DEFAULT_POLICY,
    max_duration_seconds: int = MAX_DURATION_SECONDS,
    max_filesize_mb: int = MAX_FILESIZE_MB,
    execute: bool = False,
    approve_local_preview: bool = False,
    provider: YtDlpSourceProvider | None = None,
) -> dict[str, object]:
    """Return safe codes; execute at most ONE authorized download.

    No live site access without explicit execute and approval, an already
    configured authenticated LAN coordinator and the provider's local pacer.
    """
    missing, counts = _inventory(
        timeline=timeline, database=database, cache_root=cache_root,
        max_duration_seconds=max_duration_seconds,
        max_filesize_mb=max_filesize_mb,
    )
    report: dict[str, object] = {
        "status": ("LOCAL_RENDER_ALL_FULL_SOURCES_PRESENT"
                   if not missing else "LOCAL_RENDER_MISSING_SOURCES_PREVIEW"),
        "source_count": counts["cached"] + counts["missing"],
        "cached_count": counts["cached"],
        "missing_count": counts["missing"],
        "missing_source_asset_ids": [aid for aid, _ in missing],
        "max_acquisitions_this_run": 1,
        "acquired_count": 0,
        "yt_dlp_called": False,
        "youtube_contact_attempted": False,
        "source_downloaded": False,
        "production_ready": False,
        "ready": False,
    }
    if not execute or not missing:
        return report
    if approve_local_preview is not True:
        raise ValueError("LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    target_id, video_id = missing[0]
    assets = _canonical_assets(database, {target_id})
    if assets[target_id].external_id != video_id:
        raise ValueError("LOCAL_RENDER_CANONICAL_SOURCE_CHANGED")
    url = f"https://www.youtube.com/watch?v={video_id}"
    # No real network before verifying trusted shared-IP coordination.
    provider = provider if provider is not None else YtDlpSourceProvider()
    if provider.lan_coordinator is None or provider.pacer is None:
        raise ValueError("LOCAL_RENDER_SHARED_LAN_PACING_REQUIRED")
    # Pre-parse policy registry before any remote request. Absence of the
    # policy file is the repo's existing empty-registry behavior, NOT a waiver
    # of publisher-specific copyright policy.
    _no_links(policy_file)
    rules = load_policy_rules(policy_file)
    report["yt_dlp_called"] = True
    report["youtube_contact_attempted"] = True
    observed = provider.probe(url)
    if (
        observed.external_id != video_id
        or observed.duration is None
        or not math.isfinite(observed.duration)
        or not 0 < observed.duration <= max_duration_seconds
        or observed.live_status in {"is_live", "is_upcoming", "post_live"}
        or observed.availability in {"private", "premium_only", "subscriber_only"}
    ):
        raise ValueError("LOCAL_RENDER_SOURCE_METADATA_NOT_ELIGIBLE")
    policy = evaluate_source_metadata(
        observed, rules=rules, operation=PolicyOperation.RENDER,
    )
    if policy.decision != PolicyDecision.ALLOW:
        raise ValueError("LOCAL_RENDER_POLICY_NOT_ALLOWED")
    ranges = [
        (segment.source_start, segment.source_end)
        for segment in timeline.segments
        if segment.source_asset_id == target_id
    ]
    if not ranges or max(end for _, end in ranges) > observed.duration + 0.001:
        raise ValueError("LOCAL_RENDER_SOURCE_RANGE_UNAVAILABLE")
    range_policy = evaluate_source_ranges(
        observed, ranges=ranges, rules=rules, operation=PolicyOperation.RENDER,
        deferred_findings=policy.findings,
    )
    if range_policy.decision != PolicyDecision.ALLOW:
        raise ValueError("LOCAL_RENDER_RANGE_POLICY_NOT_ALLOWED")
    # Important: provider.acquire() uses _run_yt_dlp -> LAN shared lease
    # FIRST, local SQLite process gate SECOND, native conservative sleeps,
    # rate-limit cooldowns and a finite subprocess timeout. No direct yt-dlp.
    root = _no_links(cache_root)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=False)
    got = provider.acquire(
        url, root, profile=AcquisitionProfile.EDITING_VIDEO,
        max_video_duration_seconds=max_duration_seconds,
        max_video_filesize_mb=max_filesize_mb,
    )
    if got.metadata.external_id != video_id or got.reused:
        raise ValueError("LOCAL_RENDER_ACQUIRED_IDENTITY_UNVERIFIED")
    # Check new file using the *same* strict existing-cache identity lookup
    # as the RenderPlan resolver. A suspicious/corrupt receipt must not be
    # accepted simply because the yt-dlp process exited successfully.
    checked = _find_complete_cache(root, video_id)
    if checked != _no_links(got.media_path):
        raise ValueError("LOCAL_RENDER_ACQUIRED_CACHE_UNVERIFIED")
    report["status"] = "LOCAL_RENDER_ONE_FULL_VIDEO_ACQUIRED_NOT_PRODUCTION_READY"
    report["acquired_count"] = 1
    report["source_downloaded"] = True
    report["remaining_missing_count"] = len(missing) - 1
    return report
