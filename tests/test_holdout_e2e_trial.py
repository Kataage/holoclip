"""Single-command real-media holdout audit: schema, gates and stage wiring."""
import inspect
import json

from holoclip.examples import human_clip_identity
from holoclip.holdout_e2e_trial import (
    STAGES, _kwargs, run_all_reserved_holdout_batches, run_reserved_holdout_e2e,
    summarize_observed_evidence,
)
from holoclip.holdout_preparation import _candidates
from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from test_holdout_preparation import files


def make_runners(root, *, override=None, calls=None):
    called = calls if calls is not None else []
    runners = {}
    for stage in STAGES:
        def runner(provider, *, _stage=stage, **kwargs):
            called.append(_stage.name)
            selected = _candidates(CorpusGrowthState.model_validate_json(
                kwargs["state_path"].read_text(encoding="utf-8")
            ))
            status = (override or {}).get(_stage.name, _stage.success)
            records = {}
            path = root / _stage.report
            if path.exists():
                prior = json.loads(path.read_text(encoding="utf-8"))
                if prior.get("version") == _stage.version:
                    records = prior.get("records", {})
            for item in selected:
                clip_id = human_clip_identity(item.clip_url)
                record = {
                    "status": status, "clip_url": item.clip_url,
                    "source_url": item.source_url, "source_stream_id": item.source_stream_id,
                    "qualification_allowed": False,
                }
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
                records[clip_id] = record
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "version": _stage.version, "qualification_allowed": False,
                "event_split_verified": False, "reviewed_gold_available": False,
                "benchmark_held_out": False,
                "records": records,
            }), encoding="utf-8")
            return {"counts": {status: len(selected)}, "report_path": str(path)}
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
    assert report["status"] == "SIMULATED_STAGE_TEST_COMPLETE"
    assert report["real_media_run_completed"] is False
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
    assert report["status"] == "SIMULATED_STAGE_TEST_COMPLETE"
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


def add_reserved_clips(state_path, *, count=3):
    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
    for i in range(2, count + 1):
        source = f"stream-reserved-extra-{i}"
        clip = f"https://www.youtube.com/watch?v=EXTRA{i:06d}"
        state.holdout_stream_ids.append(source)
        state.items[f"reserved-{i}"] = CorpusGrowthItem(
            clip_url=clip, source_url=f"https://www.youtube.com/watch?v=SOURCE{i:05d}",
            source_stream_id=source, query="synthetic", status="holdout_reserved",
        )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def test_more_reservations_than_limit_selects_progressive_batches_without_losing_state(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    original = state.read_bytes()
    seen = []
    first = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root, calls=seen),
    )
    assert first["status"] == "SIMULATED_STAGE_TEST_COMPLETE"
    assert first["reserved_clips"] == 3
    assert first["selected_clips"] == 1
    assert first["remaining_clips_after_batch"] == 2
    first_id = first["selected_clip_ids"][0]
    assert len(seen) == 9
    # Projected batch retains durable holdout IDs, but one selected input only.
    view = CorpusGrowthState.model_validate_json(
        (root / "holdout-e2e-batch-state.json").read_text(encoding="utf-8")
    )
    assert len(_candidates(view)) == 1
    assert len(view.holdout_stream_ids) == 3
    assert state.read_bytes() == original

    second = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root, calls=seen),
    )
    third = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root, calls=seen),
    )
    assert second["selected_clip_ids"] != first["selected_clip_ids"]
    assert third["selected_clip_ids"] not in (
        first["selected_clip_ids"], second["selected_clip_ids"],
    )
    assert len({
        first_id, second["selected_clip_ids"][0], third["selected_clip_ids"][0],
    }) == 3
    assert second["observations"]["all_nine_stages_measured_clips"] == 1
    assert third["observations"]["all_nine_stages_measured_clips"] == 1
    assert third["remaining_clips_after_batch"] == 2
    assert state.read_bytes() == original


