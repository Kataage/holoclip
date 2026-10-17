"""Offline test of video-backed B2/B3 diagnostic benchmark preparation."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_visual_benchmark as visual
from test_render_b2_b3_blind_edge_association import _study


def _fixture(tmp_path, monkeypatch):
    state, _, _ = _study(tmp_path, monkeypatch)
    ctx = state["basis"]["context"]
    ctx["roles"]["b2"]["timeline"].estimated_duration = 20.0
    ctx["roles"]["b3"]["timeline"].estimated_duration = 20.5
    lengths = {
        str(ctx["roles"]["b2"]["final"]): 20.0,
        str(ctx["roles"]["b3"]["final"]): 20.5,
    }
    monkeypatch.setattr(visual, "probe_video", lambda src: {
        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        "format": {"duration": str(lengths[str(src)])},
    })
    invocations = []

    def fake_measure(src, root, *, asset_id, start, end, dense_times=None):
        invocations.append((str(src), start, end, asset_id, dense_times))
        output = root / "measured-window"
        output.mkdir(parents=True)
        (output / "measurement.json").write_text('{"synthetic":true}', encoding="utf-8")
        return SimpleNamespace(
            frames=[SimpleNamespace(id="f1")],
            contact_sheet_paths=[str(output / "sheet.png")],
            video_path=str(output / "bounded.mp4"),
        )

    monkeypatch.setattr(visual, "measure_window", fake_measure)
    monkeypatch.setattr(visual, "verified_window", lambda case: True)
    return state, invocations


def test_read_only_plan_covers_three_regions_per_actual_video(tmp_path, monkeypatch):
    state, requests = _fixture(tmp_path, monkeypatch)
    result = visual.prepare_visual_benchmark()
    assert result["status"] == "VISUAL_BENCH_READ_ONLY_PLAN"
    assert result["prepared_video_case_count"] == 6
    assert result["variant_windows"]["b2"]["duration_seconds"] == 20.0
    assert result["variant_windows"]["b3"]["duration_seconds"] == 20.5
    assert [r["region"] for r in result["variant_windows"]["b2"]["windows"]] == [
        "opening", "edit_edge", "closing",
    ]
    assert all(row["output_clock_not_common_source_clock"]
               for row in result["variant_windows"]["b3"]["windows"])
    assert not requests
    assert not list(tmp_path.glob("*visual-benchmark*"))
    assert result["all_cases_held_out"] is False
    assert result["independent_human_gold_verified"] is False
    assert result["actual_model_calls"] == 0


def test_create_requires_public_approval_and_env(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("HOLOCLIP_DATA_CLASS", raising=False)
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        visual.prepare_visual_benchmark(execute=True)
    with pytest.raises(ValueError, match="ENV_REQUIRED"):
        visual.prepare_visual_benchmark(
            execute=True, approve_public_media=True,
        )
    assert not list(tmp_path.glob("*visual-benchmark*"))


def test_opt_in_measurements_produce_in_sample_manifest_without_uploads(
    tmp_path, monkeypatch,
):
    state, calls = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    folder = tmp_path / "benchmark-case-output"
    output = visual.prepare_visual_benchmark(
        execute=True, approve_public_media=True, output_root=folder,
    )
    assert output["status"] == "VISUAL_BENCH_LOCAL_MEASUREMENTS_UNQUALIFIED"
    assert output["actual_model_calls"] == 0
    assert output["additional_downloads"] is False
    assert output["production_promotion_allowed"] is False
    assert len(calls) == 6
    assert all(end-start == 6 for _, start, end, _, _ in calls)
    assert all(dense == [3.0] for _, _, _, _, dense in calls)
    saved = json.loads((folder / "benchmark.json").read_text(encoding="utf-8"))
    cases = saved["cases"]
    assert len(cases) == 6
    assert saved["repeats"] == 1
    assert saved["training_clip_ids"] == [output["study_id"]]
    assert all(case["held_out"] is False and case["real_media"] is True
               and case["public_data"] is True for case in cases)
    assert all("candidate-" in case["id"] for case in cases)
    assert all("b2" not in case["id"] and "b3" not in case["id"] for case in cases)
    assert all(case["teacher_reference"] == "UNVERIFIED_NO_GOLD_REFERENCE"
               for case in cases)
    assert (folder / "status.json").is_file()
    assert output["automatic_winner"] is None
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        visual.prepare_visual_benchmark(
            execute=True, approve_public_media=True, output_root=folder,
        )


def test_mismatched_real_duration_or_missing_audio_blocks(tmp_path, monkeypatch):
    state, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(visual, "probe_video", lambda src: {
        "streams": [{"codec_type": "video"}],
        "format": {"duration": "20"},
    })
    with pytest.raises(ValueError, match="AUDIO_STREAM_MISSING"):
        visual.prepare_visual_benchmark()
    monkeypatch.setattr(visual, "probe_video", lambda src: {
        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        "format": {"duration": "150"},
    })
    with pytest.raises(ValueError, match="DURATION_MISMATCH"):
        visual.prepare_visual_benchmark()


def test_changed_source_or_untrusted_cut_is_rejected(tmp_path, monkeypatch):
    state, _ = _fixture(tmp_path, monkeypatch)
    state["basis"]["context"]["roles"]["b3"]["timeline"].segments[1].source_end = 43
    with pytest.raises(ValueError, match="NOT_SINGLE_EDGE_CHANGE"):
        visual.prepare_visual_benchmark()


def test_cli_denies_unapproved_and_returns_preflight(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    cli_args = ["render-b2-b3-visual-benchmark"]
    runner = CliRunner()
    result = runner.invoke(cli.app, cli_args)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "VISUAL_BENCH_READ_ONLY_PLAN"
    denied = runner.invoke(cli.app, [*cli_args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
