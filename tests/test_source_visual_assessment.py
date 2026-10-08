"""Strictly derived, reference-free original candidate decision support."""
import copy
import json

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.source_visual_assessment import (
    VERSION, assess_visual_candidates, checked_visual_candidate_assessment,
)
from holoclip.source_visual_ledger import checked_ledger
from test_source_candidate_visual import CLIP, Provider, make_state, verify
from test_source_visual_ledger import (
    AID, BID, CID, add, strong,
)


def empty():
    return checked_ledger(None, clip_id=CID, candidate_ids=[AID, BID])


def assess(ledger):
    return assess_visual_candidates(
        ledger, clip_id=CID, candidate_ids=[AID, BID],
    )


def test_empty_ledger_is_missing_positive_evidence_not_negative_source_label():
    result = assess(empty())
    assert result["version"] == VERSION
    assert result["status"] == "UNKNOWN_NO_SAVED_POSITIVE_SOURCE_EVIDENCE"
    assert result["next_autonomous_evidence_step"] == (
        "CONTINUE_BOUNDED_AUTONOMOUS_SOURCE_SCANNING"
    )
    assert [c["evidence_tier"] for c in result["candidate_observations"]] == [
        "NO_SAVED_POSITIVE_VISUAL_WINDOWS",
        "NO_SAVED_POSITIVE_VISUAL_WINDOWS",
    ]
    assert result["candidate_rejected_from_missing_match"] is False
    assert result["source_candidate_selected"] is False
    assert result["calibrated_source_probability"] is None
    assert result["source_identity_decision"] == "UNKNOWN_NOT_SELECTED"


def test_single_positive_window_does_not_select_original_or_claim_accuracy():
    ledger, written = add()
    assert written is True
    result = assess(ledger)
    assert result["status"] == "UNREVIEWED_SINGLE_CONTEXT_ORIGINAL_HYPOTHESIS"
    assert result["next_autonomous_evidence_step"] == (
        "SEEK_ADDITIONAL_DISTINCT_VIDEO_CONTEXTS"
    )
    assert result["candidate_ids_with_positive_evidence"] == [AID]
    first, second = result["candidate_observations"]
    assert first["recorded_positive_observations"] == 1
    assert first["independent_two_sided_window_lower_bound"] == 1
    assert first["distinct_finished_contexts_observed"] == 1
    assert first["distinct_original_contexts_observed"] == 1
    assert first["sampled_finished_positive_ranges_seconds"] == [[0., 12.]]
    assert second["evidence_tier"] == "NO_SAVED_POSITIVE_VISUAL_WINDOWS"
    assert second["source_rejected"] is False


def test_nonindependent_replays_do_not_upgrade_diverse_hypothesis():
    ledger, _ = add()
    ledger, _ = add(
        ledger, source_start=720., source_sha="c"*64,
    )
    result = assess(ledger)
    assert result["candidate_observations"][0][
        "recorded_positive_observations"
    ] == 2
    assert result["candidate_observations"][0][
        "independent_two_sided_window_lower_bound"
    ] == 1
    assert result["status"] == "UNREVIEWED_SINGLE_CONTEXT_ORIGINAL_HYPOTHESIS"


def test_independent_two_sided_windows_upgrade_hypothesis_not_source_gold():
    ledger, _ = add()
    ledger, _ = add(
        ledger, clip_start=24., source_start=1440.,
        clip_sha="d"*64, source_sha="e"*64,
    )
    result = assess(ledger)
    assert result["status"] == "UNREVIEWED_DIVERSE_SINGLE_ORIGINAL_HYPOTHESIS"
    assert result["next_autonomous_evidence_step"] == (
        "SEEK_INDEPENDENT_ORIGINAL_IDENTITY_CHECK"
    )
    assert result["candidate_observations"][0][
        "independent_two_sided_window_lower_bound"
    ] == 2
    assert result["source_identity_verified"] is False
    assert result["used_in_learning_or_holdout"] is False
    assert result["calibrated_source_probability"] is None