def test_intermediate_audit_ignores_previous_run_later_stage_success(tmp_path):
    store, state, root = files(tmp_path)
    make_runners(root)["partial_time_map"](
        None, state_path=state, store=store, policy_rules=[],
        max_clips=1, progress=None,
    )
    runners = make_runners(root, override={"original_audio": "INSUFFICIENT_AUDIO"})
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert result["status"] == "BLOCKED_UPSTREAM_STAGE"
    assert result["blocked_stage"] == "original_audio"
    assert result["observations"]["stage_status_counts"]["partial_time_map"] == {
        "NOT_EXECUTED_THIS_RUN": 1
    }
    assert result["observations"]["local_time_map_candidate_segments"] == 0
    assert result["observations"]["candidate_coverage_seconds"] == 0.
    assert result["observations"]["all_nine_stages_measured_clips"] == 0
    assert result["observations"]["per_clip_stage_status"][
        result["selected_clip_ids"][0]
    ]["partial_time_map"] == "NOT_EXECUTED_THIS_RUN"


def test_state_change_mid_run_stops_without_claiming_stale_success(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    original = runners["finished_video"]
    def change_state(provider, **kwargs):
        result = original(provider, **kwargs)
        data = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
        data.cursors["external-change"] = 2
        state.write_text(data.model_dump_json(), encoding="utf-8")
        return result
    runners["finished_video"] = change_state
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert result["status"] == "BLOCKED_CHANGED_RESERVATION_STATE"
    assert result["blocked_stage"] == "original_audio"
    assert len(result["stages"]) == 1
    assert result["real_media_run_completed"] is False


def test_existing_lock_blocks_duplicate_run(tmp_path):
    store, state, root = files(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".holdout-e2e.lock").write_text("owner", encoding="utf-8")
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=1, runners=make_runners(root),
    )
    assert result["status"] == "BLOCKED_CONCURRENT_RUN"
    assert (root / ".holdout-e2e.lock").read_text(encoding="utf-8") == "owner"


def test_preflight_keeps_existing_full_run_report_intact(tmp_path):
    store, state, root = files(tmp_path)
    original = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    assert original["status"] == "SIMULATED_STAGE_TEST_COMPLETE"
    full_path = root / "holdout-e2e-audit.json"
    full_bytes = full_path.read_bytes()
    preview = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, preflight_only=True,
    )
    assert preview["status"] == "PREFLIGHT_ONLY_NO_MEDIA_EXECUTED"
    assert preview["report_path"].endswith("holdout-e2e-preflight.json")
    assert full_path.read_bytes() == full_bytes
    assert (root / "holdout-e2e-preflight.json").exists()


def test_duplicate_clip_reservations_fail_before_any_acquisition(tmp_path):
    store, state, root = files(tmp_path)
    original = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
    duplicated = next(iter(original.items.values())).model_copy(deep=True)
    original.items["second-reservation-same-clip"] = duplicated
    state.write_text(original.model_dump_json(), encoding="utf-8")
    def must_not_call(*args, **kwargs):
        raise AssertionError("Ambiguous reservation cannot enter media stages")
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=3, runners={stage.name: must_not_call for stage in STAGES},
    )
    assert report["status"] == "BLOCKED_AMBIGUOUS_RESERVATION"
    assert report["stages"] == []
    assert not (root / ".holdout-e2e.lock").exists()


def test_postflight_state_change_does_not_claim_e2e_completed(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)
    last = runners["partial_time_map"]
    def mutate_after_last_stage(provider, **kwargs):
        result = last(provider, **kwargs)
        data = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
        data.cursors["modified-while-finishing"] = 3
        state.write_text(data.model_dump_json(), encoding="utf-8")
        return result
    runners["partial_time_map"] = mutate_after_last_stage
    report = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=1, runners=runners,
    )
    assert report["status"] == "BLOCKED_CHANGED_RESERVATION_STATE"
    assert report["blocked_stage"] == "postflight"
    assert not report["real_media_run_completed"]


