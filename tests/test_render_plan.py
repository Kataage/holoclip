"""Local-only RenderPlan and FFmpeg clip assembly QA; no YouTube or paid API."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment
from holoclip import render_plan as render


def _fixture(tmp_path):
    media = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.mp4"
        path.write_bytes((f"mock video and audio {name}" * 100).encode())
        media.append(render.LocalRenderSource(
            asset=SourceAsset(
                id=name, platform="local", external_id=name,
                access_class="local", availability="available",
            ),
            path=path, local_render_approved=True,
        ))
    timeline = ClipTimeline(
        id="source-proven-timeline",
        segments=[
            TimelineSegment(
                id="cut-a", source_asset_id="a",
                source_start=0.5, source_end=1.5,
                output_order=0, purpose="setup",
            ),
            TimelineSegment(
                id="cut-b", source_asset_id="b",
                source_start=0.25, source_end=1.25,
                output_order=1, purpose="payoff",
            ),
        ],
        estimated_duration=2,
    )
    return timeline, media, tmp_path / "new-preview.mp4"


def _probe_stub(monkeypatch):
    monkeypatch.setattr(render, "_probe", lambda path: (3.0, True, True))


def test_compile_multisource_local_plan_is_no_write_and_stable(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    first = render.compile_local_render_plan(timeline, sources, output)
    again = render.compile_local_render_plan(timeline, sources, output)
    assert first.id == again.id
    assert len(first.sources) == 2
    assert first.expected_duration == 2
    assert first.ready is False and first.production_ready is False
    assert not output.exists()
    assert all(len(item.sha256) == 64 for item in first.sources)
    monkeypatch.setattr(render.shutil, "which", lambda cmd: cmd)
    cmd = render._ffmpeg_command(first, tmp_path / "temp.mp4")
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in filt
    assert "[0:v:0]trim=start=0.500000:end=1.500000" in filt
    assert "[1:v:0]trim=start=0.250000:end=1.250000" in filt
    assert "-nostdin" in cmd and "-n" in cmd
    assert "-protocol_whitelist" in cmd
    assert "http" not in filt


def test_explicit_local_permission_and_available_source_required(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    sources[0].local_render_approved = False
    with pytest.raises(ValueError, match="LOCAL_RENDER_PERMISSION_UNVERIFIED"):
        render.compile_local_render_plan(timeline, sources, output)
    sources[0].local_render_approved = True
    sources[0].asset.availability = "unavailable"
    with pytest.raises(ValueError, match="LOCAL_RENDER_PERMISSION_UNVERIFIED"):
        render.compile_local_render_plan(timeline, sources, output)


def test_unmapped_or_duplicate_sources_rejected(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_SOURCE_MAP_INCOMPLETE"):
        render.compile_local_render_plan(timeline, sources[:1], output)
    with pytest.raises(ValueError, match="LOCAL_RENDER_SOURCE_MAP_INVALID"):
        render.compile_local_render_plan(timeline, [sources[0], sources[0]], output)


@pytest.mark.parametrize("bounds", [(0.2, 3.5), (1.0, 1.0)])
def test_unmeasured_ranges_rejected(tmp_path, monkeypatch, bounds):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    timeline.segments[0].source_start = bounds[0]
    timeline.segments[0].source_end = bounds[1]
    if bounds[1] > bounds[0]:
        timeline.estimated_duration = bounds[1] - bounds[0] + 1
    with pytest.raises(ValueError):
        render.compile_local_render_plan(timeline, sources, output)


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    output.write_bytes(b"keep old bytes")
    with pytest.raises(ValueError, match="LOCAL_RENDER_OUTPUT_UNSAFE_OR_EXISTS"):
        render.compile_local_render_plan(timeline, sources, output)
    assert output.read_bytes() == b"keep old bytes"


def test_local_source_symlink_is_blocked(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    linked = tmp_path / "linked.mp4"
    try:
        linked.symlink_to(sources[0].path)
    except OSError:
        pytest.skip("symlinks unavailable")
    sources[0].path = linked
    with pytest.raises(ValueError, match="LOCAL_RENDER_LINKED_PATH"):
        render.compile_local_render_plan(timeline, sources, output)


def test_source_modified_after_compile_never_renders(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    plan = render.compile_local_render_plan(timeline, sources, output)
    sources[0].path.write_bytes(b"source replaced")
    monkeypatch.setattr(
        render, "_call",
        lambda *a, **k: pytest.fail("FFmpeg must never run with changed source"),
    )
    with pytest.raises(ValueError, match="LOCAL_RENDER_SOURCE_CHANGED"):
        render.render_local_preview(plan)
    assert not output.exists()


def test_plan_fingerprint_tampering_fails_before_ffmpeg(tmp_path, monkeypatch):
    _probe_stub(monkeypatch)
    timeline, sources, output = _fixture(tmp_path)
    plan = render.compile_local_render_plan(timeline, sources, output)
    changed = plan.model_copy(update={"id": "attacker"})
    monkeypatch.setattr(
        render, "_call",
        lambda *a, **k: pytest.fail("FFmpeg must not launch"),
    )
    with pytest.raises(ValueError, match="LOCAL_RENDER_PLAN_FINGERPRINT_CHANGED"):
        render.render_local_preview(changed)


def test_mock_ffmpeg_basic_qa_and_no_overwrite_commit(tmp_path, monkeypatch):
    timeline, sources, output = _fixture(tmp_path)
    _probe_stub(monkeypatch)
    plan = render.compile_local_render_plan(timeline, sources, output)
    def fake_probe(path):
        return (2.0 if path.name == "preview.mp4" else 3.0, True, True)
    monkeypatch.setattr(render, "_probe", fake_probe)
    monkeypatch.setattr(render.shutil, "which", lambda cmd: cmd)
    def fake_render(args, **kw):
        Path(args[-1]).write_bytes(b"mock finished MP4 bytes")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(render, "_call", fake_render)
    result = render.render_local_preview(plan)
    assert result["status"] == "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"
    assert result["source_count"] == 2 and result["segment_count"] == 2
    assert result["ready"] is False and result["publication_performed"] is False
    assert output.read_bytes() == b"mock finished MP4 bytes"
    with pytest.raises(ValueError, match="LOCAL_RENDER_OUTPUT_UNSAFE_OR_EXISTS"):
        render.render_local_preview(plan)


def test_bad_output_qa_never_commits_file(tmp_path, monkeypatch):
    timeline, sources, output = _fixture(tmp_path)
    _probe_stub(monkeypatch)
    plan = render.compile_local_render_plan(timeline, sources, output)
    def fake_probe(path):
        return (8.0 if path.name == "preview.mp4" else 3.0, True, True)
    monkeypatch.setattr(render, "_probe", fake_probe)
    monkeypatch.setattr(render.shutil, "which", lambda cmd: cmd)
    def fake_render(args, **kw):
        Path(args[-1]).write_bytes(b"bad output")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(render, "_call", fake_render)
    with pytest.raises(ValueError, match="LOCAL_RENDER_BASIC_QA_FAILED"):
        render.render_local_preview(plan)
    assert not output.exists()
    assert not list(tmp_path.glob(".holoclip-render-*"))


def test_failed_ffmpeg_uses_fixed_code_and_no_output(tmp_path, monkeypatch):
    timeline, sources, output = _fixture(tmp_path)
    _probe_stub(monkeypatch)
    plan = render.compile_local_render_plan(timeline, sources, output)
    monkeypatch.setattr(render.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(render, "_call", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=b"", stderr=b"token leaked in ffmpeg stderr",
    ))
    with pytest.raises(ValueError, match="^LOCAL_RENDER_FFMPEG_FAILED$"):
        render.render_local_preview(plan)
    assert not output.exists()


def test_cli_preview_only_and_execute_gating(tmp_path, monkeypatch):
    timeline, sources, output = _fixture(tmp_path)
    timeline_path = tmp_path / "timeline.json"
    mapping_path = tmp_path / "sources.json"
    timeline_path.write_text(timeline.model_dump_json(), encoding="utf-8")
    mapping_path.write_text(json.dumps([
        item.model_dump(mode="json") for item in sources
    ]), encoding="utf-8")
    _probe_stub(monkeypatch)
    args = [
        "render-local-preview", "--timeline", str(timeline_path),
        "--sources", str(mapping_path), "--output", str(output),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["status"] == "LOCAL_RENDER_PLAN_VALIDATED_NOT_EXECUTED"
    assert parsed["output_created"] is False and parsed["ready"] is False
    assert not output.exists()
    monkeypatch.setattr(
        cli, "render_local_preview",
        lambda plan: {"status": "LOCAL_RENDER_TEST_EXECUTED", "ready": False},
    )
    confirmed = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert confirmed.exit_code == 0
    assert json.loads(confirmed.stdout)["status"] == "LOCAL_RENDER_TEST_EXECUTED"


def test_cli_refuses_unauthorized_manifest(tmp_path, monkeypatch):
    timeline, sources, output = _fixture(tmp_path)
    timeline_path = tmp_path / "timeline.json"
    source_path = tmp_path / "sources.json"
    timeline_path.write_text(timeline.model_dump_json())
    manifest = [row.model_dump(mode="json") for row in sources]
    manifest[0]["local_render_approved"] = False
    source_path.write_text(json.dumps(manifest))
    _probe_stub(monkeypatch)
    result = CliRunner().invoke(cli.app, [
        "render-local-preview", "--timeline", str(timeline_path),
        "--sources", str(source_path), "--output", str(output), "--execute",
    ])
    assert result.exit_code == 6
    response = json.loads(result.stdout)
    assert response["status"] == "LOCAL_RENDER_BLOCKED"
    assert response["ready"] is False
    assert not output.exists()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg/ffprobe not installed",
)
def test_real_ffmpeg_synthetic_multisource_roundtrip(tmp_path):
    # Synthetic local A/V only. Never downloads, uses YouTube, or publishes.
    timeline, sources, output = _fixture(tmp_path)
    for idx, src in enumerate(sources):
        source_color = "blue" if idx == 0 else "red"
        args = [
            shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={source_color}:s=160x90:r=30:d=3",
            "-f", "lavfi", "-i", f"sine=frequency={440+idx*110}:duration=3",
            "-c:v", "mpeg4", "-q:v", "6", "-c:a", "aac",
            "-shortest", "-y", str(src.path),
        ]
        proc = subprocess.run(
            args, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30, check=False,
        )
        assert proc.returncode == 0, "synthetic source generation failed"
    plan = render.compile_local_render_plan(timeline, sources, output)
    outcome = render.render_local_preview(plan)
    assert outcome["status"] == "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"
    assert outcome["ready"] is False
    assert output.is_file()
    assert abs(outcome["measured_duration"] - 2.0) <= 0.5
