"""Cross-cycle non-gold video evidence: independence, ambiguity, replay, tamper."""
import copy
import json

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.source_candidate_visual import _NO_GOLD, _finished_starts
from holoclip.source_visual_ledger import (
    VERSION, append_visual_observation, checked_ledger,
)
from test_source_candidate_visual import A, B, Provider, make_state, verify


CID = "AAAAAAAAAAA"
AID = "BBBBBBBBBBB"
BID = "CCCCCCCCCCC"
HASH_A = "a"*64
HASH_B = "b"*64


def strong():
    return {
        "status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
        "matched_source_window": 0,
        "matched_frames": 3,
        "finished_frame_times_local": [0., 2., 4.],
        "source_frame_times_local": [0., 2., 4.],
        **_NO_GOLD,
    }


def add(current=None, *, source_id=AID, clip_start=0.,
        source_start=120., clip_sha=HASH_A, source_sha=HASH_B):
    if current is None:
        current = checked_ledger(
            None, clip_id=CID, candidate_ids=[AID, BID],
        )
    return append_visual_observation(
        current, clip_id=CID, candidate_ids=[AID, BID],
        source_stream_id=source_id,
        finished_range=[clip_start, clip_start+12.],
        source_ranges=[[source_start, source_start+12.]],
        finished_sha=clip_sha, source_shas=[source_sha],
        observed=strong(),
    )


def test_distinct_two_sided_correspondences_across_runs_accumulate_not_gold():
    ledger, recorded = add()
    assert recorded is True
    assert ledger["version"] == VERSION
    assert ledger["summary"]["status"] == (
        "UNREVIEWED_SINGLE_VISUAL_WINDOW_HYPOTHESIS"
    )
    same, recorded = add(ledger)
    assert recorded is False
    assert same == ledger  # same sample rerun, including cached bytes

    # The *original* differs, but the finished side is the same 12s;
    # this is NOT a second independent finished scene.
    same_output, recorded = add(
        ledger, source_start=720., source_sha="c"*64,
    )
    assert recorded is True
    assert same_output["summary"]["independent_two_sided_window_counts"][
        AID
    ] == 1

    # A different finished and source range is independent on BOTH sides.
    diverse, recorded = add(
        same_output, clip_start=24., source_start=1440.,
        clip_sha="d"*64, source_sha="e"*64,
    )
    assert recorded is True
    assert diverse["summary"]["status"] == (
        "UNREVIEWED_TWO_SIDED_DIVERSE_VISUAL_HYPOTHESIS"
    )
    assert diverse["summary"]["independent_two_sided_window_counts"][AID] == 2
    assert diverse["summary"]["recorded_observations"] == 3
    for field in _NO_GOLD:
        assert diverse[field] is False
        assert diverse["summary"][field] is False


def test_different_candidates_in_historical_ledger_prevent_single_winner():
    ledger, _ = add()
    ledger, _ = add(
        ledger, source_id=AID, clip_start=24.,
        source_start=1440., source_sha="e"*64,
    )
    ledger, _ = add(
        ledger, source_id=BID, clip_start=48.,
        source_start=630., source_sha="f"*64,
    )
    assert ledger["summary"]["status"] == (
        "UNKNOWN_DIFFERENT_CANDIDATES_SHARE_VISUAL_EVIDENCE"
    )
    assert ledger["summary"]["corroborated_candidate_count"] == 2
    assert ledger["summary"]["independent_two_sided_window_counts"][AID] == 2
    assert ledger["source_identity_verified"] is False


def test_changed_video_bytes_at_identical_source_finish_spans_fail_closed():
    ledger, _ = add()
    with pytest.raises(ValueError, match="Media changed"):
        add(ledger, clip_sha="c"*64)
    with pytest.raises(ValueError, match="Media changed"):
        add(ledger, source_sha="d"*64)


