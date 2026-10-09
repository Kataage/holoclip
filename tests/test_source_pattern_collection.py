"""Unattended public pattern collection is fair, bounded, and not gold."""
import asyncio
import copy

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.source_candidate_visual import verify_pending_source_visual
from holoclip.source_pattern_collection import collect_public_source_patterns
from test_source_candidate_visual import (
    Provider, fake_measure, fake_probe, make_state,
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


def test_each_distinct_original_candidate_receives_bounded_pattern_controls(
    tmp_path, monkeypatch,
):
    """A second original must not be masked by the first candidate's control."""
    from holoclip import source_candidate_visual as scout
    from holoclip import source_pattern_collection as collector
    from holoclip.source_visual_pattern_controls import _report, PATTERNS, APPROVED
    from holoclip.source_visual_pattern_report import report_saved_patterns
    from holoclip.video_measurement import checksum_file

    make_state(tmp_path / "state.json", candidates=[
        "https://www.youtube.com/watch?v=BBBBBBBBBBB",
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
    ])
    provider = Provider()
    monkeypatch.setattr(scout, "YtDlpSourceProvider", Provider)

    def fake_measured(media, root, *, asset_id, start, end, transcript, dense_times):
        data = fake_measure(
            media, root, asset_id=asset_id, start=start, end=end,
            transcript=transcript, dense_times=dense_times,
        )
        data.media_sha256 = checksum_file(media)
        return data

    tested = []

    def synthetic_pattern(window, *, root):
        tested.append(window.media_sha256)
        record = _report("MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE")
        record["source_video_sha256"] = window.media_sha256
        record["measured_frame_sha256s"] = ["a"*64, "b"*64, "c"*64]
        record["measured_frame_count"] = 3
        record["patterns"] = {
            name: {
                "expected": expected,
                "observed_status": (APPROVED if expected == "constructed_positive"
                                    else "UNKNOWN_NO_VISUAL_SEQUENCE"),
                "behaved_as_constructed": True,
            }
            for name, expected in PATTERNS.items()
        }
        return record

    monkeypatch.setattr(scout, "measure_source_video_controls",
                        lambda *args, **kwargs: {"status": "UNKNOWN_NO_CONTROL"})
    monkeypatch.setattr(scout, "measure_video_pattern_controls", synthetic_pattern)
    original = scout.verify_pending_source_visual

    def fake_verify(provider, **kwargs):
        return original(
            provider, measure=fake_measured, probe=fake_probe, **kwargs,
        )

    monkeypatch.setattr(collector, "verify_pending_source_visual", fake_verify)
    result = asyncio.run(collect_public_source_patterns(
        provider, store=tmp_path / "none.jsonl",
        state_path=tmp_path / "state.json", output=tmp_path / "media",
        policy_rules=[], cycles=1, discover=False,
        max_clips=1, max_candidates=2, target_distinct_streams=2,
    ))
    assert len(tested) == 2
    assert result["new_unique_video_samples_this_run"] == 2
    assert result["distinct_candidate_streams_measured"] == 2
    assert result["target_reached"] is True
    assert result["cycle_diagnostics"][0]["source_pattern_attempt_outcomes"] == {
        "pattern:MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE": 2
    }
    stored = report_saved_patterns(tmp_path / "state.json")
    assert stored["real_measured_source_video_samples"] == 2
    assert len(stored["per_source_pattern_results"]) == 2
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    observations = state.items["candidate"].source_scout["visual_probe"][
        "candidate_observations"
    ]
    assert len(observations) == 2
    assert all(row["constructed_pattern_observation"]["result"][
        "source_identity_verified"] is False for row in observations)
    assert result["ready"] is False


def test_first_pattern_execution_failure_does_not_starve_second_candidate(
    tmp_path, monkeypatch,
):
    from holoclip import source_candidate_visual as scout
    from holoclip.source_visual_pattern_controls import _report

    make_state(tmp_path / "state.json", candidates=[
        "https://www.youtube.com/watch?v=BBBBBBBBBBB",
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
    ])
    monkeypatch.setattr(scout, "YtDlpSourceProvider", Provider)
    monkeypatch.setattr(scout, "measure_source_video_controls",
                        lambda *args, **kwargs: {"status": "UNKNOWN_NO_CONTROL"})
    attempted = []

    def flaky(window, *, root):
        attempted.append(root)
        if len(attempted) == 1:
            raise RuntimeError("first artificial control failed")
        record = _report("UNKNOWN_MEASURED_VIDEO_HASH_OR_BUDGET")
        return record

    monkeypatch.setattr(scout, "measure_video_pattern_controls", flaky)
    report = scout.verify_pending_source_visual(
        Provider(), store=tmp_path / "none.jsonl",
        state_path=tmp_path / "state.json", output=tmp_path / "media",
        policy_rules=[], max_candidates=2,
        measure=fake_measure, probe=fake_probe,
    )
    assert report["attempted"] == 1
    assert len(attempted) == 2
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )
    observations = state.items["candidate"].source_scout["visual_probe"][
        "candidate_observations"
    ]
    assert observations[0]["constructed_pattern_error_class"] == "RuntimeError"
    assert observations[1]["constructed_pattern_observation"]["result"][
        "status"] == "UNKNOWN_MEASURED_VIDEO_HASH_OR_BUDGET"
    assert state.items["candidate"].source_stream_id is None
    assert report["source_identity_verified"] is False


