"""Offline, fail-closed discovery of *complete* yt-dlp editing-video source caches.

Resolve canonical SourceAsset IDs against the existing SQLite repository and
local yt-dlp media.info.json receipts. Never probe/download a remote URL and
never treat bounded video sections or audio-only training caches as a full
original stream. Explicit operator permission is required before rendering.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from holoclip.domain import (
    AccessClass, ClipTimeline, SourceAsset, SourceAvailability, SourcePlatform,
)
from holoclip.render_plan import LocalRenderSource, _no_links, _source_file

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_FULL_VIDEO = re.compile(r"media\.(?:mp4|mkv|mov|webm)\Z", re.IGNORECASE)
MAX_META_BYTES = 1024 * 1024
MAX_REPOSITORY_BYTES = 128 * 1024 * 1024
_MAX_SOURCES = 32


def _canonical_assets(database: Path, required: set[str]) -> dict[str, SourceAsset]:
    """Read-only SQLite; never run schema creation/migration on operator data."""
    db = _no_links(database)
    if (not db.is_file() or db.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}
            or db.stat().st_size > MAX_REPOSITORY_BYTES):
        raise ValueError("LOCAL_RENDER_CANONICAL_REPOSITORY_UNAVAILABLE")
    uri = db.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            conn.execute("PRAGMA query_only = ON")
            got = {}
            for asset_id in sorted(required):
                row = conn.execute(
                    "SELECT payload FROM source_assets WHERE id=?", (asset_id,),
                ).fetchone()
                if row is None or not isinstance(row[0], str) or len(row[0]) > 65536:
                    raise ValueError("LOCAL_RENDER_CANONICAL_SOURCE_MISSING")
                asset = SourceAsset.model_validate_json(row[0])
                if asset.id != asset_id:
                    raise ValueError("LOCAL_RENDER_CANONICAL_SOURCE_MISMATCH")
                got[asset_id] = asset
    except (sqlite3.Error, UnicodeError, TypeError) as exc:
        raise ValueError("LOCAL_RENDER_CANONICAL_REPOSITORY_UNAVAILABLE") from exc
    return got


def _find_complete_cache(root: Path, external_id: str) -> Path:
    """Exact fixed directory layouts only; no broad recursive filesystem search."""
    paths = (
        root / external_id / "editing_video",
        root / "original" / external_id / "editing_video",
        root / "examples" / "sources" / external_id / "editing_video",
    )
    found = []
    for folder in paths:
        _no_links(folder)
        if not folder.is_dir():
            continue
        receipt = _no_links(folder / "media.info.json")
        if not receipt.is_file() or not 1 <= receipt.stat().st_size <= MAX_META_BYTES:
            raise ValueError("LOCAL_RENDER_CACHE_IDENTITY_UNVERIFIED")
        try:
            info = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError("LOCAL_RENDER_CACHE_IDENTITY_UNVERIFIED") from exc
        if (not isinstance(info, dict) or info.get("id") != external_id
                or str(info.get("extractor_key", "")).lower() != "youtube"):
            raise ValueError("LOCAL_RENDER_CACHE_IDENTITY_MISMATCH")
        media = []
        for entry in folder.iterdir():
            if not _FULL_VIDEO.fullmatch(entry.name):
                continue
            _no_links(entry)
            if entry.is_file():
                media.append(_source_file(entry))
        if len(media) != 1:
            raise ValueError("LOCAL_RENDER_COMPLETE_VIDEO_AMBIGUOUS_OR_MISSING")
        found.append(media[0])
    if len(found) != 1:
        raise ValueError(
            "LOCAL_RENDER_COMPLETE_VIDEO_AMBIGUOUS_OR_MISSING"
            if found else "LOCAL_RENDER_COMPLETE_VIDEO_CACHE_MISSING"
        )
    return found[0]


def resolve_cached_render_sources(
    timeline: ClipTimeline, *,
    database: Path = Path("data/holoclip.db"),
    cache_root: Path = Path("data/sources"),
    approve_local_preview: bool = False,
) -> list[LocalRenderSource]:
    """Produce exact LocalRenderSource mappings from previously saved media only.

    This is not authorization to publish or a clipping-rights determination.
    """
    if approve_local_preview is not True:
        raise ValueError("LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED")
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    required = {segment.source_asset_id for segment in timeline.segments}
    if not 1 <= len(required) <= _MAX_SOURCES:
        raise ValueError("LOCAL_RENDER_SOURCE_MAP_INCOMPLETE")
    root = _no_links(cache_root)
    if not root.is_dir():
        raise ValueError("LOCAL_RENDER_CACHE_ROOT_UNAVAILABLE")
    assets = _canonical_assets(database, required)
    result = []
    seen_paths: set[Path] = set()
    for asset_id in sorted(required):
        asset = assets[asset_id]
        # Only full original-video caches with verifiable YouTube IDs. Local
        # video aliases and human-made clips cannot be inferred from names.
        if (asset.platform != SourcePlatform.YOUTUBE
                or not _YOUTUBE_ID.fullmatch(asset.external_id)
                or asset.access_class not in {AccessClass.PUBLIC, AccessClass.AUTHORIZED}
                or asset.availability != SourceAvailability.AVAILABLE):
            raise ValueError("LOCAL_RENDER_SOURCE_NOT_ELIGIBLE_FOR_AUTOMAP")
        path = _find_complete_cache(root, asset.external_id)
        if path in seen_paths:
            raise ValueError("LOCAL_RENDER_DUPLICATE_CACHE_PATH")
        seen_paths.add(path)
        result.append(LocalRenderSource(
            asset=asset, path=path, local_render_approved=True,
        ))
    return result
