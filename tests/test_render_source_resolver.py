"""Offline auto-mapping of canonical YouTube source IDs to complete local video caches."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_source_resolver as resolver
from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment, make_source_asset_id
from holoclip.repository import SQLiteCanonicalRepository


VID_A = "AbCdEfG1234"
VID_B = "XyZtUvW5678"


def _fixture(tmp_path: Path):
    database = tmp_path / "holoclip.db"
    cache_root = tmp_path / "source-cache"
    cache_root.mkdir()
    sources = []
    with SQLiteCanonicalRepository(database) as repo:
        for name, vid in (("a", VID_A), ("b", VID_B)):
            asset = SourceAsset(
                id=make_source_asset_id("youtube", vid),
                platform="youtube", external_id=vid,
                canonical_url=f"https://www.youtube.com/watch?v={vid}",
                access_class="public", availability="available",
                source_type="stream",
            )
            repo.upsert_source_asset(asset)
            folder = cache_root / "original" / vid / "editing_video"
            folder.mkdir(parents=True)
            (folder / "media.info.json").write_text(json.dumps({
                "id": vid, "extractor_key": "Youtube",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            }), encoding="utf-8")
            (folder / "media.mkv").write_bytes((name + " local offline data").encode() * 100)
            sources.append((asset, folder))
    timeline = ClipTimeline(
        id="multi-source-local-timeline",
        segments=[
            TimelineSegment(id="cut-1", source_asset_id=sources[0][0].id,
                            source_start=1, source_end=3, output_order=0),
            TimelineSegment(id="cut-2", source_asset_id=sources[1][0].id,
                            source_start=2, source_end=3, output_order=1),
        ],
        estimated_duration=3,
    )
    return database, cache_root, sources, timeline


def _resolve(db, root, timeline):
    return resolver.resolve_cached_render_sources(
        timeline, database=db, cache_root=root, approve_local_preview=True,
    )


def test_auto_map_two_distinct_streams_by_canonical_asset_id(tmp_path):
    database, cache_root, pairs, timeline = _fixture(tmp_path)
    before = database.read_bytes()
    resolved = _resolve(database, cache_root, timeline)
    assert {s.asset.id for s in resolved} == {a.id for a, _ in pairs}
    assert {s.path.name for s in resolved} == {"media.mkv"}
    assert all(s.path.parts[-2] == "editing_video" for s in resolved)
    assert all(s.local_render_approved is True for s in resolved)
    assert database.read_bytes() == before


def test_requires_explicit_local_preview_permission(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"):
        resolver.resolve_cached_render_sources(timeline, database=db, cache_root=root)
    assert (root / "original" / VID_A / "editing_video" / "media.mkv").exists()


def test_no_download_if_only_training_audio_or_bounded_sections(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    full = root / "original" / VID_A / "editing_video"
    (root / "original" / VID_A / "training_audio").mkdir()
    (root / "original" / VID_A / "training_audio" / "media.webm").write_bytes(b"audio only")
    (root / "original" / VID_A / "editing_video_sections" / "hash").mkdir(parents=True)
    (root / "original" / VID_A / "editing_video_sections" / "hash" / "media.mkv").write_bytes(b"cropped video")
    (full / "media.mkv").unlink()
    full.rmdir() if not any(full.iterdir()) else None
    with pytest.raises(ValueError, match="LOCAL_RENDER_COMPLETE_VIDEO_AMBIGUOUS_OR_MISSING"):
        _resolve(db, root, timeline)


@pytest.mark.parametrize("edit", [
    lambda receipt, media: receipt.update({"id": VID_B}),
    lambda receipt, media: receipt.update({"extractor_key": "Dailymotion"}),
    lambda receipt, media: receipt.pop("id"),
    lambda receipt, media: receipt.update({"id": ["unexpected"]}),
])
def test_cache_receipt_mismatch_fails_closed(tmp_path, edit):
    db, root, _, timeline = _fixture(tmp_path)
    folder = root / "original" / VID_A / "editing_video"
    path = folder / "media.info.json"
    info = json.loads(path.read_text())
    edit(info, folder / "media.mkv")
    path.write_text(json.dumps(info))
    with pytest.raises(ValueError, match="LOCAL_RENDER_CACHE_IDENTITY_MISMATCH"):
        _resolve(db, root, timeline)


def test_duplicate_complete_cache_is_ambiguous(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    original = root / "original" / VID_A / "editing_video"
    extra = root / VID_A / "editing_video"
    extra.mkdir(parents=True)
    for name in ("media.info.json", "media.mkv"):
        (extra / name).write_bytes((original / name).read_bytes())
    with pytest.raises(ValueError, match="LOCAL_RENDER_COMPLETE_VIDEO_AMBIGUOUS_OR_MISSING"):
        _resolve(db, root, timeline)


def test_multiple_media_candidates_are_rejected(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    folder = root / "original" / VID_A / "editing_video"
    (folder / "media.mp4").write_bytes(b"another local stream")
    with pytest.raises(ValueError, match="LOCAL_RENDER_COMPLETE_VIDEO_AMBIGUOUS_OR_MISSING"):
        _resolve(db, root, timeline)


def test_source_symlink_rejected_without_following(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    folder = root / "original" / VID_A / "editing_video"
    old = folder / "media.mkv"
    old.unlink()
    destination = tmp_path / "external.mkv"
    destination.write_bytes(b"private untouched")
    try:
        old.symlink_to(destination)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    with pytest.raises(ValueError, match="LOCAL_RENDER_LINKED_PATH"):
        _resolve(db, root, timeline)
    assert destination.read_bytes() == b"private untouched"


def test_missing_database_must_not_create_file(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    db.unlink()
    with pytest.raises(ValueError, match="LOCAL_RENDER_CANONICAL_REPOSITORY_UNAVAILABLE"):
        _resolve(db, root, timeline)
    assert not db.exists()


def test_unknown_canonical_asset_blocks_filename_guessing(tmp_path):
    db, root, _, timeline = _fixture(tmp_path)
    timeline.segments[0].source_asset_id = "invented-video-asset"
    with pytest.raises(ValueError, match="LOCAL_RENDER_CANONICAL_SOURCE_MISSING"):
        _resolve(db, root, timeline)


def test_private_or_unavailable_asset_blocks_auto_mapping(tmp_path):
    db, root, pairs, timeline = _fixture(tmp_path)
    with SQLiteCanonicalRepository(db) as repo:
        repo.upsert_source_asset(pairs[0][0].model_copy(
            update={"availability": "unavailable"}
        ))
    with pytest.raises(ValueError, match="LOCAL_RENDER_SOURCE_NOT_ELIGIBLE_FOR_AUTOMAP"):
        _resolve(db, root, timeline)


def test_autosource_cli_no_manual_manifest_and_no_output(tmp_path, monkeypatch):
    db, root, _, timeline = _fixture(tmp_path)
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(timeline.model_dump_json())
    monkeypatch.setattr("holoclip.render_plan._probe", lambda p: (6.0, True, True))
    output = tmp_path / "preview.mp4"
    args = [
        "render-local-preview", "--timeline", str(timeline_path),
        "--auto-sources", "--database", str(db),
        "--cache-root", str(root), "--approve-local-preview",
        "--output", str(output),
    ]
    outcome = CliRunner().invoke(cli.app, args)
    assert outcome.exit_code == 0, outcome.output
    doc = json.loads(outcome.stdout)
    assert doc["status"] == "LOCAL_RENDER_PLAN_VALIDATED_NOT_EXECUTED"
    assert doc["source_count"] == 2 and doc["segment_count"] == 2
    assert doc["ready"] is False
    assert not output.exists()


def test_autosource_cli_requires_permission_and_rejects_mixed_manifest(tmp_path, monkeypatch):
    db, root, _, timeline = _fixture(tmp_path)
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(timeline.model_dump_json())
    base = [
        "render-local-preview", "--timeline", str(timeline_path),
        "--auto-sources", "--database", str(db), "--cache-root", str(root),
        "--output", str(tmp_path / "out.mp4"),
    ]
    result = CliRunner().invoke(cli.app, base)
    assert result.exit_code == 6
    assert json.loads(result.stdout)["reason"] == "LOCAL_RENDER_EXPLICIT_PREVIEW_APPROVAL_REQUIRED"
    assert not (tmp_path / "out.mp4").exists()
    result = CliRunner().invoke(cli.app, [*base, "--approve-local-preview",
                                          "--sources", str(tmp_path / "in.json")])
    assert result.exit_code == 6
    assert json.loads(result.stdout)["reason"] == "LOCAL_RENDER_SELECT_EXACTLY_ONE_SOURCE_MODE"
