"""Single-command real-media holdout audit: schema, gates and stage wiring."""
import inspect
import json

from holoclip.examples import human_clip_identity
from holoclip.holdout_e2e_trial import (
    STAGES, _kwargs, run_reserved_holdout_e2e, summarize_observed_evidence,
)
from holoclip.holdout_preparation import _candidates
from holoclip.corpus_growth import CorpusGrowthState
from test_holdout_preparation import files


def make_runners(root, *, override=None, calls=None):
    called = calls if calls is not None else []
    runners = {}
    for stage in STAGES:
        def runner(provider, *, _stage=stage, **kwargs):
            called.append(_stage.name)
            clip_id = human_clip_identity(
                _candidates(CorpusGrowthState.model_validate_json(
                    kwargs["state_path"].read_text(encoding="utf-8")
                ))[0].clip_url
            )
            status = (override or {}).get(_stage.name, _stage.success)
            record = {"status": status}
            if _stage.name == "original_audio":
                record["anchors"] = [{"clip_start": 0, "clip_end": 6,
                                      "source_start": 40, "source_end": 46}]
            if _stage.name == "sampled_frame_links":
                record["group_observations"] = [{
                    "status": "PARTIAL_AUDIO_VISUAL_FRAME_LINKS",
                    "frame_links": [{}, {}],
                }]
            if _stage.name == "two_sided_cut_evidence":
                record["transitions"] = [{
                    "candidate": {"status": "MULTIMODAL_EDIT_JUMP_CANDIDATE"}
                }]
            if _stage.name == "partial_time_map":
                record["time_map"] = {
                    "status": "PARTIAL_LOCAL_INTERVAL_CANDIDATES",
                    "candidate_coverage_seconds": 2.,
                    "segments": [{"output_start": .5, "output_end": 2.5}],
                }
            path = root / _stage.report
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "version": _stage.version, "qualification_allowed": False,
                "event_split_verified": False, "reviewed_gold_available": False,
                "records": {clip_id: record},
            }), encoding="utf-8")
            return {"counts": {status: 1}, "report_path": str(path)}
        runners[stage.name] = runner
    return runners


def test_real_stage_signatures_and_root_paths_are_consistent(tmp_path):
    for stage in STAGES:
        supplied = _kwargs(stage.name, store=tmp_path / "examples",
                           state=tmp_path / "state", root=tmp_path / "root",
                           rules=[], max_clips=1, progress=None)
        params = inspect.signature(stage.execute).parameters
        assert all(key in params for key in supplied), stage.name
        assert supplied["output"] == (tmp_path / "root" if
            stage.name in ("finished_video", "original_audio")
            else tmp_path / "root" / {
                "source_video_pairs": "source_pairs",
                "sampled_visual_geometry": "visual_edits",
                "acoustic_cut_brackets": "cut_brackets",
                "source_audio_clocks": "source_clocks",
                "sampled_frame_links": "frame_links",
                "two_sided_cut_evidence": "cut_sides",
                "partial_time_map": "partial_time_maps",
            }[stage.name])


def test_one_command_runs_nine_stages_and_persists_observed_only_metrics(tmp_path):
    store, state, root = files(tmp_path)
    calls = []
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root, calls=calls),
    )
    assert report["status"] == "UNREVIEWED_MEASUREMENTS_COMPLETE"
    assert report["real_media_run_completed"] is True
    assert report["ready"] is False
    assert report["production_ready"] is False
    assert len(calls) == 9
    assert calls == [s.name for s in STAGES]
    summary = report["observations"]
    assert summary["all_nine_stages_measured_clips"] == 1
    assert summary["clips_with_unique_original_audio_matches"] == 1
    assert summary["sampled_source_finished_frame_links"] == 2
    assert summary["multimodal_cut_candidates"] == 1
    assert summary["local_time_map_candidate_segments"] == 1
    assert summary["candidate_coverage_seconds"] == 2.
    for field in (
        "editor_cut_precision", "editor_cut_recall",
        "native_timestamp_error_seconds", "event_holdout_accuracy",
    ):
        assert summary[field] is None
    saved = json.loads((root / "holdout-e2e-audit.json").read_text(encoding="utf-8"))
    assert saved["status"] == report["status"]
    assert len(saved["stages"]) == 9


