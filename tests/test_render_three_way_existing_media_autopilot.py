"""Bounded offline A/B/B2 autopilot, derived from the canonical doctor only."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_existing_media_autopilot as auto
from holoclip.render_cut_extension_finish_ab import _stage_paths
from test_render_three_way_prerequisites import _case, _fake_scan, _patch_measure
from test_render_cut_extension_mp4_ab import _args


def _run(options, *, execute=False, approval=False, steps=6):
    return auto.run_existing_media_autopilot(
        **options, execute=execute,
        approve_local_preview=approval, max_steps=steps,
    )


def test_default_is_strictly_read_only_and_no_media_creation(tmp_path, monkeypatch):
    options, prepared, mp4, asr = _case(tmp_path, monkeypatch)
    baseline = mp4.read_bytes(), asr.read_bytes()
    monkeypatch.setattr(
        auto, "_dispatch", lambda *a, **k: pytest.fail("preview executed a stage"),
    )
    result = _run(options)
    assert result["status"] == "LOCAL_AUTOPILOT_READ_ONLY_PREVIEW"
    assert result["total_executed_stage_count"] == 0
    assert result["jobs"][0]["initial_stage"] == "MEASURE_TECHNICAL"
    assert result["jobs"][0]["status"] == "LOCAL_AUTOPILOT_PREVIEW_ONLY"
    assert result["downloaded_video_count"] == 0
    assert result["remote_model_requests"] == 0
    assert result["ready"] is False and result["publication_performed"] is False
    assert not prepared["receipt"].exists()
    assert (mp4.read_bytes(), asr.read_bytes()) == baseline
    assert str(tmp_path) not in json.dumps(result)


def test_execution_measures_both_existing_local_stages_and_resumes_without_repeat(
    tmp_path, monkeypatch,
):
    options, plan, mp4, asr = _case(tmp_path, monkeypatch)
    before = mp4.read_bytes(), asr.read_bytes()
    fake_scan = _fake_scan(monkeypatch)
    fake_audio = _patch_measure(monkeypatch)
    result = _run(options, execute=True)
    case = result["jobs"][0]
    assert case["initial_stage"] == "MEASURE_TECHNICAL"
    assert case["executed_stages"] == ["MEASURE_TECHNICAL", "MEASURE_ACOUSTIC"]
    assert case["final_stage"] == "EVIDENCE_COMPLETE"
    assert case["status"] == "LOCAL_AUTOPILOT_TECHNICAL_EVIDENCE_COMPLETE_UNQUALIFIED"
    assert result["total_executed_stage_count"] == 2
    assert len(fake_scan) == 3 and len(fake_audio) >= 6
    assert plan["receipt"].is_file()
    assert (mp4.read_bytes(), asr.read_bytes()) == before
    monkeypatch.setattr(
        auto, "_dispatch", lambda *a, **k: pytest.fail("must not rerun finished QA"),
    )
    repeat = _run(options, execute=True)
    assert repeat["total_executed_stage_count"] == 0
    assert repeat["jobs"][0]["final_stage"] == "EVIDENCE_COMPLETE"
    assert repeat["ready"] is False


def test_total_step_budget_resumes_without_repeating_diagnostics(
    tmp_path, monkeypatch,
):
    options, plan, _, _ = _case(tmp_path, monkeypatch)
    scan_calls = _fake_scan(monkeypatch)
    acoustic_calls = _patch_measure(monkeypatch)
    first = _run(options, execute=True, steps=1)
    assert first["jobs"][0]["executed_stages"] == ["MEASURE_TECHNICAL"]
    assert first["jobs"][0]["final_stage"] == "MEASURE_ACOUSTIC"
    assert first["total_executed_stage_count"] == 1
    assert plan["receipt"].is_file()
    assert len(scan_calls) == 3 and len(acoustic_calls) == 0
    second = _run(options, execute=True, steps=1)
    assert second["jobs"][0]["executed_stages"] == ["MEASURE_ACOUSTIC"]
    assert second["jobs"][0]["final_stage"] == "EVIDENCE_COMPLETE"
    assert len(scan_calls) == 3 and len(acoustic_calls) >= 6


def test_render_approval_required_even_for_prepared_and_approved_unrelated_tasks(
    tmp_path, monkeypatch,
):
    options, prepared, _, _ = _case(tmp_path, monkeypatch)
    b2 = prepared["finished"]["b2"]
    stage_paths = _stage_paths(b2["output"])
    for stage in ("audio", "caption", "burnin"):
        stage_paths[stage].unlink()
    prepared["finished"]["finished_receipt"].unlink()
    original = b2["output"].read_bytes()
    monkeypatch.setattr(
        auto, "_dispatch", lambda *a, **k: pytest.fail("render without approval"),
    )
    refused = _run(options, execute=True)
    assert refused["jobs"][0]["initial_stage"] == "FINISH_B2"
    assert refused["jobs"][0]["status"] == "LOCAL_AUTOPILOT_RENDER_APPROVAL_REQUIRED"
    assert refused["total_executed_stage_count"] == 0
    assert b2["output"].read_bytes() == original


def test_stepwise_rechecks_stage_after_new_only_local_finishing(
    tmp_path, monkeypatch,
):
    options, prepared, _, _ = _case(tmp_path, monkeypatch)
    b2 = prepared["finished"]["b2"]
    paths = _stage_paths(b2["output"])
    originals = {
        path: path.read_bytes()
        for path in [prepared["finished"]["finished_receipt"], *[
            paths[k] for k in ("audio", "caption", "burnin") if paths[k].exists()
        ]]
    }
    for path in originals:
        path.unlink()
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    real_dispatch = auto._dispatch
    seen = []

    def fake_finish(stage, *, kwargs, approval):
        seen.append(stage)
        if stage == "FINISH_B2":
            assert approval is True
            for path, content in originals.items():
                assert not path.exists()
                path.write_bytes(content)
        else:
            real_dispatch(stage, kwargs=kwargs, approval=approval)

    monkeypatch.setattr(auto, "_dispatch", fake_finish)
    result = _run(options, execute=True, approval=True)
    assert seen == ["FINISH_B2", "MEASURE_TECHNICAL", "MEASURE_ACOUSTIC"]
    assert result["jobs"][0]["final_stage"] == "EVIDENCE_COMPLETE"
    assert result["jobs"][0]["stage_count"] == 3
    assert result["ready"] is False


def test_stale_or_concurrent_lock_not_removed_or_replaced(
    tmp_path, monkeypatch,
):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    lock = options["preview_root"] / (
        f".holoclip-three-way-autopilot-{options['job_id']}.lock"
    )
    content = b"belongs to another running local process"
    lock.write_bytes(content)
    result = _run(options, execute=True)
    assert result["jobs"][0]["status"] == "LOCAL_AUTOPILOT_INPUT_OR_LOCK_BLOCKED"
    assert result["total_executed_stage_count"] == 0
    assert lock.read_bytes() == content


def test_bad_existing_diagnostics_not_repaired_or_overwritten(
    tmp_path, monkeypatch,
):
    options, plan, _, _ = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    first = _run(options, execute=True, steps=1)
    assert first["jobs"][0]["final_stage"] == "MEASURE_ACOUSTIC"
    plan["receipt"].write_bytes(b"tampered diagnostic sidecar")
    previous = plan["receipt"].read_bytes()
    response = _run(options, execute=True)
    assert response["jobs"][0]["final_stage"] == "BLOCKED_UNVERIFIED"
    assert response["total_executed_stage_count"] == 0
    assert plan["receipt"].read_bytes() == previous


def test_invalid_or_missing_queue_does_not_create_any_video(tmp_path):
    missing = tmp_path / "absent.sqlite3"
    result = auto.run_existing_media_autopilot(queue=missing, execute=True)
    assert result["selected_job_count"] == 0
    assert result["total_executed_stage_count"] == 0
    assert not missing.exists()
    with pytest.raises(ValueError):
        auto.run_existing_media_autopilot(
            queue=missing, max_steps=7, execute=True,
        )


def test_dispatch_failure_stops_and_never_tries_later_stages(
    tmp_path, monkeypatch,
):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    fake = _fake_scan(monkeypatch)
    monkeypatch.setattr(
        auto, "_dispatch",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("transient decode failed")),
    )
    result = _run(options, execute=True)
    assert result["jobs"][0]["status"] == "LOCAL_AUTOPILOT_STAGE_FAILED_STOPPED"
    assert result["total_executed_stage_count"] == 0
    assert result["jobs"][0]["final_stage"] == "MEASURE_TECHNICAL"
    assert len(fake) == 0


def test_cli_explicit_execution_is_separate_from_preview(
    tmp_path, monkeypatch,
):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    _fake_scan(monkeypatch)
    _patch_measure(monkeypatch)
    monkeypatch.setattr(
        cli, "MeshLLMClient",
        lambda *a, **kw: pytest.fail("remote LLM forbidden"),
    )
    monkeypatch.setattr(
        cli, "MeshTranscriber",
        lambda *a, **kw: pytest.fail("remote ASR forbidden"),
    )
    argv = [
        "render-three-way-existing-media-autopilot",
        *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, argv)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["total_executed_stage_count"] == 0
    execution = CliRunner().invoke(cli.app, [*argv, "--execute"])
    assert execution.exit_code == 0, execution.stdout
    report = json.loads(execution.stdout)
    assert report["total_executed_stage_count"] == 2
    assert report["jobs"][0]["final_stage"] == "EVIDENCE_COMPLETE"
    assert report["ready"] is False and report["publication_performed"] is False