def test_busy_collector_cli_returns_nonzero_for_scheduler(monkeypatch):
    from typer.testing import CliRunner
    from holoclip import cli as commands

    monkeypatch.setattr(commands, "YtDlpSourceProvider", lambda **_kwargs: object())
    monkeypatch.setattr(commands, "load_policy_rules", lambda _path: [])

    async def busy(_provider, **_kwargs):
        return {
            "status": "BLOCKED_CONCURRENT_COLLECTION",
            "ready": False, "qualification_allowed": False,
        }

    monkeypatch.setattr(commands, "run_locked_source_pattern_collection", busy)
    result = CliRunner().invoke(commands.app, [
        "examples-collect-source-patterns", "--no-discover",
    ])
    assert result.exit_code == 6
    assert "BLOCKED_CONCURRENT_COLLECTION" in result.stdout


def test_old_three_stream_target_does_not_silence_future_daily_runs(
    tmp_path, monkeypatch,
):
    """Previously recorded examples must not count toward a new run's quota."""
    from holoclip import source_pattern_collection as collector

    snapshots = [
        {"real_measured_source_video_samples": 3,
         "distinct_candidate_source_streams_measured": 3},
        {"real_measured_source_video_samples": 3,
         "distinct_candidate_source_streams_measured": 3},
        {"real_measured_source_video_samples": 3,
         "distinct_candidate_source_streams_measured": 3},
    ]
    calls = []

    def fake_report(_path):
        return {
            "constructed_pattern_audit": snapshots[0],
            "ready": False,
            "reviewed_gold_available": False,
        }

    def fake_saved(_path):
        return snapshots[0]

    def fake_verify(_provider, **_kwargs):
        calls.append(1)
        return {
            "attempted": 1, "counts": {"UNKNOWN_NO_VISUAL_MATCH": 1},
            "attempted_clip_keys": [],
        }

    monkeypatch.setattr(collector, "report_saved_source_controls", fake_report)
    monkeypatch.setattr(collector, "report_saved_patterns", fake_saved)
    monkeypatch.setattr(collector, "verify_pending_source_visual", fake_verify)
    options = dict(
        store=tmp_path / "clips.jsonl", state_path=tmp_path / "state.json",
        output=tmp_path / "media", policy_rules=[], discover=False,
        cycles=2, target_distinct_streams=3,
    )
    day1 = asyncio.run(collect_public_source_patterns(object(), **options))
    day2 = asyncio.run(collect_public_source_patterns(object(), **options))
    assert len(calls) == 4
    for report in (day1, day2):
        assert report["cycles_completed"] == 2
        assert report["status"] == "UNKNOWN_NO_NEW_PUBLIC_VIDEO_PATTERN_MEASUREMENTS"
        assert report["observed_source_samples_before"] == 3
        assert report["distinct_candidate_streams_before"] == 3
        assert report["distinct_candidate_streams_measured"] == 3
        assert report["new_distinct_candidate_streams_this_run"] == 0
        assert report["new_unique_video_samples_this_run"] == 0
        assert report["target_scope"] == "this_run_new_distinct_candidate_streams"
        assert report["target_reached"] is False
        assert report["ready"] is False