def test_preflight_and_missing_reservations_never_touch_network(tmp_path):
    store, state, root = files(tmp_path)
    def no_runner(*args, **kwargs):
        raise AssertionError("Preflight must not call a source provider")
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, preflight_only=True,
        runners={stage.name: no_runner for stage in STAGES},
    )
    assert report["status"] == "PREFLIGHT_ONLY_NO_MEDIA_EXECUTED"
    assert report["real_media_run_completed"] is False
    assert report["stages"] == []
    missing = run_reserved_holdout_e2e(
        None, store=store, state_path=tmp_path / "not-found.json",
        root=root, policy_rules=[], max_clips=1,
        runners={stage.name: no_runner for stage in STAGES},
    )
    assert missing["status"] == "BLOCKED_MISSING_RESERVATION_STATE"
    assert missing["ready"] is False


def test_bad_audio_stage_prevents_all_downstream_calls(tmp_path):
    store, state, root = files(tmp_path)
    calls = []
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1,
        runners=make_runners(root, calls=calls, override={
            "original_audio": "INSUFFICIENT_UNIQUE_ANCHORS",
        }),
    )
    assert report["status"] == "BLOCKED_UPSTREAM_STAGE"
    assert report["blocked_stage"] == "original_audio"
    assert calls == ["finished_video", "original_audio"]
    assert not report["real_media_run_completed"]
    assert not (root / "partial_time_maps" / "holdout-partial-time-map.json").exists()


def test_forged_evaluation_promotion_fails_closed(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    original = runners["original_audio"]
    def poison(provider, **kwargs):
        result = original(provider, **kwargs)
        path = root / "holdout-audio-alignment.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["qualification_allowed"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        return result
    runners["original_audio"] = poison
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert report["status"] == "STAGE_FAILED"
    assert report["blocked_stage"] == "original_audio"
    assert report["error_class"] == "ValueError"
    assert report["ready"] is False


def test_exception_details_are_never_persisted(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    def crash(provider, **kwargs):
        raise RuntimeError("secret-api-key=private")
    runners["finished_video"] = crash
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert report["status"] == "STAGE_FAILED"
    assert report["error_class"] == "RuntimeError"
    text = (root / "holdout-e2e-audit.json").read_text(encoding="utf-8")
    assert "secret-api-key" not in text
    assert "private" not in text


def test_unknown_map_does_not_claim_accuracy_or_coverage(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    final = runners["partial_time_map"]
    def unknown(provider, **kwargs):
        data = final(provider, **kwargs)
        path = root / "partial_time_maps" / "holdout-partial-time-map.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        next(iter(doc["records"].values()))["time_map"] = {
            "status": "UNKNOWN_NO_SUPPORTED_LOCAL_INTERVALS",
            "segments": [], "unknown_intervals": [{"start": 0., "end": 15.}],
        }
        path.write_text(json.dumps(doc), encoding="utf-8")
        return data
    runners["partial_time_map"] = unknown
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert report["status"] == "UNREVIEWED_MEASUREMENTS_COMPLETE"
    assert report["observations"]["local_time_map_candidate_segments"] == 0
    assert report["observations"]["measured_but_no_supported_local_map_clips"] == 1
    assert report["observations"]["editor_cut_recall"] is None


def test_stage_report_versions_and_safety_flags_checked(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    path = root / "partial_time_maps" / "holdout-partial-time-map.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["version"] = "fake-promoted-version"
    path.write_text(json.dumps(doc), encoding="utf-8")
    clip_id = next(iter(doc["records"]))
    import pytest
    with pytest.raises(ValueError, match="Unsafe"):
        summarize_observed_evidence(root, {clip_id})