def test_all_batches_processes_every_reserved_clip_exactly_once(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=5)
    original = state.read_bytes()
    calls = []
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=3, runners=make_runners(root, calls=calls),
    )
    assert report["status"] == "SIMULATED_ALL_BATCHES_COMPLETE"
    assert report["real_media_run_completed"] is False
    assert report["ready"] is False
    assert report["qualification_allowed"] is False
    assert report["reserved_clips"] == 5
    assert report["processed_clips"] == 5
    assert report["remaining_clips"] == 0
    assert [len(b["selected_clip_ids"]) for b in report["batches"]] == [2, 2, 1]
    assert len(set(report["processed_clip_ids"])) == 5
    assert len(calls) == 27
    totals = report["observed_totals"]
    assert totals["clips_with_unique_original_audio_matches"] == 5
    assert totals["sampled_source_finished_frame_links"] == 10
    assert totals["multimodal_cut_candidates"] == 5
    assert totals["local_time_map_candidate_segments"] == 5
    assert totals["candidate_coverage_seconds"] == 10.
    assert report["editor_cut_precision"] is None
    assert report["editor_cut_recall"] is None
    assert report["native_timestamp_error_seconds"] is None
    assert report["event_holdout_accuracy"] is None
    assert state.read_bytes() == original
    assert not (root / ".holdout-e2e.lock").exists()
    saved = json.loads((root / "holdout-e2e-all-batches.json").read_text(encoding="utf-8"))
    assert saved["batches"] == report["batches"]


def test_all_batches_limit_stops_cleanly_and_reports_remaining(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=5)
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=2, runners=make_runners(root),
    )
    assert report["status"] == "BOUNDED_BATCH_LIMIT_REACHED"
    assert report["processed_clips"] == 4
    assert report["remaining_clips"] == 1
    assert len(report["batches"]) == 2
    assert report["real_media_run_completed"] is False
    assert report["qualification_allowed"] is False


def test_all_batches_stops_on_failure_without_attempting_next_batch(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=4)
    calls = []
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=4,
        runners=make_runners(root, calls=calls, override={
            "original_audio": "INSUFFICIENT_UNIQUE_ANCHORS",
        }),
    )
    assert report["status"] == "BLOCKED_BATCH"
    assert report["failed_batch"] == 1
    assert report["blocked_stage"] == "original_audio"
    assert report["blocked_batch_status"] == "BLOCKED_UPSTREAM_STAGE"
    assert len(report["batches"]) == 1
    assert report["batches"][0]["completed_stage_count"] == 2
    assert report["processed_clips"] == 0
    assert report["remaining_clips"] == 4
    assert calls == ["finished_video", "original_audio"]
    assert report["real_media_run_completed"] is False
    assert not (root / ".holdout-e2e.lock").exists()


def test_all_batches_respects_other_run_lock_without_overwriting_audit(tmp_path):
    store, state, root = files(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".holdout-e2e.lock"
    lock.write_text("busy", encoding="utf-8")
    audit = root / "holdout-e2e-all-batches.json"
    audit.write_text('{"status": "previous-result"}', encoding="utf-8")
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=2, runners=make_runners(root),
    )
    assert report["status"] == "BLOCKED_CONCURRENT_RUN"
    assert lock.read_text(encoding="utf-8") == "busy"
    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "previous-result"


def test_all_batches_does_not_claim_success_without_source_reservations(tmp_path):
    store, state, root = files(tmp_path)
    state.unlink()
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=2, runners=make_runners(root),
    )
    assert report["status"] == "BLOCKED_MISSING_RESERVATION_STATE"
    assert report["real_media_run_completed"] is False
    assert report["batches"] == []


def test_all_batches_rechecks_original_corpus_after_each_batch(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    calls = []
    runners = make_runners(root, calls=calls)
    original = runners["partial_time_map"]
    def mutate_state(provider, **kwargs):
        outcome = original(provider, **kwargs)
        new = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
        new.cursors["external"] = 17
        state.write_text(new.model_dump_json(), encoding="utf-8")
        return outcome
    runners["partial_time_map"] = mutate_state
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=3, runners=runners,
    )
    assert report["status"] == "BLOCKED_BATCH"
    assert report["failed_batch"] == 1
    assert report["blocked_batch_status"] == "BLOCKED_CHANGED_RESERVATION_STATE"
    assert report["processed_clips"] == 0
    assert len(calls) == 9
    assert not (root / ".holdout-e2e.lock").exists()


def _one_weak_audio_clip_runners(root, state, *, weak_status="INSUFFICIENT_UNIQUE_ANCHORS",
                                 calls=None):
    """Inject exactly one evidence failure, without fabricating rights approval."""
    collection = _candidates(CorpusGrowthState.model_validate_json(
        state.read_text(encoding="utf-8")
    ))
    bad_id = human_clip_identity(collection[0].clip_url)
    runners = make_runners(root, calls=calls)
    original_audio = runners["original_audio"]

    def one_weak_clip(provider, **kwargs):
        result = original_audio(provider, **kwargs)
        selected = _candidates(CorpusGrowthState.model_validate_json(
            kwargs["state_path"].read_text(encoding="utf-8")
        ))
        assert len(selected) == 1
        current_id = human_clip_identity(selected[0].clip_url)
        if current_id == bad_id:
            report = root / "holdout-audio-alignment.json"
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["records"][current_id]["status"] = weak_status
            report.write_text(json.dumps(payload), encoding="utf-8")
        return result

    runners["original_audio"] = one_weak_clip
    return runners, bad_id


