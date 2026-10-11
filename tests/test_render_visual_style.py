"""Finite visual presentation decisions: no inferred zoom regions or shifted time."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment
from holoclip.render_plan import LocalRenderSource
from holoclip import render_plan
from holoclip.render_visual_style import (
    CutStyle, VISUAL_STYLE_VERSION, audio_suffix, select_cut_styles,
    style_evidence, video_suffix,
)


def _timeline(*, zoom=True, hint="dip_to_black", confidence=0.98,
              same_source=False):
    return ClipTimeline(
        id="test-editorial-visual-style",
        segments=[
            TimelineSegment(
                id="z", source_asset_id="a", source_start=0.1,
                source_end=2.5, output_order=0,
                purpose="payoff" if zoom else "setup",
                confidence=confidence,
            ),
            TimelineSegment(
                id="b", source_asset_id="a" if same_source else "b",
                source_start=0.2, source_end=2.6, output_order=1,
                purpose="reaction", confidence=confidence,
                transition_hint=hint,
            ),
        ],
        estimated_duration=4.8,
    )


def test_approved_zoom_and_explicit_cross_source_transition():
    timeline = _timeline()
    styles = select_cut_styles(timeline)
    assert styles == [
        CutStyle(zoom=True, fade_in=False, fade_out=True),
        CutStyle(zoom=True, fade_in=True, fade_out=False),
    ]
    summary = style_evidence(timeline)
    assert summary == {
        "version": VISUAL_STYLE_VERSION,
        "zoom_cut_count": 2,
        "dip_transition_count": 1,
        "total_cut_count": 2,
    }
    video = video_suffix(styles[0], 2.4)
    audio = audio_suffix(styles[0], 2.4)
    assert "zoompan=" in video
    assert "1.035" in video and "d=1:s=1280x720:fps=30" in video
    assert "fade=t=out:st=2.320000:d=0.08" in video
    assert "afade=t=out:st=2.320000:d=0.08" in audio


@pytest.mark.parametrize("timeline", [
    _timeline(zoom=False, hint=None),
    _timeline(hint="fade=t=in:st=0:d=1;unsafe"),
    _timeline(confidence=0.8),
    _timeline(same_source=True),
])
def test_guarded_rules_do_not_invent_transitions(timeline):
    styles = select_cut_styles(timeline)
    assert not any(style.fade_in or style.fade_out for style in styles)
    assert len(styles) == 2
    if timeline.segments[0].confidence < 0.92:
        assert not any(style.zoom for style in styles)


def test_short_segment_does_not_zoom_or_fade():
    timeline = ClipTimeline(
        id="short",
        segments=[TimelineSegment(
            id="short", source_asset_id="a", source_start=0,
            source_end=0.5, output_order=0, purpose="payoff",
            confidence=1.0, transition_hint="dip_to_black",
        )], estimated_duration=0.5,
    )
    assert select_cut_styles(timeline) == [CutStyle()]
    assert video_suffix(CutStyle(), 0.5) == ""
    assert audio_suffix(CutStyle(), 0.5) == ""


def test_render_plan_filter_graph_keeps_exact_same_timeline_and_limits(
    tmp_path, monkeypatch,
):
    timeline = _timeline()
    sources = []
    for name in ("a", "b"):
        source = tmp_path / f"{name}.mp4"
        source.write_bytes(b"synthetic bytes " * 30)
        sources.append(LocalRenderSource(
            asset=SourceAsset(
                id=name, platform="local", external_id=name,
                availability="available", access_class="local",
            ),
            path=source, local_render_approved=True,
        ))
    monkeypatch.setattr(render_plan, "_probe", lambda path: (4.0, True, True))
    monkeypatch.setattr(render_plan.shutil, "which", lambda name: name)
    plan = render_plan.compile_local_render_plan(
        timeline, sources, tmp_path / "styled.mp4",
    )
    args = render_plan._ffmpeg_command(plan, tmp_path / "temp.mp4")
    graph = args[args.index("-filter_complex") + 1]
    assert graph.count("zoompan=") == 2
    assert graph.count("fade=t=out") == 1
    assert graph.count("fade=t=in") == 1
    assert graph.count("afade=t=out") == 1
    assert graph.count("afade=t=in") == 1
    assert "concat=n=2:v=1:a=1" in graph
    assert "trim=start=0.100000:end=2.500000" in graph
    assert "trim=start=0.200000:end=2.600000" in graph
    assert "unsafe" not in graph
    assert plan.visual_style_version == VISUAL_STYLE_VERSION
    assert plan.ready is False
    other = plan.model_copy(update={"visual_style_version": "injected-v99"})
    with pytest.raises(ValueError, match="LOCAL_RENDER_UNTRUSTED_PLAN"):
        render_plan.render_local_preview(other)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe not installed",
)
def test_offline_real_ffmpeg_zoom_fade_and_audio_duration_no_shift(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    timeline = _timeline()
    sources = []
    for n, (name, color) in enumerate((("a", "blue"), ("b", "red"))):
        path = tmp_path / f"{name}.mp4"
        proc = subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=4",
            "-f", "lavfi", "-i",
            f"sine=frequency={400 + n*220}:sample_rate=48000:duration=4",
            "-c:v", "mpeg4", "-q:v", "6", "-c:a", "aac", "-shortest",
            str(path),
        ], capture_output=True, timeout=45, check=False)
        assert proc.returncode == 0, f"local synthetic source failed: {color}"
        sources.append(LocalRenderSource(
            asset=SourceAsset(
                id=name, platform="local", external_id=name,
                availability="available", access_class="local",
            ), path=path, local_render_approved=True,
        ))
    output = tmp_path / "finished.mp4"
    plan = render_plan.compile_local_render_plan(timeline, sources, output)
    result = render_plan.render_local_preview(plan)
    assert result["visual_style"] == style_evidence(timeline)
    assert abs(result["measured_duration"] - 4.8) <= 0.5
    assert output.is_file() and result["production_ready"] is False
    proc = subprocess.run([
        ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_read_frames",
        "-of", "json", str(output),
    ], capture_output=True, timeout=45, check=False)
    assert proc.returncode == 0
    stream = json.loads(proc.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1280, 720)
    assert 140 <= int(stream["nb_read_frames"]) <= 146
