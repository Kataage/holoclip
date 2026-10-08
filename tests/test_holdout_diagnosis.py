"""Media-free triage of real/failed/simulated reserved source video audits."""
from __future__ import annotations

import json

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.holdout_diagnosis import diagnose_holdout_e2e
from holoclip.holdout_e2e_trial import (
    STAGES, run_all_reserved_holdout_batches, run_reserved_holdout_e2e,
)
from test_holdout_e2e_trial import add_reserved_clips, make_runners
from test_holdout_preparation import files


def _diagnose(store, state, root):
    return diagnose_holdout_e2e(store=store, state_path=state, root=root)


def test_no_reservation_state_explains_without_network(tmp_path):
    store, state, root = files(tmp_path)
    state.unlink()
    report = _diagnose(store, state, root)
    assert report["status"] == "BLOCKED_MISSING_RESERVATION_STATE"
    assert report["next_action"] == "RESTORE_DURABLE_RESERVATION_STATE"
    assert report["network_or_media_examined"] is False
    assert report["real_media_run_completed"] is False
    assert report["ready"] is False
    assert (root / "holdout-e2e-diagnosis.json").exists()


def test_existing_reservations_without_audit_have_actionable_status(tmp_path):
    store, state, root = files(tmp_path)
    before = state.read_bytes()
    report = _diagnose(store, state, root)
    assert report["status"] == "NO_REAL_MEDIA_TRIAL_RECORDED"
    assert report["next_action"] == "RUN_BOUNDED_HOLDOUT_MEDIA_TRIAL"
    assert report["reserved_clips"] == 1
    assert len(report["unattempted_clip_ids"]) == 1
    assert report["batches"] == []
    assert report["editor_cut_precision"] is None
    assert state.read_bytes() == before


def test_full_multi_batch_simulation_only_even_when_all_stages_succeed(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=5)
    original_bytes = state.read_bytes()
    result = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=3, runners=make_runners(root),
    )
    assert result["status"] == "SIMULATED_ALL_BATCHES_COMPLETE"
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "SIMULATED_TRIAL_ONLY"
    assert diagnosis["audit_kind"] == "MULTI_BATCH"
    assert diagnosis["reserved_clips"] == 5
    assert diagnosis["attempted_clips"] == 5
    assert diagnosis["unattempted_clip_ids"] == []
    assert len(diagnosis["batches"]) == 3
    assert diagnosis["stage_status_totals"]["original_audio"] == {
        "PARTIAL_AUDIO_MATCH": 5,
    }
    assert diagnosis["first_blocker"] is None
    assert diagnosis["network_or_media_examined"] is False
    assert diagnosis["real_media_run_completed"] is False
    assert diagnosis["next_action"] == "RUN_WITH_REAL_MEDIA_PROVIDER"
    assert diagnosis["qualification_allowed"] is False
    assert diagnosis["event_holdout_accuracy"] is None
    assert state.read_bytes() == original_bytes


def test_failure_diagnosis_names_actual_blocker_from_persisted_stage_status(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=4)
    run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=2, max_batches=3, runners=make_runners(root, override={
            "original_audio": "INSUFFICIENT_UNIQUE_ANCHORS",
        }),
    )
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "TRIAL_BLOCKED_REVIEW_STAGE"
    assert diagnosis["reserved_clips"] == 4
    assert diagnosis["attempted_clips"] == 2
    assert len(diagnosis["unattempted_clip_ids"]) == 2
    assert diagnosis["first_blocker"] == {
        "batch": 1, "stage": "original_audio",
        "run_status": "BLOCKED_UPSTREAM_STAGE",
        "observed_status_counts": {"INSUFFICIENT_UNIQUE_ANCHORS": 2},
        "diagnostic_action": "CHECK_ORIGINAL_AUDIO_CORRESPONDENCE",
    }
    assert diagnosis["next_action"] == "CHECK_ORIGINAL_AUDIO_CORRESPONDENCE"
    assert diagnosis["stage_status_totals"]["finished_video"] == {
        "PREPARED_UNREVIEWED": 2,
    }
    assert diagnosis["stage_status_totals"]["source_video_pairs"] == {
        "NOT_EXECUTED_THIS_RUN": 2,
    }


