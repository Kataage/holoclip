"""Offline cut-signal triage must not turn saved unreviewed evidence into gold."""
import json

import pytest

from holoclip.holdout_diagnosis import diagnose_holdout_e2e
from holoclip.holdout_e2e_trial import run_all_reserved_holdout_batches, run_reserved_holdout_e2e
from holoclip.real_cut_observations import (
    aggregate_real_cut_observations, describe_real_cut_observations,
)
from holoclip.real_cut_triage import diagnose_saved_real_cut_observations
from test_holdout_e2e_trial import add_reserved_clips, make_runners
from test_holdout_preparation import files
from test_real_cut_observations import cut_row, side_row


def _run(tmp_path, count=3):
    store, state, root = files(tmp_path)
    add_reserved_clips(state, count=count)
    trial = run_all_reserved_holdout_batches(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=2, max_batches=2,
        runners=make_runners(root),
    )
    assert trial["status"] == "SIMULATED_ALL_BATCHES_COMPLETE"
    return store, state, root


def _diagnose(store, state, root):
    return diagnose_holdout_e2e(
        store=store, state_path=state, root=root, policy_rules=[],
    )


def test_all_batches_triage_preserves_simulated_status_and_unverified_counts(tmp_path):
    store, state, root = _run(tmp_path)
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "SIMULATED_TRIAL_ONLY"
    triage = diagnosis["cut_signal_triage"]
    assert triage["status"] == "REFERENCE_FREE_SIGNAL_TRIAGE"
    assert triage["clips_with_diagnostics"] == 3
    assert triage["clips_with_reference_free_measurements"] == 0
    assert triage["signal_counts"] == {"CUT_MEASUREMENT_UNVERIFIED": 3}
    assert all(codes == ["CUT_MEASUREMENT_UNVERIFIED"]
               for codes in triage["per_clip_signal_codes"].values())
    assert triage["ready"] is False
    assert triage["editor_cut_precision"] is None
    assert triage["editor_cut_recall"] is None
    assert diagnosis["real_media_run_completed"] is False


def test_completed_single_batch_also_has_offline_triage(tmp_path):
    store, state, root = files(tmp_path)
    result = run_reserved_holdout_e2e(
        None, store=store, state_path=state, root=root,
        policy_rules=[], max_clips=1, runners=make_runners(root),
    )
    assert result["status"] == "SIMULATED_STAGE_TEST_COMPLETE"
    triage = _diagnose(store, state, root)["cut_signal_triage"]
    assert triage["status"] == "REFERENCE_FREE_SIGNAL_TRIAGE"
    assert triage["clips_with_diagnostics"] == 1
    assert triage["signal_counts"] == {"CUT_MEASUREMENT_UNVERIFIED": 1}


def test_measured_synthetic_signals_get_distinct_codes_but_no_gold(tmp_path):
    store, state, root = _run(tmp_path, count=1)
    path = root / "holdout-e2e-all-batches.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    selected = audit["batches"][0]["selected_clip_ids"][0]
    record = describe_real_cut_observations(cut_row(), side_row())
    record["clip_id"] = selected
    audit["batches"][0]["real_cut_observations"] = {selected: record}
    audit["real_cut_observations"] = {selected: record}
    audit["real_cut_observation_totals"] = aggregate_real_cut_observations(
        audit["real_cut_observations"]
    )
    path.write_text(json.dumps(audit), encoding="utf-8")
    diag = _diagnose(store, state, root)
    triage = diag["cut_signal_triage"]
    assert diag["status"] == "SIMULATED_TRIAL_ONLY"
    assert triage["clips_with_reference_free_measurements"] == 1
    assert triage["per_clip_signal_codes"][selected] == [
        "OVERBROAD_ACOUSTIC_INTERVALS",
        "UNRESOLVED_ACOUSTIC_TRANSITIONS",
        "PARTIAL_OUTPUT_VISUAL_CORROBORATION",
    ]
    assert triage["independent_editor_gold"] is False
    assert triage["real_media_rechecked"] is False
    assert triage["editor_cut_precision"] is None