def test_fresh_stream_quota_is_delta_not_lifetime(tmp_path, monkeypatch):
    from holoclip import source_pattern_collection as collector

    snapshots = [
        {"real_measured_source_video_samples": 8,
         "distinct_candidate_source_streams_measured": 5},
        {"real_measured_source_video_samples": 9,
         "distinct_candidate_source_streams_measured": 6},
        {"real_measured_source_video_samples": 10,
         "distinct_candidate_source_streams_measured": 7},
    ]
    audit_calls = 0
    read_calls = 0
    attempted = []

    def controls(_path):
        nonlocal audit_calls
        index = 0 if audit_calls == 0 else min(read_calls, 2)
        audit_calls += 1
        return {"constructed_pattern_audit": snapshots[index], "ready": False}

    def patterns(_path):
        nonlocal read_calls
        index = min(read_calls, 2)
        read_calls += 1
        return snapshots[index]

    def verify(_provider, **_kwargs):
        attempted.append(1)
        return {"attempted": 1, "counts": {}, "attempted_clip_keys": []}

    monkeypatch.setattr(collector, "report_saved_source_controls", controls)
    monkeypatch.setattr(collector, "report_saved_patterns", patterns)
    monkeypatch.setattr(collector, "verify_pending_source_visual", verify)
    outcome = asyncio.run(collect_public_source_patterns(
        object(), store=tmp_path / "clips", state_path=tmp_path / "state",
        output=tmp_path / "video", policy_rules=[], discover=False,
        cycles=5, target_distinct_streams=2,
    ))
    assert len(attempted) == 2  # not a lifetime 2-stream total, not all 5
    assert outcome["new_unique_video_samples_this_run"] == 2
    assert outcome["new_distinct_candidate_streams_this_run"] == 2
    assert outcome["target_reached"] is True
    assert outcome["cycles_completed"] == 2
    assert outcome["status"] == (
        "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS"
    )
    assert outcome["ready"] is False


def test_histories_shrinking_during_run_cannot_produce_positive_audit(
    tmp_path, monkeypatch,
):
    from holoclip import source_pattern_collection as collector

    old = {"real_measured_source_video_samples": 4,
           "distinct_candidate_source_streams_measured": 4}
    new = {"real_measured_source_video_samples": 3,
           "distinct_candidate_source_streams_measured": 3}
    monkeypatch.setattr(collector, "report_saved_source_controls",
                        lambda _path: {"constructed_pattern_audit": old})
    snapshots = iter([old, new])
    monkeypatch.setattr(collector, "report_saved_patterns",
                        lambda _path: next(snapshots))
    monkeypatch.setattr(collector, "verify_pending_source_visual",
                        lambda *_args, **_kwargs: {
                            "attempted": 1, "counts": {},
                            "attempted_clip_keys": [],
                        })
    with pytest.raises(ValueError, match="decreased"):
        asyncio.run(collect_public_source_patterns(
            object(), store=tmp_path / "clips", state_path=tmp_path / "state",
            output=tmp_path / "media", policy_rules=[], discover=False,
            cycles=1,
        ))
