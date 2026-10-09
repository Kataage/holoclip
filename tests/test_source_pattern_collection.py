"""Unattended public pattern collection is fair, bounded, and not gold."""
import asyncio
import copy

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.source_candidate_visual import verify_pending_source_visual
from holoclip.source_pattern_collection import collect_public_source_patterns
from test_source_candidate_visual import (
    A, CLIP, Provider, fake_measure, fake_probe, make_state,
)

NEXT = "https://www.youtube.com/watch?v=DDDDDDDDDDD"
THIRD = "https://www.youtube.com/watch?v=EEEEEEEEEEE"


def _seed_multiple(path):
    make_state(path)
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    template = copy.deepcopy(state.items["candidate"])
    for key, url in (("second", NEXT), ("third", THIRD)):
        other = template.model_copy(deep=True)
        other.clip_url = url
        state.items[key] = other
    path.write_text(state.model_dump_json(), encoding="utf-8")


class MultipleClips(Provider):
    def probe(self, url):
        if url in (NEXT, THIRD):
            from test_source_candidate_visual import meta
            return meta(url, duration=40.)
        return super().probe(url)


def _run_once(provider, tmp_path, **kwargs):
    return verify_pending_source_visual(
        provider, store=tmp_path/"none.jsonl",
        state_path=tmp_path/"state.json", output=tmp_path/"media",
        policy_rules=[], measure=fake_measure, probe=fake_probe,
        max_clips=1, **kwargs,
    )


def test_pending_visual_queue_rotates_across_invocations(tmp_path):
    _seed_multiple(tmp_path/"state.json")
    provider = MultipleClips()
    choices = []
    for _ in range(4):
        before = len(provider.calls)
        result = _run_once(provider, tmp_path)
        assert result["attempted"] == 1
        assert len(result["attempted_clip_keys"]) == 1
        choices.append(result["attempted_clip_keys"][0])
        assert len(provider.calls) > before
    assert choices == ["candidate", "second", "third", "candidate"]
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    assert state.cursors["source_visual_pending_round_robin"] == 4


def test_invalid_persistent_cursor_aborts_before_public_video_calls(tmp_path):
    make_state(tmp_path/"state.json")
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    state.cursors["source_visual_pending_round_robin"] = -1
    (tmp_path/"state.json").write_text(state.model_dump_json(), encoding="utf-8")
    provider = Provider()
    with pytest.raises(ValueError, match="cursor"):
        _run_once(provider, tmp_path)
    assert not provider.calls


def test_metadata_free_batch_reports_unknown_without_invented_success(tmp_path, monkeypatch):
    make_state(tmp_path/"state.json")
    # Non-yt fixture media is deliberately not counted as real measured video.
    from holoclip import source_pattern_collection as runner
    original = verify_pending_source_visual

    def fake_visual(provider, **kwargs):
        return original(
            provider, measure=fake_measure, probe=fake_probe, **kwargs,
        )
    monkeypatch.setattr(runner, "verify_pending_source_visual", fake_visual)
    result = asyncio.run(collect_public_source_patterns(
        Provider(), store=tmp_path/"none.jsonl", state_path=tmp_path/"state.json",
        output=tmp_path/"media", policy_rules=[],
        cycles=2, max_clips=1, target_distinct_streams=3, discover=False,
    ))
    assert result["cycles_completed"] == 2
    assert result["status"] == "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS"
    assert result["new_unique_video_samples_this_run"] == 0
    assert result["distinct_candidate_streams_measured"] == 0
    assert all(row["new_unique_video_samples"] == 0
               for row in result["cycle_diagnostics"])
    assert result["controls"]["constructed_pattern_audit"][
        "real_measured_source_video_samples"] == 0
    assert result["target_reached"] is False
    assert result["qualification_allowed"] is False
    assert result["editor_cut_precision"] is None
    assert result["ready"] is False


def test_collector_rejects_bad_budget_before_any_download(tmp_path):
    make_state(tmp_path/"state.json")
    provider = Provider()
    with pytest.raises(ValueError):
        asyncio.run(collect_public_source_patterns(
            provider, store=tmp_path/"none.jsonl",
            state_path=tmp_path/"state.json", output=tmp_path/"media",
            policy_rules=[], cycles=21, discover=False,
        ))
    assert not provider.calls


def test_missing_queue_exits_after_single_zero_attempt(tmp_path):
    state = CorpusGrowthState()
    path = tmp_path/"state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    result = asyncio.run(collect_public_source_patterns(
        Provider(), store=tmp_path/"none.jsonl", state_path=path,
        output=tmp_path/"media", policy_rules=[],
        cycles=5, discover=False,
    ))
    assert result["cycles_completed"] == 1
    assert result["cycle_diagnostics"][0]["attempted_clips"] == 0
    assert result["status"].startswith("UNKNOWN_")