@pytest.mark.parametrize("mutation", [
    "promote_flag", "summary_promotion", "wrong_stream", "fake_event",
    "bad_timestamp", "bad_duration", "duplicate_event", "wrong_version",
])
def test_persisted_ledger_mutations_rejected_before_any_positive_counts(mutation):
    ledger, _ = add()
    tampered = copy.deepcopy(ledger)
    if mutation == "promote_flag":
        tampered["source_identity_verified"] = True
    elif mutation == "summary_promotion":
        tampered["summary"]["independent_two_sided_window_counts"][AID] = 8
    elif mutation == "wrong_stream":
        tampered["events"][0]["source_stream_id"] = "other"
    elif mutation == "fake_event":
        tampered["events"][0]["reviewed_gold_available"] = True
    elif mutation == "bad_timestamp":
        tampered["events"][0]["source_frame_times_local"] = [4., 2., 3.]
    elif mutation == "bad_duration":
        tampered["events"][0]["source_requested_range_seconds"] = [5., 500.]
    elif mutation == "duplicate_event":
        tampered["events"].append(copy.deepcopy(tampered["events"][0]))
    else:
        tampered["version"] = "old"
    with pytest.raises(ValueError):
        checked_ledger(tampered, clip_id=CID, candidate_ids=[AID, BID])


def test_incomplete_or_weak_image_pair_can_never_enter_evidence():
    value = checked_ledger(None, clip_id=CID, candidate_ids=[AID, BID])
    for observation in (
        {"status": "UNKNOWN_NO_VISUAL_SEQUENCE",
         "matched_frames": 2, **_NO_GOLD},
        {"status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
         "matched_frames": 3, **_NO_GOLD},
    ):
        same, recorded = append_visual_observation(
            value, clip_id=CID, candidate_ids=[AID, BID],
            source_stream_id=AID,
            finished_range=[0., 12.], source_ranges=[[120., 132.]],
            finished_sha=HASH_A, source_shas=[HASH_B],
            observed=observation,
        )
        assert same == value
        assert recorded is False


def test_output_window_selection_is_independent_and_bounded():
    assert _finished_starts(40.)[:3] == [14., 0., 28.]
    assert _finished_starts(24.) == [0., 12.]
    assert _finished_starts(12.) == [0.]
    assert _finished_starts(11.) == []
    for duration in (24., 25., 30., 36., 40., 75., 180.):
        starts = _finished_starts(duration)
        assert starts
        assert len(starts) <= 6
        assert len(starts) == len(set(starts))
        assert all(0 <= t <= duration-12 for t in starts)
        assert all(abs(b-a) >= 11.999
                   for i, a in enumerate(starts) for b in starts[i+1:])


def test_unattended_worker_rotates_finished_ranges_and_aggregates_distinct_pairs(tmp_path):
    make_state(tmp_path/"state.json")
    provider = Provider()
    def same_source_correctly_grounded(_finished, _sources):
        return strong()

    for _ in range(2):
        report = verify(
            provider, tmp_path,
            compare_sequences=same_source_correctly_grounded,
        )
        assert report["attempted"] == 1
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    scout = state.items["candidate"].source_scout
    ledger = scout["visual_evidence"]
    assert ledger["summary"]["status"] == (
        "UNREVIEWED_TWO_SIDED_DIVERSE_VISUAL_HYPOTHESIS"
    )
    assert ledger["summary"]["independent_two_sided_window_counts"][AID] == 2
    assert [e["finished_range_seconds"] for e in ledger["events"]] == [
        [14., 26.], [0., 12.],
    ]
    assert provider.calls[0][:3] == (
        "https://www.youtube.com/watch?v=AAAAAAAAAAA", 14., 26.,
    )
    assert provider.calls[4][:3] == (
        "https://www.youtube.com/watch?v=AAAAAAAAAAA", 0., 12.,
    )
    assert scout["visual_probe"]["cumulative_source_visual_support"] == ledger[
        "summary"
    ]
    assert state.items["candidate"].source_stream_id is None
    assert state.items["candidate"].source_url is None


def test_corrupt_history_blocks_all_new_media_acquisition(tmp_path):
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    wrong = checked_ledger(
        None, clip_id=CID, candidate_ids=[AID],
    )
    wrong["source_identity_verified"] = True
    state.items["candidate"].source_scout["visual_evidence"] = wrong
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    provider = Provider()
    report = verify(provider, tmp_path)
    assert report["counts"] == {
        "BLOCKED_INVALID_SOURCE_VISUAL_EVIDENCE": 1,
    }
    assert provider.calls == []
    saved = json.loads((tmp_path/"state.json").read_text(encoding="utf-8"))
    assert saved["items"]["candidate"]["source_url"] is None