def test_incomplete_run_budget_reports_unattempted_original_clip_ids(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=4)
    run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=2, runners=make_runners(root),
    )
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "TRIAL_INCOMPLETE_BATCH_BUDGET"
    assert diagnosis["next_action"] == "INCREASE_FINITE_BATCH_BUDGET_AND_RERUN"
    assert diagnosis["attempted_clips"] == 2
    assert len(diagnosis["unattempted_clip_ids"]) == 2
    assert diagnosis["real_media_run_completed"] is False


def test_original_reservation_state_change_invalidates_old_audit(tmp_path):
    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    before = _diagnose(store, state, root)
    assert before["status"] == "SIMULATED_TRIAL_ONLY"
    corpus = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
    corpus.cursors["other-worker-progress"] = 9
    state.write_text(corpus.model_dump_json(), encoding="utf-8")
    report = _diagnose(store, state, root)
    assert report["status"] == "BLOCKED_STALE_RESERVATION_STATE"
    assert report["next_action"] == "RERUN_TRIAL_AFTER_RESERVATION_CHANGE"
    assert report["real_media_run_completed"] is False
    assert report["batches"] == []


def test_legacy_audit_without_reservation_digest_fails_closed(tmp_path):
    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    path = root / "holdout-e2e-audit.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    del audit["reservation_state_sha256"]
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "BLOCKED_AUDIT_MISSING_RESERVATION_DIGEST"
    assert diagnosis["next_action"] == "REEXECUTE_TRIAL_TO_BIND_RESERVATIONS"


def test_promoted_or_tampered_report_never_looks_like_actual_gold(tmp_path):
    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    path = root / "holdout-e2e-audit.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    audit["reviewed_gold_available"] = True
    audit["real_media_run_completed"] = True
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "BLOCKED_INVALID_AUDIT"
    assert diagnosis["ready"] is False
    assert diagnosis["reviewed_gold_available"] is False
    assert diagnosis["real_media_run_completed"] is False


def test_unreviewed_claim_with_missing_stage_tallies_fails_closed(tmp_path):
    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    path = root / "holdout-e2e-audit.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    audit["status"] = "UNREVIEWED_MEASUREMENTS_COMPLETE"
    audit["real_media_run_completed"] = True
    audit["observations"]["stage_status_counts"].pop("partial_time_map")
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "BLOCKED_INVALID_AUDIT"
    assert diagnosis["editor_cut_precision"] is None
    assert diagnosis["real_media_run_completed"] is False


def test_report_inspection_prefers_all_batches_without_double_counting_single(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    all_report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=3, runners=make_runners(root),
    )
    assert all_report["processed_clips"] == 3
    assert (root / "holdout-e2e-audit.json").is_file()
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["audit_kind"] == "MULTI_BATCH"
    assert diagnosis["attempted_clips"] == 3
    assert diagnosis["stage_status_totals"]["original_audio"] == {
        "PARTIAL_AUDIO_MATCH": 3,
    }


def test_diagnosis_never_serializes_provider_exception_messages(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)

    def bad_media(*args, **kwargs):
        raise RuntimeError("secret-token-exposed-in-ffmpeg-stderr")

    runners["finished_video"] = bad_media
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert result["status"] == "STAGE_FAILED"
    report = _diagnose(store, state, root)
    assert report["status"] == "TRIAL_BLOCKED_REVIEW_STAGE"
    serialized = json.dumps(report)
    assert "secret-token" not in serialized
    assert "ffmpeg-stderr" not in serialized
    assert report["first_blocker"]["stage"] == "finished_video"


def test_missing_stage_report_explained_instead_of_invalid_audit(tmp_path):
    store, state, root = files(tmp_path)
    runners = make_runners(root)

    def skip_report(*args, **kwargs):
        return {"attempted": 1}

    runners["source_video_pairs"] = skip_report
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=runners,
    )
    assert result["status"] == "BLOCKED_UPSTREAM_STAGE"
    assert result["blocked_stage"] == "source_video_pairs"
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "TRIAL_BLOCKED_REVIEW_STAGE"
    assert diagnosis["first_blocker"]["stage"] == "source_video_pairs"
    assert diagnosis["first_blocker"]["observed_status_counts"] == {
        "MISSING_REPORT": 1,
    }
    assert diagnosis["next_action"] == "VERIFY_PINNED_MEDIA_AND_REPORT_CHECKSUMS"


