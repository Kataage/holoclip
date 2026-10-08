"""Focused bounded, fail-closed and non-promoting visual refinement tests."""
import json

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.source_candidate_visual import (
    _NO_GOLD, compare_sparse_visual_sequences,
)
from holoclip.source_visual_refinement import (
    VERSION, adaptive_refinement_starts, make_weak_hint,
)
from test_source_candidate_visual import (
    A, CLIP, Provider, frame, make_state, verify,
)
from types import SimpleNamespace

SHA = "a"*64
SOURCE = A.split("=")[-1]


def hint(seed=1800., cursor=1):
    return make_weak_hint(
        source_id=SOURCE, duration=3600., seed_start=seed,
        completed_starts=[0., 1800., 3588.], next_cursor=cursor,
        finished_sha=SHA,
    )


def test_finite_adaptive_windows_expand_around_weak_center_without_repeats():
    candidate = hint()
    assert candidate["version"] == VERSION
    sampled = adaptive_refinement_starts(
        candidate, source_id=SOURCE, duration=3600.,
        cursor=1, finished_sha=SHA,
    )
    assert sampled == [1812., 1788., 1824.]
    assert len(sampled) == 3
    assert len(set(sampled)) == 3
    assert all(0 <= t <= 3588 for t in sampled)
    assert all(abs(t - prev) >= 11.999
               for t in sampled for prev in [0., 1800., 3588.])
    assert candidate["source_identity_verified"] is False


@pytest.mark.parametrize("corruption", [
    "wrong_source", "old_cursor", "new_clip", "promoted", "negative_seed",
    "duplicate_starts", "extra_key",
])
def test_stale_malformed_promoted_weak_hints_cannot_trigger_source_seek(corruption):
    value = hint()
    if corruption == "wrong_source":
        value["source_id"] = "impostor"
    elif corruption == "old_cursor":
        value["next_cursor"] = 9
    elif corruption == "new_clip":
        value["finished_media_sha256"] = "b"*64
    elif corruption == "promoted":
        value["source_identity_verified"] = True
    elif corruption == "negative_seed":
        value["seed_start"] = -2.
    elif corruption == "duplicate_starts":
        value["completed_coarse_starts"] = [1800., 1800.]
    else:
        value["unsupported_editor_cut"] = True
    with pytest.raises(ValueError):
        adaptive_refinement_starts(
            value, source_id=SOURCE, duration=3600.,
            cursor=1, finished_sha=SHA,
        )


def test_two_advancing_matches_yield_weak_non_gold_hint_only():
    finished = SimpleNamespace(frames=[frame("clip", i) for i in range(5)])
    original = SimpleNamespace(frames=[frame("source", i) for i in range(5)])

    def compare(src, dst):
        # Deliberately only two matched images, not three cut gold.
        if src.endswith("-1") and dst.endswith("-1") or (
                src.endswith("-2") and dst.endswith("-2")):
            return {"score": .91}
        return None

    found = compare_sparse_visual_sequences(
        finished, [original], extract=lambda x: x, compare=compare,
    )
    assert found["status"] == "UNKNOWN_NO_VISUAL_SEQUENCE"
    assert found["weak_source_window"] == 0
    assert found["weak_visual_pair_count"] == 2
    assert found["matched_frames"] == 0
    assert all(found[flag] is False for flag in _NO_GOLD)


def test_multiple_weak_source_windows_stay_unknown_without_refinement_priority():
    finished = SimpleNamespace(frames=[frame("clip", i) for i in range(5)])
    first = SimpleNamespace(frames=[frame("alpha", i) for i in range(5)])
    second = SimpleNamespace(frames=[frame("beta", i) for i in range(5)])

    def compare(src, dst):
        if ((src.startswith("alpha") and src[-1] == dst[-1] and src[-1] in "12")
                or (src.startswith("beta") and src[-1] == dst[-1] and src[-1] == "3")):
            return {"score": .92}
        return None

    result = compare_sparse_visual_sequences(
        finished, [first, second], extract=lambda x: x, compare=compare,
    )
    assert result["weak_source_window"] is None
    assert result["weak_visual_pair_count"] == 0
    assert result["status"] == "UNKNOWN_NO_VISUAL_SEQUENCE"


def test_unattended_weak_local_probe_one_shot_then_broad_search_resumes(tmp_path):
    make_state(tmp_path/"state.json")
    provider = Provider()
    calls = []

    def sparse(_finished, _sources):
        calls.append(1)
        if len(calls) == 1:
            return {
                "status": "UNKNOWN_NO_VISUAL_SEQUENCE",
                "weak_source_window": 1,
                "weak_visual_pair_count": 2, **_NO_GOLD,
            }
        return {
            "status": "UNKNOWN_NO_VISUAL_SEQUENCE",
            "weak_source_window": None,
            "weak_visual_pair_count": 0, **_NO_GOLD,
        }

    first = verify(provider, tmp_path, compare_sequences=sparse)
    assert first["attempted"] == 1
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    scout = state.items["candidate"].source_scout
    assert scout["visual_scan_cursors"][SOURCE] == 1
    assert scout["visual_refinements"][SOURCE]["next_cursor"] == 1
    assert scout["visual_probe"]["candidate_observations"][0][
        "weak_visual_hint_saved"
    ] is True
    first_windows = provider.calls[1:]
    assert [start for _, start, _ in first_windows] == [0., 1800., 3588.]

    provider.calls.clear()
    verify(provider, tmp_path, compare_sequences=sparse)
    assert [start for _, start, _ in provider.calls[1:]] == [
        1812., 1788., 1824.,
    ]
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    scout = state.items["candidate"].source_scout
    assert scout["visual_scan_cursors"][SOURCE] == 1
    assert SOURCE not in scout["visual_refinements"]
    assert scout["visual_probe"]["candidate_observations"][0][
        "probe_mode"
    ] == "one_shot_local_refinement"

    provider.calls.clear()
    verify(provider, tmp_path, compare_sequences=sparse)
    assert [start for _, start, _ in provider.calls[1:]] != [
        1812., 1788., 1824.,
    ]
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    assert state.items["candidate"].source_scout[
        "visual_scan_cursors"
    ][SOURCE] == 2
    assert state.items["candidate"].source_url is None
    assert state.items["candidate"].source_stream_id is None


def test_malicious_persisted_hint_blocks_source_media_before_acquisition(tmp_path):
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    state.items["candidate"].source_scout["visual_refinements"] = {
        SOURCE: {**hint(), "source_identity_verified": True},
    }
    state.items["candidate"].source_scout["visual_scan_cursors"] = {
        SOURCE: 1,
    }
    (tmp_path/"state.json").write_text(
        state.model_dump_json(), encoding="utf-8",
    )
    provider = Provider()
    result = verify(provider, tmp_path)
    assert result["attempted"] == 1
    assert provider.calls == [(CLIP, 14., 26.)]
    stored = json.loads((tmp_path/"state.json").read_text(encoding="utf-8"))
    row = stored["items"]["candidate"]["source_scout"]["visual_probe"]
    assert row["candidate_observations"][0]["status"] == (
        "BLOCKED_STALE_OR_PROMOTED_REFINEMENT"
    )
    assert row["source_identity_verified"] is False