def test_opt_in_continues_only_audio_insufficiency_without_false_success(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=4)
    original_state = state.read_bytes()
    calls = []
    runners, bad_id = _one_weak_audio_clip_runners(root, state, calls=calls)
    result = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=3, max_batches=4, runners=runners,
        continue_on_insufficient_audio=True,
    )
    assert result["status"] == "SIMULATED_INSUFFICIENT_AUDIO_RECORDED"
    assert result["real_media_run_completed"] is False
    assert result["continue_on_insufficient_audio"] is True
    assert result["configured_max_clips"] == 3
    assert result["max_clips_per_batch"] == 1
    assert result["reserved_clips"] == 4
    assert result["attempted_clips"] == 4
    assert result["processed_clips"] == 3
    assert result["deferred_audio_clips"] == 1
    assert result["deferred_audio_clip_ids"] == [bad_id]
    assert bad_id not in result["processed_clip_ids"]
    assert result["remaining_clips"] == 0
    assert len(result["batches"]) == 4
    assert all(len(b["selected_clip_ids"]) == 1 for b in result["batches"])
    assert len(set(x for b in result["batches"] for x in b["selected_clip_ids"])) == 4
    assert len(calls) == 3 * len(STAGES) + 2
    assert result["batches"][0]["deferred_insufficient_audio"] is True
    assert result["batches"][0]["blocked_stage"] == "original_audio"
    assert result["observed_totals"]["clips_with_unique_original_audio_matches"] == 3
    assert state.read_bytes() == original_state
    assert result["qualification_allowed"] is False
    assert result["reviewed_gold_available"] is False
    assert result["ready"] is False
    assert not (root / ".holdout-e2e.lock").exists()


def test_opt_in_insufficient_audio_with_bounded_budget_never_claims_complete(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=4)
    runners, bad_id = _one_weak_audio_clip_runners(root, state)
    result = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=3, max_batches=2, runners=runners,
        continue_on_insufficient_audio=True,
    )
    assert result["status"] == "BOUNDED_BATCH_LIMIT_REACHED"
    assert result["deferred_audio_clip_ids"] == [bad_id]
    assert result["processed_clips"] == 1
    assert result["attempted_clips"] == 2
    assert result["remaining_clips"] == 2
    assert result["real_media_run_completed"] is False


def test_opt_in_never_bypasses_policy_or_transport_failure(tmp_path):
    for unsafe in ("QUARANTINED_POLICY", "RETRYABLE_FAILURE"):
        case = tmp_path / unsafe
        case.mkdir()
        store, state, root = files(case)
        add_reserved_clips(state, count=3)
        calls = []
        runners, _ = _one_weak_audio_clip_runners(
            root, state, weak_status=unsafe, calls=calls,
        )
        result = run_all_reserved_holdout_batches(
            None, store=store, state_path=state, root=root, policy_rules=[],
            max_clips=3, max_batches=3, runners=runners,
            continue_on_insufficient_audio=True,
        )
        assert result["status"] == "BLOCKED_BATCH"
        assert result["blocked_stage"] == "original_audio"
        assert result["deferred_audio_clip_ids"] == []
        assert result["processed_clips"] == 0
        assert result["attempted_clips"] == 1
        assert len(result["batches"]) == 1
        assert calls == ["finished_video", "original_audio"]
        assert not (root / ".holdout-e2e.lock").exists()


def test_default_all_batches_still_fails_closed_on_insufficient_audio(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    runners, _ = _one_weak_audio_clip_runners(root, state)
    # Force single-clip budget to exercise same bad source without opt-in.
    result = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=3, runners=runners,
    )
    assert result["status"] == "BLOCKED_BATCH"
    assert result["deferred_audio_clips"] == 0
    assert result["attempted_clips"] == 1
    assert result["remaining_clips"] == 3
