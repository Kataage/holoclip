"""No-network missing-source planning and single gated original acquisition."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment, make_source_asset_id
from holoclip.providers.source.base import AcquisitionProfile, SourceMetadata
from holoclip.providers.source.yt_dlp import YtDlpSourceProvider
from holoclip.repository import SQLiteCanonicalRepository
from holoclip import render_source_acquisition as acquisition


VID1 = "abcDEFgh012"
VID2 = "zYxWvuTS345"


def _case(tmp_path):
    db = tmp_path / "canonical.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    assets = []
    with SQLiteCanonicalRepository(db) as repo:
        for video in (VID1, VID2):
            asset = SourceAsset(
                id=make_source_asset_id("youtube", video),
                platform="youtube", external_id=video, source_type="stream",
                canonical_url=f"https://www.youtube.com/watch?v={video}",
                access_class="public", availability="available",
            )
            repo.upsert_source_asset(asset)
            assets.append(asset)
    timeline = ClipTimeline(
        id="ordered-candidate",
        segments=[
            TimelineSegment(
                id="seg0", source_asset_id=assets[1].id,
                source_start=5, source_end=10, output_order=0,
            ),
            TimelineSegment(
                id="seg1", source_asset_id=assets[0].id,
                source_start=3, source_end=7, output_order=1,
            ),
        ],
        estimated_duration=9,
    )
    return db, cache, timeline, assets


def _fake_provider(
    *, source_duration=100.0, description="", live_status="not_live",
    external_id=VID2, fail_probe=False,
):
    calls = []
    class Provider:
        lan_coordinator = object()
        pacer = object()
        def probe(self, url):
            calls.append(("probe", url))
            if fail_probe:
                raise ValueError("PRIVATE_EXTERNAL_ERROR")
            return SourceMetadata(
                provider="yt-dlp", url=url, external_id=external_id,
                title="public archive", description=description,
                duration=source_duration, live_status=live_status,
                availability="public",
            )
        def acquire(self, url, root, *, profile, max_video_duration_seconds,
                    max_video_filesize_mb):
            calls.append(("acquire", url, profile, max_video_duration_seconds,
                          max_video_filesize_mb))
            path = root / external_id / "editing_video"
            path.mkdir(parents=True, exist_ok=True)
            (path / "media.info.json").write_text(json.dumps({
                "id": external_id, "extractor_key": "Youtube",
            }))
            media = path / "media.mkv"
            media.write_bytes(b"synthetic local video bytes for test")
            return SimpleNamespace(
                metadata=SimpleNamespace(external_id=external_id),
                reused=False, media_path=media,
            )
    return Provider(), calls


def test_default_preview_lists_missing_in_timeline_order_without_network(tmp_path, monkeypatch):
    db, root, timeline, assets = _case(tmp_path)
    monkeypatch.setattr(
        acquisition, "YtDlpSourceProvider",
        lambda: pytest.fail("preview must never instantiate network provider"),
    )
    before = db.read_bytes()
    result = acquisition.acquire_one_missing_render_source(
        timeline, database=db, cache_root=root,
    )
    assert result["status"] == "LOCAL_RENDER_MISSING_SOURCES_PREVIEW"
    assert result["missing_source_asset_ids"] == [assets[1].id, assets[0].id]
    assert result["cached_count"] == 0
    assert result["missing_count"] == 2
    assert result["source_downloaded"] is False
    assert result["youtube_contact_attempted"] is False
    assert result["ready"] is False
    assert db.read_bytes() == before
    assert list(root.iterdir()) == []


def test_unapproved_execution_stops_before_network_or_cache_write(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root, execute=True,
        )
    assert not list(root.iterdir())


def test_missing_shared_lan_coordination_stops_before_metadata(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    provider, calls = _fake_provider()
    provider.lan_coordinator = None
    with pytest.raises(ValueError, match="LOCAL_RENDER_SHARED_LAN_PACING_REQUIRED"):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root,
            execute=True, approve_local_preview=True, provider=provider,
        )
    assert calls == []


def test_one_confirmed_download_and_second_source_left_for_later(tmp_path):
    db, root, timeline, assets = _case(tmp_path)
    provider, calls = _fake_provider()
    result = acquisition.acquire_one_missing_render_source(
        timeline, database=db, cache_root=root,
        execute=True, approve_local_preview=True, provider=provider,
    )
    assert result["status"] == "LOCAL_RENDER_ONE_FULL_VIDEO_ACQUIRED_NOT_PRODUCTION_READY"
    assert result["acquired_count"] == 1
    assert result["remaining_missing_count"] == 1
    assert result["ready"] is False
    assert calls[0] == ("probe", f"https://www.youtube.com/watch?v={VID2}")
    assert calls[1][2] == AcquisitionProfile.EDITING_VIDEO
    assert calls[1][3:] == (14400, 512)
    assert len(calls) == 2
    assert (root / VID2 / "editing_video" / "media.mkv").is_file()
    assert not (root / VID1).exists()
    again = acquisition.acquire_one_missing_render_source(
        timeline, database=db, cache_root=root,
    )
    assert again["cached_count"] == 1
    assert again["missing_source_asset_ids"] == [assets[0].id]


def test_existing_cache_cannot_be_overwritten_when_receipt_untrusted(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    folder = root / VID2 / "editing_video"
    folder.mkdir(parents=True)
    item = folder / "media.mkv"
    item.write_bytes(b"DO_NOT_DELETE_OLD_CACHE")
    provider, calls = _fake_provider()
    with pytest.raises(ValueError, match="LOCAL_RENDER_CACHE_IDENTITY_UNVERIFIED"):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root,
            execute=True, approve_local_preview=True, provider=provider,
        )
    assert calls == []
    assert item.read_bytes() == b"DO_NOT_DELETE_OLD_CACHE"


@pytest.mark.parametrize(("metadata", "code"), [
    ({"source_duration": None}, "LOCAL_RENDER_SOURCE_METADATA_NOT_ELIGIBLE"),
    ({"source_duration": 14401}, "LOCAL_RENDER_SOURCE_METADATA_NOT_ELIGIBLE"),
    ({"live_status": "is_live"}, "LOCAL_RENDER_SOURCE_METADATA_NOT_ELIGIBLE"),
    ({"external_id": VID1}, "LOCAL_RENDER_SOURCE_METADATA_NOT_ELIGIBLE"),
    ({"description": "切り抜き禁止"}, "LOCAL_RENDER_POLICY_NOT_ALLOWED"),
])
def test_fresh_metadata_and_no_clipping_policy_block_download(
    tmp_path, metadata, code,
):
    db, root, timeline, _ = _case(tmp_path)
    provider, calls = _fake_provider(**metadata)
    with pytest.raises(ValueError, match=code):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root,
            execute=True, approve_local_preview=True, provider=provider,
        )
    assert all(row[0] != "acquire" for row in calls)


def test_local_registry_range_block_prevents_video_download(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    policy = tmp_path / "policy.jsonl"
    policy.write_text(json.dumps({
        "id": "do-not-cut-5", "decision": "block", "reason": "no clipping",
        "operations": ["render"], "source_id": VID2,
        "start": 5, "end": 8,
    }) + "\n")
    provider, calls = _fake_provider()
    with pytest.raises(ValueError, match="LOCAL_RENDER_RANGE_POLICY_NOT_ALLOWED"):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root, policy_file=policy,
            execute=True, approve_local_preview=True, provider=provider,
        )
    assert all(row[0] != "acquire" for row in calls)


def test_policy_corruption_stops_before_remote_metadata(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    policy = tmp_path / "policy.jsonl"
    policy.write_text("{bad rules}")
    provider, calls = _fake_provider()
    with pytest.raises(ValueError):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root, policy_file=policy,
            execute=True, approve_local_preview=True, provider=provider,
        )
    assert not calls


@pytest.mark.parametrize("bad", [
    {"max_duration_seconds": 14401},
    {"max_duration_seconds": 10},
    {"max_filesize_mb": 2048},
    {"max_filesize_mb": 0},
    {"max_duration_seconds": True},
])
def test_invalid_budget_fails_in_preview(tmp_path, bad):
    db, root, timeline, _ = _case(tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_INVALID_ACQUISITION_BUDGET"):
        acquisition.acquire_one_missing_render_source(
            timeline, database=db, cache_root=root, **bad,
        )


def test_bounded_yt_dlp_args_use_existing_paced_run_and_no_refresh(tmp_path, monkeypatch):
    source_id = VID1
    url = f"https://www.youtube.com/watch?v={source_id}"
    provider = YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
    )
    monkeypatch.setattr(provider, "probe", lambda _url: SourceMetadata(
        provider="yt-dlp", url=url, external_id=source_id,
        title="source", duration=100, availability="public",
    ))
    found = []
    def fake_run(args, **kwargs):
        found.append((args, kwargs))
        root = Path(args[args.index("--paths") + 1])
        root.mkdir(parents=True, exist_ok=True)
        path = root / "media.mkv"
        path.write_bytes(b"binary fixture")
        (root / "media.info.json").write_text(json.dumps({
            "id": source_id, "extractor_key": "Youtube",
        }))
        return subprocess.CompletedProcess(args, 0, stdout=str(path), stderr="")
    monkeypatch.setattr(provider, "_run_yt_dlp", fake_run)
    result = provider.acquire(
        url, tmp_path, profile=AcquisitionProfile.EDITING_VIDEO,
        max_video_duration_seconds=120, max_video_filesize_mb=64,
    )
    assert result.reused is False
    args, kw = found[0]
    assert args[args.index("--max-filesize") + 1] == "64M"
    assert args[args.index("--match-filter") + 1] == "duration <= 120"
    assert kw["retry_with_fresh_runtime"] is False
    assert kw["timeout_seconds"] <= 3600


def test_bounded_provider_rejects_too_long_without_downloading(tmp_path, monkeypatch):
    provider = YtDlpSourceProvider(
        executable="yt-dlp", js_runtime="node", manage_updates=False,
    )
    monkeypatch.setattr(provider, "probe", lambda url: SourceMetadata(
        provider="yt-dlp", url=url, external_id=VID1,
        title="stream", duration=200,
    ))
    monkeypatch.setattr(
        provider, "_run_yt_dlp",
        lambda *a, **kw: pytest.fail("metadata duration disallows full download"),
    )
    with pytest.raises(ValueError, match="LOCAL_RENDER_SOURCE_DURATION_OVER_BUDGET"):
        provider.acquire(
            f"https://www.youtube.com/watch?v={VID1}", tmp_path,
            profile=AcquisitionProfile.EDITING_VIDEO,
            max_video_duration_seconds=120, max_video_filesize_mb=64,
        )


def test_cli_no_download_by_default_and_requires_execute_permission(tmp_path):
    db, root, timeline, _ = _case(tmp_path)
    t = tmp_path / "timeline.json"
    t.write_text(timeline.model_dump_json())
    args = [
        "render-local-source-acquire", "--timeline", str(t),
        "--database", str(db), "--cache-root", str(root),
    ]
    dry = CliRunner().invoke(cli.app, args)
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["status"] == "LOCAL_RENDER_MISSING_SOURCES_PREVIEW"
    blocked = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert blocked.exit_code == 6
    assert json.loads(blocked.stdout)["reason"] == "LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"
