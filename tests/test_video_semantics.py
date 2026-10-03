import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from holoclip.providers.cloud import CloudAdapters
from holoclip.providers.cost import CostPolicyError
from holoclip.providers.mesh import ExecutionMode, Route
from holoclip.video_measurement import measure_window, select_frame_indices
from holoclip.video_semantics import (
    MeasuredFrame, MeasuredVideoWindow, semantic_evidence, validate_observation,
)


def measured_window():
    return MeasuredVideoWindow(id="w1", source_asset_id="a1", source_start=10, source_end=12,
        video_path="bounded.mp4", media_sha256="sha", frames=(MeasuredFrame(id="f1", shot_id="s1",
        pts_seconds=0.5, locator="f1.png", checksum="sha"),), contact_sheet_paths=(),
        transcript={"t1": "驚いた"}, extraction_version="test")


def observation(**changes):
    return {"window_id": "w1", "events": [{"kind": "avatar_reaction", "description": "Surprise",
            "evidence_ids": ["f1"], "confidence": 0.9, "focus_candidates": ["avatar"], **changes}],
            "unknowns": []}


@pytest.mark.parametrize("changes", [{"start": 1}, {"bbox": [0, 0, 10, 10]},
                                      {"crop": [0, 0, 10, 10]}, {"tracking_path": []},
                                      {"evidence_ids": ["invented"]}, {"evidence_ids": ["t1"]}])
def test_vlm_cannot_author_measurements(changes):
    with pytest.raises(ValueError):
        validate_observation(observation(**changes), measured_window())


def test_training_runtime_evidence_uses_window_time_and_blocks_diagnostic():
    result = {"output": observation(), "provenance": {"provider": "fixture", "diagnostic_only": False}}
    evidence = semantic_evidence(measured_window(), result)[0]
    assert (evidence.start, evidence.end) == (10, 12)
    assert evidence.payload["pixel_target"] == "NOT_MEASURED"
    result["provenance"]["diagnostic_only"] = True
    with pytest.raises(ValueError, match="diagnostic"):
        semantic_evidence(measured_window(), result)


def test_sampling_retains_each_shot_and_dense_reactions():
    pts = [i / 4 for i in range(12)]
    result = select_frame_indices(pts, [(0, 4), (4, 8), (8, 12)], [1.5])
    assert {1, 5, 9} <= set(result)
    assert {4, 5, 6, 7, 8} <= set(result)


async def test_groq_multimage_http_and_paid_payload_guard(tmp_path, free_cost_guard):
    image = tmp_path / "frame.png"
    image.write_bytes(b"fixture")
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        adapter = CloudAdapters(free_cost_guard, {"groq": "test-key"}, client=c)
        route = Route(entry=free_cost_guard.registry["groq:qwen/qwen3.8-27b"],
                      mode=ExecutionMode.MULTIFRAME, qualification=())
        assert await adapter.call_json(route, "test", images=[image], public=True) == {"ok": True}
        assert seen[0]["messages"][0]["content"][1]["type"] == "image_url"
        with pytest.raises(CostPolicyError, match="Too many"):
            await adapter.call_json(route, "test", images=[image] * 4, public=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_real_measurement_manifest_cache_and_contact_tiles(tmp_path):
    pytest.importorskip("scenedetect")
    source = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=10:duration=2", "-c:v", "libx264", "-threads", "1",
                    str(source)], check=True)
    first = measure_window(source, tmp_path / "measurements", asset_id="a", start=0, end=2,
                           dense_times=[1])
    second = measure_window(source, tmp_path / "measurements", asset_id="a", start=0, end=2,
                            dense_times=[1])
    assert first == second
    tiles = json.loads(Path(first.contact_sheet_paths[0]).with_suffix(".json").read_text())
    assert {t["frame_id"] for t in tiles} == {f.id for f in first.frames}
    assert first.frames[0].pts_seconds >= 0