def test_offline_diagnosis_cli_reads_metadata_without_provider(tmp_path):
    from typer.testing import CliRunner
    from holoclip.cli import app

    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    outcome = CliRunner().invoke(app, [
        "examples-diagnose-holdout-e2e",
        "--store", str(store),
        "--state", str(state),
        "--root", str(root),
    ])
    # A synthetic test is not a successful production media audit.
    assert outcome.exit_code == 3, outcome.output
    assert '"status": "SIMULATED_TRIAL_ONLY"' in outcome.output
    assert (root / "holdout-e2e-diagnosis.json").is_file()


def test_all_batches_cli_emits_offline_diagnosis_without_extra_media(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import holoclip.cli as cli

    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=2)
    # Test the public CLI wiring on injected fake runners; never instantiate a
    # real provider or imply that generated stage records are real A/V proof.
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: None)
    monkeypatch.setattr(cli, "load_policy_rules", lambda path: [])

    def fake_local_batches(provider, **kwargs):
        assert provider is None
        return run_all_reserved_holdout_batches(
            provider, **kwargs, runners=make_runners(root),
        )

    monkeypatch.setattr(cli, "run_all_reserved_holdout_batches", fake_local_batches)
    outcome = CliRunner().invoke(cli.app, [
        "examples-validate-holdout-e2e", "--all-batches",
        "--store", str(store), "--state", str(state),
        "--root", str(root), "--max-clips", "1", "--max-batches", "2",
    ])
    assert outcome.exit_code == 3, outcome.output
    assert '"offline_diagnosis": "SIMULATED_TRIAL_ONLY"' in outcome.output
    assert '"unattempted_clips": 0' in outcome.output
    assert '"diagnostic_report_path"' in outcome.output
    assert '"cut_signal_triage": "REFERENCE_FREE_SIGNAL_TRIAGE"' in outcome.output
    assert '"CUT_MEASUREMENT_UNVERIFIED": 2' in outcome.output
    assert (root / "holdout-e2e-all-batches.json").is_file()
    assert (root / "holdout-e2e-diagnosis.json").is_file()


def test_conflicted_holdout_split_is_preserved_as_preflight_cause(tmp_path):
    store, state, root = files(tmp_path)
    corpus = CorpusGrowthState.model_validate_json(state.read_text(encoding="utf-8"))
    reserved = next(iter(corpus.items.values()))
    corpus.items["queued-same-source"] = CorpusGrowthItem(
        clip_url="https://www.youtube.com/watch?v=queuedfake1",
        source_url=reserved.source_url,
        source_stream_id=reserved.source_stream_id,
        status="queued", query="conflict guard synthetic test",
    )
    state.write_text(corpus.model_dump_json(), encoding="utf-8")
    # No media runner may run on a split conflict.
    def must_not_acquire(*args, **kwargs):
        raise AssertionError("Train/holdout split conflict must block before media")
    report = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=2,
        runners={stage.name: must_not_acquire for stage in STAGES},
    )
    assert report["status"] == "BLOCKED_TRAIN_HOLDOUT_SPLIT"
    assert report["failed_batch"] == 1
    assert report["batches"] == []
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "BLOCKED_TRAIN_HOLDOUT_SPLIT"
    assert diagnosis["next_action"] == "REVIEW_TRAIN_HOLDOUT_SPLIT"
    assert diagnosis["network_or_media_examined"] is False