def test_competing_candidates_override_single_candidate_diversity():
    ledger, _ = add()
    ledger, _ = add(
        ledger, clip_start=24., source_start=1440.,
        clip_sha="d"*64, source_sha="e"*64,
    )
    ledger, _ = add(
        ledger, source_id=BID, clip_start=48., source_start=650.,
        clip_sha="f"*64, source_sha="1"*64,
    )
    result = assess(ledger)
    assert result["status"] == "UNKNOWN_COMPETING_VISUAL_ORIGINALS"
    assert result["next_autonomous_evidence_step"] == (
        "COMPARE_COMPETING_ORIGINALS_AUTOMATICALLY"
    )
    assert result["candidate_ids_with_positive_evidence"] == [AID, BID]
    assert result["competing_positive_candidates"] is True
    assert result["source_candidate_selected"] is False
    assert result["source_identity_decision"] == "UNKNOWN_NOT_SELECTED"


@pytest.mark.parametrize("tamper", [
    "claim_source", "forge_score", "changed_summary", "change_events",
    "wrong_clip", "wrong_version", "change_candidate_count",
])
def test_saved_assessment_is_recomputed_and_tampering_fails_closed(tamper):
    ledger, _ = add()
    value = assess(ledger)
    changed = copy.deepcopy(value)
    if tamper == "claim_source":
        changed["source_identity_verified"] = True
    elif tamper == "forge_score":
        changed["calibrated_source_probability"] = .97
    elif tamper == "changed_summary":
        changed["candidate_observations"][0][
            "independent_two_sided_window_lower_bound"
        ] = 17
    elif tamper == "change_events":
        changed["ledger_events_sha256"] = "0"*64
    elif tamper == "wrong_clip":
        changed["clip_id"] = "other"
    elif tamper == "wrong_version":
        changed["version"] = "obsolete"
    else:
        changed["candidate_count"] = 1
    with pytest.raises(ValueError):
        checked_visual_candidate_assessment(
            changed, ledger, clip_id=CID,
            candidate_ids=[AID, BID],
        )


def test_changing_source_shortlist_invalidates_old_assessment():
    ledger, _ = add()
    saved = assess(ledger)
    with pytest.raises(ValueError):
        checked_visual_candidate_assessment(
            saved, ledger, clip_id=CID, candidate_ids=[AID],
        )


def test_ledger_source_hash_must_stay_constant_across_finished_windows():
    ledger, _ = add()
    with pytest.raises(ValueError, match="Video bytes changed"):
        add(
            ledger, clip_start=24., source_start=120.,
            clip_sha="d"*64, source_sha="f"*64,
        )


def test_ledger_finished_hash_must_stay_constant_across_original_candidates():
    ledger, _ = add()
    with pytest.raises(ValueError, match="Video bytes changed"):
        add(
            ledger, source_id=BID, clip_start=0., source_start=650.,
            clip_sha="d"*64, source_sha="e"*64,
        )


def test_worker_persists_candidate_assessment_in_autonomous_pipeline(tmp_path):
    make_state(tmp_path/"state.json")
    provider = Provider()
    for _ in range(2):
        outcome = verify(
            provider, tmp_path,
            compare_sequences=lambda _output, _sources: strong(),
        )
        assert outcome["attempted"] == 1
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    scout = state.items["candidate"].source_scout
    assessment = scout["visual_candidate_assessment"]
    assert assessment["version"] == VERSION
    assert assessment["status"] == "UNREVIEWED_DIVERSE_SINGLE_ORIGINAL_HYPOTHESIS"
    assert assessment["candidate_observations"][0][
        "independent_two_sided_window_lower_bound"
    ] == 2
    assert scout["visual_probe"]["candidate_assessment"] == assessment
    assert state.items["candidate"].source_url is None
    assert state.items["candidate"].source_stream_id is None


def test_malformed_saved_assessment_blocks_all_video_downloads(tmp_path):
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    original = state.items["candidate"].source_scout
    real_clip_id = human_clip_identity(CLIP)
    current = checked_ledger(
        None, clip_id=real_clip_id, candidate_ids=[AID],
    )
    forged = assess_visual_candidates(
        current, clip_id=real_clip_id, candidate_ids=[AID],
    )
    forged["source_candidate_selected"] = True
    original["visual_evidence"] = current
    original["visual_candidate_assessment"] = forged
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    provider = Provider()
    outcome = verify(provider, tmp_path)
    assert outcome["counts"] == {
        "BLOCKED_INVALID_OR_STALE_VISUAL_CANDIDATE_ASSESSMENT": 1
    }
    assert provider.calls == []
    saved = json.loads((tmp_path/"state.json").read_text(encoding="utf-8"))
    assert saved["items"]["candidate"]["source_url"] is None
