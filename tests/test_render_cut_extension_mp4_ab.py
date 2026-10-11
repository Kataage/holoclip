"""Technical-only local MP4 A/B trials; no internet or FFmpeg in CI."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_cut_extension_mp4_ab as ab
from holoclip import render_plan
from holoclip.corpus_growth import CorpusGrowthState
from test_render_cut_extension_trials import _case as source_case, _cli as source_cli
from test_render_ready_queue import VIDEO


def _inputs(tmp_path):
    params, receipt, timeline, media, path = source_case(tmp_path)
    return params, receipt, timeline, media, path


def _prepare(params, **kwargs):
    return ab.prepare_cut_extension_mp4_trial(**params, **kwargs)


def _args(params, *other):
    return ["render-cut-extension-mp4-trial", *source_cli(params)[1:], *other]


def _offline_render(monkeypatch):
    monkeypatch.setattr(ab, "_probe", lambda p: (6.0, True, True))
    monkeypatch.setattr(render_plan, "_probe", lambda p: (100.0, True, True))
    policies = []
    monkeypatch.setattr(
        ab, "_offline_render_policy",
        lambda timeline, sources, policy: policies.append(
            (timeline.estimated_duration, len(sources), policy)
        ),
    )
    calls = []
    def fake_renderer(plan):
        calls.append(plan)
        payload = b"synthetic local trial MP4 with verified A/V" * 100
        plan.output.write_bytes(payload)
        return {
            "status": "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY",
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "measured_duration": plan.expected_duration,
        }
    monkeypatch.setattr(ab, "render_local_preview", fake_renderer)
    return calls, policies


def test_dry_run_creates_no_files_and_derives_one_change_only(tmp_path):
    params, receipt, timeline, media, transcript = _inputs(tmp_path)
    original = media.read_bytes()
    prepared = _prepare(params)
    result = ab.run_cut_extension_mp4_trial(prepared)
    assert result["status"] == "LOCAL_EXTENSION_AB_TRIAL_NOT_RENDERED"
    assert prepared["job_id"] == receipt["job_id"]
    assert result["trial_action"] in {"EXTEND_END", "EXTEND_START"}
    assert result["baseline_mp4_filename"] == media.name
    assert result["source_candidate_is_in_sample_not_quality_gold"] is True
    assert result["technical_qa_not_editorial_qa"] is True
    assert result["human_quality_improvement_verified"] is False
    assert result["ready"] is False and result["auto_adoption_allowed"] is False
    assert not prepared["output"].exists() and not prepared["receipt"].exists()
    assert media.read_bytes() == original
    assert len(prepared["trial"].segments) == len(timeline.segments)
    assert prepared["trial"].timeline_version == timeline.timeline_version + 1
    actual = sorted(prepared["trial"].segments, key=lambda s: s.output_order)
    unchanged = sorted(timeline.segments, key=lambda s: s.output_order)
    changed = [i for i, (a, b) in enumerate(zip(actual, unchanged))
               if (a.source_start, a.source_end) !=
               (b.source_start, b.source_end)]
    assert changed == [result["source_cut_index"]]
    index = changed[0]
    assert actual[index].id != unchanged[index].id
    assert actual[index].source_segment_ids == []
    assert actual[index].confidence == 0.0
    assert actual[index].transition_hint is None
    assert actual[index].purpose.value == "other"
    output = json.dumps(result, ensure_ascii=False)
    assert VIDEO not in output
    assert "切れていた続きの反応" not in output
    assert str(tmp_path) not in output
    assert transcript.exists()


def test_user_approval_required_before_any_video_write(tmp_path, monkeypatch):
    params, *_ = _inputs(tmp_path)
    prepared = _prepare(params)
    def deny(*a, **k):
        raise AssertionError("should never call render without approval")
    monkeypatch.setattr(ab, "render_local_preview", deny)
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        ab.run_cut_extension_mp4_trial(prepared, execute=True)
    assert not prepared["output"].exists()


def test_execute_renders_isolated_mp4_and_creates_source_sha_receipt(tmp_path, monkeypatch):
    params, receipt, _, media, source = _inputs(tmp_path)
    original = media.read_bytes()
    source_before = source.read_bytes()
    prepared = _prepare(params)
    calls, policies = _offline_render(monkeypatch)
    output = ab.run_cut_extension_mp4_trial(
        prepared, execute=True, approve_local_preview=True,
    )
    assert output["status"] == "LOCAL_EXTENSION_AB_RENDERED_TECHNICAL_ONLY"
    assert len(calls) == len(policies) == 1
    assert policies[0][0] == prepared["trial"].estimated_duration
    assert policies[0][1] == 1
    assert prepared["output"].exists()
    assert prepared["receipt"].exists()
    assert output["baseline_mp4_sha256_reverified"] == hashlib.sha256(original).hexdigest()
    assert output["trial_mp4_sha256"] == hashlib.sha256(
        prepared["output"].read_bytes()
    ).hexdigest()
    assert output["measured_duration_difference_seconds"] == pytest.approx(
        prepared["trial"].estimated_duration - 6
    )
    assert output["trial_subtitle_evaluation"] == "NOT_MEASURED"
    assert output["trial_audio_quality_vs_baseline"] == "NOT_MEASURED"
    assert output["trial_av_qa"] == "LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY"
    assert output["publication_performed"] is False
    assert output["auto_adoption_allowed"] is False
    assert output["ready"] is False
    assert media.read_bytes() == original
    assert source.read_bytes() == source_before
    stored = json.loads(prepared["receipt"].read_text(encoding="utf-8"))
    assert stored == output
    assert "trial_timeline" not in output
    assert VIDEO not in json.dumps(stored)
    assert str(tmp_path) not in json.dumps(stored)
    # Neither original queue state nor original QA checksum is altered.
    with sqlite3.connect(params["queue"]) as conn:
        state = conn.execute(
            "SELECT state,output_sha256 FROM render_jobs WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()
    assert state == ("QA_PASSED", hashlib.sha256(original).hexdigest())
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE_OR_EXISTS"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert media.read_bytes() == original


def test_existing_sidecar_preflights_before_ffmpeg(tmp_path, monkeypatch):
    params, *_ = _inputs(tmp_path)
    prepared = _prepare(params)
    prepared["receipt"].write_text("private existing receipt", encoding="utf-8")
    calls, _ = _offline_render(monkeypatch)
    with pytest.raises(ValueError, match="RECEIPT_EXISTS_OR_UNSAFE"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not calls
    assert prepared["receipt"].read_text() == "private existing receipt"


def test_existing_mp4_is_never_overwritten(tmp_path, monkeypatch):
    params, *_ = _inputs(tmp_path)
    prepared = _prepare(params)
    prepared["output"].write_bytes(b"already existing video")
    calls, _ = _offline_render(monkeypatch)
    with pytest.raises(ValueError, match="OUTPUT_UNSAFE_OR_EXISTS"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not calls
    assert prepared["output"].read_bytes() == b"already existing video"


def test_modified_original_baseline_refuses_comparison_before_ffmpeg(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    prepared = _prepare(params)
    media.write_bytes(b"modified original bytes")
    calls, _ = _offline_render(monkeypatch)
    with pytest.raises(ValueError, match="BASELINE_BYTES_CHANGED"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not calls
    assert not prepared["output"].exists()


def test_original_baseline_video_or_audio_missing_refuses_trial(tmp_path, monkeypatch):
    params, *_ = _inputs(tmp_path)
    prepared = _prepare(params)
    calls, _ = _offline_render(monkeypatch)
    monkeypatch.setattr(ab, "_probe", lambda _: (6.0, True, False))
    with pytest.raises(ValueError, match="BASELINE_AV_CHANGED"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert not calls


def test_source_policy_block_stops_trial_while_original_stays_intact(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    prepared = _prepare(params)
    _offline_render(monkeypatch)
    monkeypatch.setattr(
        ab, "_offline_render_policy",
        lambda *a: (_ for _ in ()).throw(
            ValueError("LOCAL_RENDER_AUTO_QA_RANGE_POLICY_NOT_ALLOWED")
        ),
    )
    with pytest.raises(ValueError, match="POLICY_NOT_ALLOWED"):
        ab.run_cut_extension_mp4_trial(
            prepared, execute=True, approve_local_preview=True,
        )
    assert media.exists()
    assert not prepared["output"].exists()


@pytest.mark.parametrize("bad", [
    "baseline_start", "baseline_end", "different_cut",
    "wrong_side", "over_budget", "outside_index",
])
def test_trial_rejects_invalid_ranges_or_semantically_stale_cuts(tmp_path, bad):
    params, *_ = _inputs(tmp_path)
    prepared = _prepare(params)
    proposal = {
        "baseline_source_intervals": [
            {"source_start_seconds": s.source_start, "source_end_seconds": s.source_end}
            for s in sorted(prepared["original"]["baseline_timeline"].segments,
                            key=lambda x: x.output_order)
        ],
        "trial_source_intervals": [
            {"source_start_seconds": s.source_start, "source_end_seconds": s.source_end}
            for s in sorted(prepared["trial"].segments, key=lambda x: x.output_order)
        ],
        "source_cut_index": prepared["source_cut_index"],
        "trial_action": prepared["trial_action"],
    }
    index = proposal["source_cut_index"]
    if bad == "baseline_start":
        proposal["baseline_source_intervals"][0]["source_start_seconds"] += 1
    elif bad == "baseline_end":
        proposal["baseline_source_intervals"][0]["source_end_seconds"] += 1
    elif bad == "different_cut":
        other = 1 - index
        proposal["trial_source_intervals"][other]["source_start_seconds"] += .5
    elif bad == "wrong_side":
        proposal["trial_action"] = (
            "EXTEND_END" if prepared["trial_action"] == "EXTEND_START"
            else "EXTEND_START"
        )
    elif bad == "over_budget":
        proposal["trial_source_intervals"][index]["source_end_seconds"] = 500
    elif bad == "outside_index":
        proposal["source_cut_index"] = 999
    with pytest.raises(ValueError):
        ab._trial_timeline(prepared["original"]["baseline_timeline"], proposal)


def test_missing_or_holdout_protected_corpus_never_builds_trial(tmp_path):
    params, *_ = _inputs(tmp_path)
    params["growth_state"].unlink()
    with pytest.raises(ValueError, match="NO_VERIFIED_PROPOSAL"):
        _prepare(params)
    other = tmp_path / "other"
    other.mkdir()
    params, *_ = _inputs(other)
    params["growth_state"].write_text(
        CorpusGrowthState(holdout_stream_ids=[VIDEO]).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NO_VERIFIED_PROPOSAL"):
        _prepare(params)


def test_cli_dry_run_zero_network_zero_file_writes(tmp_path, monkeypatch):
    params, _, _, media, _ = _inputs(tmp_path)
    original = media.read_bytes()
    class DenyAI:
        def __init__(self, *a, **kw):
            pytest.fail("No AI should start")
    monkeypatch.setattr(cli, "MeshLLMClient", DenyAI)
    result = CliRunner().invoke(cli.app, _args(params))
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["status"] == "LOCAL_EXTENSION_AB_TRIAL_NOT_RENDERED"
    assert data["ready"] is False
    assert media.read_bytes() == original


def test_cli_requires_approval_even_with_execute(tmp_path, monkeypatch):
    params, *_ = _inputs(tmp_path)
    monkeypatch.setattr(
        ab, "render_local_preview",
        lambda _: pytest.fail("No FFmpeg without approval"),
    )
    result = CliRunner().invoke(cli.app, _args(params, "--execute"))
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "LOCAL_EXTENSION_AB_BLOCKED"