@pytest.mark.parametrize("tamper", [
    "forged_total", "missing_batch", "fake_candidate", "promote_gold",
    "stranger_clip", "incomplete_total",
])
def test_corrupt_cut_diagnostics_fail_closed_and_do_not_claim_gold(tmp_path, tamper):
    store, state, root = _run(tmp_path)
    path = root / "holdout-e2e-all-batches.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    clip_id = audit["batches"][0]["selected_clip_ids"][0]
    if tamper == "forged_total":
        audit["real_cut_observation_totals"]["cut_candidates"] = 99
    elif tamper == "missing_batch":
        del audit["batches"][0]["real_cut_observations"]
    elif tamper == "fake_candidate":
        record = audit["real_cut_observations"][clip_id]
        record["cut_candidate_count"] = 999
        audit["batches"][0]["real_cut_observations"][clip_id] = record
    elif tamper == "promote_gold":
        record = audit["real_cut_observations"][clip_id]
        record["reviewed_gold_available"] = True
        audit["batches"][0]["real_cut_observations"][clip_id] = record
    elif tamper == "stranger_clip":
        record = audit["real_cut_observations"].pop(clip_id)
        audit["real_cut_observations"]["youtube:otheridentity"] = record
    else:
        del audit["real_cut_observation_totals"]
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "BLOCKED_INVALID_AUDIT"
    assert diagnosis["cut_signal_triage"]["status"] == "NOT_AVAILABLE"
    assert diagnosis["ready"] is False
    assert diagnosis["reviewed_gold_available"] is False
    assert diagnosis["editor_cut_precision"] is None


def test_legacy_multibatch_uses_explicit_unavailable_without_erasing_old_audit(tmp_path):
    store, state, root = _run(tmp_path)
    path = root / "holdout-e2e-all-batches.json"
    audit = json.loads(path.read_text(encoding="utf-8"))
    del audit["real_cut_observations"]
    del audit["real_cut_observation_totals"]
    for batch in audit["batches"]:
        batch.pop("real_cut_observations", None)
    path.write_text(json.dumps(audit), encoding="utf-8")
    diagnosis = _diagnose(store, state, root)
    assert diagnosis["status"] == "SIMULATED_TRIAL_ONLY"
    assert diagnosis["cut_signal_triage"]["status"] == "LEGACY_NO_CUT_OBSERVATIONS"
    assert diagnosis["ready"] is False


def test_unavailable_audio_is_not_zero_cut_or_faked_success():
    row = describe_real_cut_observations({
        **cut_row(),
        "refinement": {
            "version": "bounded-acoustic-cut-brackets-v2",
            "status": "UNKNOWN_SAMPLING_BUDGET",
            "transitions": [],
        },
    })
    assert row["cut_candidate_count"] is None
    audit = {
        "observations": {
            "real_cut_observations": {"youtube:aaaaaaaaaaa": row},
            "real_cut_observation_totals": aggregate_real_cut_observations({
                "youtube:aaaaaaaaaaa": row,
            }),
        },
    }
    rows = [{
        "selected_clip_ids": ["youtube:aaaaaaaaaaa"],
        "status": "UNREVIEWED_MEASUREMENTS_COMPLETE",
    }]
    diagnosed = diagnose_saved_real_cut_observations(
        audit, rows, multi_batch=False,
    )
    assert diagnosed["signal_counts"] == {"ACOUSTIC_SUPPORT_UNKNOWN": 1}
    assert diagnosed["clips_with_reference_free_measurements"] == 0
    assert diagnosed["editor_cut_recall"] is None


def test_normalized_tamper_rejected_without_media():
    record = describe_real_cut_observations(cut_row(), side_row())
    row_id = "youtube:aaaaaaaaaaa"
    record["overbroad_candidate_count"] = 3
    payload = {
        "observations": {
            "real_cut_observations": {row_id: record},
            "real_cut_observation_totals": aggregate_real_cut_observations({
                row_id: record,
            }),
        },
    }
    with pytest.raises(ValueError):
        diagnose_saved_real_cut_observations(
            payload, [{"selected_clip_ids": [row_id],
                       "status": "UNREVIEWED_MEASUREMENTS_COMPLETE"}],
            multi_batch=False,
        )