def test_diagnosis_distinguishes_partial_audio_deferral_from_verified_completion(tmp_path):
    from test_holdout_e2e_trial import _one_weak_audio_clip_runners

    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=3)
    runners, bad_id = _one_weak_audio_clip_runners(root, state)
    trial = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=3, max_batches=3, runners=runners,
        continue_on_insufficient_audio=True,
    )
    assert trial["status"] == "SIMULATED_INSUFFICIENT_AUDIO_RECORDED"
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "TRIAL_PARTIAL_WITH_AUDIO_GAPS"
    assert diagnosis["deferred_audio_clip_ids"] == [bad_id]
    assert diagnosis["deferred_audio_clips"] == 1
    assert diagnosis["attempted_clips"] == 3
    assert diagnosis["unattempted_clip_ids"] == []
    assert diagnosis["simulated_only"] is True
    assert diagnosis["real_media_run_completed"] is False
    assert diagnosis["next_action"] == "CHECK_ORIGINAL_AUDIO_CORRESPONDENCE"
    assert diagnosis["first_blocker"]["stage"] == "original_audio"
    assert diagnosis["ready"] is False
    assert diagnosis["reviewed_gold_available"] is False
    assert diagnosis["editor_cut_precision"] is None


def test_diagnosis_rejects_forged_audio_deferral_from_policy_failure(tmp_path):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=2)
    run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, max_batches=2,
        runners=make_runners(root, override={
            "original_audio": "QUARANTINED_POLICY",
        }),
    )
    audit_path = root / "holdout-e2e-all-batches.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    selected = audit["batches"][0]["selected_clip_ids"][0]
    audit["batches"][0]["deferred_insufficient_audio"] = True
    audit["deferred_audio_clip_ids"] = [selected]
    audit["deferred_audio_clips"] = 1
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosed = _diagnose(store, state, root)
    assert diagnosed["status"] == "BLOCKED_INVALID_AUDIT"
    assert diagnosed["deferred_audio_clips"] == 0
    assert diagnosed["ready"] is False


def test_diagnosis_rejects_unattributed_audio_deferrals(tmp_path):
    from test_holdout_e2e_trial import _one_weak_audio_clip_runners

    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=2)
    runners, _ = _one_weak_audio_clip_runners(root, state)
    run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=3, max_batches=2, runners=runners,
        continue_on_insufficient_audio=True,
    )
    path = root / "holdout-e2e-all-batches.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    audit["deferred_audio_clip_ids"] = []
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosed = _diagnose(store, state, root)
    assert diagnosed["status"] == "BLOCKED_INVALID_AUDIT"
    assert diagnosed["reviewed_gold_available"] is False


def test_offline_diagnosis_requires_matching_current_clip_rights_rules(tmp_path):
    from holoclip.clip_policy import ClipPolicyRule, PolicyDecision

    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root, policy_rules=[],
        max_clips=1, runners=make_runners(root),
    )
    rule = ClipPolicyRule(
        id="newly-blocked-topic", decision=PolicyDecision.BLOCK,
        reason="new permission constraint", source_id="test",
        evidence_url="https://example.invalid/prohibition",
    )
    stale = diagnose_holdout_e2e(
        store=store, state_path=state, root=root, policy_rules=[rule],
    )
    assert stale["status"] == "BLOCKED_STALE_POLICY_RULES"
    assert stale["next_action"] == "RERUN_TRIAL_AFTER_POLICY_CHANGE"
    assert stale["real_media_run_completed"] is False
    assert stale["reviewed_gold_available"] is False
    current = diagnose_holdout_e2e(
        store=store, state_path=state, root=root, policy_rules=[],
    )
    assert current["status"] == "SIMULATED_TRIAL_ONLY"
    assert current["policy_verification"] == "MATCHED_CURRENT_RULES"


def test_offline_diagnosis_blocks_legacy_audit_without_policy_digest(tmp_path):
    store, state, root = files(tmp_path)
    run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=1, runners=make_runners(root),
    )
    path = root / "holdout-e2e-audit.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("policy_rules_sha256", None)
    path.write_text(json.dumps(old), encoding="utf-8")
    checked = diagnose_holdout_e2e(
        store=store, state_path=state, root=root, policy_rules=[],
    )
    assert checked["status"] == "BLOCKED_AUDIT_MISSING_POLICY_DIGEST"
    assert checked["real_media_run_completed"] is False
    assert checked["ready"] is False
