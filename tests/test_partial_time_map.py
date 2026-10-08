"""Partial source/output timing candidates cannot become exact editing maps."""
import json
from pathlib import Path

import pytest

from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_partial_time_map_pipeline import build_reserved_partial_maps
from holoclip.partial_time_map import construct_partial_time_map
from test_editorial_profile import trusted_example
from test_multimodal_cut_sides import _pipeline_fixture, _run as run_sides


def anchor(start=0., end=6., offset=40.):
    return {"clip_start": start, "clip_end": end,
            "source_start": start + offset, "source_end": end + offset,
            "correlation": .95, "uniqueness_margin": .12}


def frame(t, *, offset=40., name=None):
    label = str(name if name is not None else t)
    return {
        "finished_clip_time_estimate_seconds": t,
        "source_audio_clock_estimate_seconds": round(t + offset, 3),
        "expected_source_from_audio_seconds": round(t + offset, 3),
        "audio_visual_residual_seconds": 0.,
        "finished_frame_id": f"f-{label}",
        "source_frame_id": f"s-{label}",
        "score": .75, "orb_inliers": 30,
    }


def group(links, index=0):
    return {
        "group": index, "status": "PARTIAL_AUDIO_VISUAL_FRAME_LINKS",
        "original_absolute_frame_pts_verified": False,
        "frame_accurate_cuts_verified": False,
        "dense_output_source_map_verified": False,
        "frame_links": links,
    }


def test_two_audio_backed_distinct_frames_define_only_local_candidate():
    result = construct_partial_time_map(
        [group([frame(.5), frame(2.5), frame(8.5), frame(10.5)])],
        [anchor(), anchor(8., 12.)], [],
        finished_duration=15.,
    )
    assert result["status"] == "PARTIAL_LOCAL_INTERVAL_CANDIDATES"
    assert len(result["segments"]) == 2
    assert [(p["output_start"], p["output_end"]) for p in result["segments"]] == [
        (.5, 2.5), (8.5, 10.5),
    ]
    assert all(p["status"] == "LOCAL_INTERPOLATION_CANDIDATE" for p in result["segments"])
    assert all(p["valid_for_edit_execution"] is False for p in result["segments"])
    assert all(p["valid_for_training_gold"] is False for p in result["segments"])
    assert result["candidate_coverage_seconds"] == 4.
    assert result["candidate_coverage_fraction"] < .3
    assert result["unknown_intervals"] == [
        {"start": 0., "end": .5, "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT"},
        {"start": 2.5, "end": 8.5, "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT"},
        {"start": 10.5, "end": 15., "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT"},
    ]
    assert result["interpolation_verified"] is False
    assert result["dense_output_source_map_verified"] is False
    assert result["frame_accurate_cuts_verified"] is False
    assert result["qualification_allowed"] is False


def test_audio_cut_intersection_forbids_interpolation_even_with_matching_images():
    a = group([frame(.5), frame(2.5)])
    prohibited = [{
        "status": "CUT_INTERVAL_CANDIDATE", "interval": [1.5, 1.8],
    }]
    result = construct_partial_time_map(
        [a], [anchor()], prohibited, finished_duration=15.,
    )
    assert result["status"] == "UNKNOWN_NO_SUPPORTED_LOCAL_INTERVALS"
    assert result["segments"] == []
    assert result["unknown_intervals"] == [
        {"start": 0., "end": 15., "reason": "UNOBSERVED_OR_UNSUPPORTED_OUTPUT"}
    ]
    assert result["rejected"]["intersects_cut_or_unknown"] == 1


def test_unknown_gap_no_unique_audio_or_speed_change_cannot_be_covered():
    pts = [frame(.5), frame(2.5)]
    no_audio = construct_partial_time_map([group(pts)], [], [], finished_duration=10.)
    assert no_audio["segments"] == []
    assert no_audio["rejected"]["no_audio_support"] == 1
    gap = construct_partial_time_map(
        [group([frame(.5), frame(4.5)])], [anchor()], [], finished_duration=10.,
    )
    assert gap["segments"] == []
    assert gap["rejected"]["wide_or_unobserved_gap"] == 1
    speed = construct_partial_time_map(
        [group([frame(.5), frame(2.5, offset=41.)])],
        [anchor()], [], finished_duration=10.,
    )
    assert speed["segments"] == []
    assert speed["rejected"]["time_or_speed_disagreement"] == 1


def test_duplicate_source_point_and_negative_timing_and_unknown_status_rejected():
    repeated = [frame(.5), frame(2.5)]
    repeated[1]["source_frame_id"] = repeated[0]["source_frame_id"]
    result = construct_partial_time_map(
        [group(repeated)], [anchor()], [], finished_duration=10.,
    )
    assert result["rejected"]["duplicate_source_frames"] == 1
    assert result["segments"] == []
    unknown = construct_partial_time_map(
        [{**group([frame(.5), frame(2.5)]), "status": "UNKNOWN_TOO_SPARSE"}],
        [anchor()], [], finished_duration=10.,
    )
    assert unknown["segments"] == []
    assert unknown["rejected"]["unreviewed_visual_status"] == 1
    malformed = frame(.5)
    malformed["finished_clip_time_estimate_seconds"] = float("nan")
    invalid = construct_partial_time_map(
        [group([malformed, frame(2.5)])], [anchor()], [], finished_duration=10.,
    )
    assert invalid["segments"] == []
    assert invalid["rejected"]["invalid_sample"] == 1
    with pytest.raises(ValueError):
        construct_partial_time_map([group([])], [anchor()],
                                   [{"interval": [6, 2]}], finished_duration=10.)


def test_work_budget_fails_closed_and_emits_no_partial_intervals():
    points = [frame(.5), frame(1.1), frame(1.7)]
    result = construct_partial_time_map(
        [group(points)], [anchor()], [], finished_duration=10.,
        max_pairs=1,
    )
    assert result["status"] == "UNKNOWN_PAIR_BUDGET"
    assert result["segments"] == []
    assert result["unknown_intervals"] == [
        {"start": 0., "end": 10., "reason": "PAIR_BUDGET_EXHAUSTED"}
    ]


def fixture(tmp_path):
    provider, store, state, prepared, audio_root = _pipeline_fixture(tmp_path)
    assert run_sides(
        provider, store, state, prepared, audio_root,
        compare=lambda *args, **kwargs: {
            "status": "UNKNOWN_OUTPUT_VISUAL_JUMP", "verified_edit_cut": False,
            "frame_accurate_cuts_verified": False,
        },
    )["counts"] == {"MEASURED_UNREVIEWED_CUT_SIDE_EVIDENCE": 1}
    return provider, store, state, prepared, audio_root


def run(provider, store, state, prepared, audio_root, **kwargs):
    return build_reserved_partial_maps(
        provider, store=store, state_path=state, prepared_root=prepared,
        audio_root=audio_root, pairs_root=prepared / "source_pairs",
        clocks_root=prepared / "source_clocks",
        frame_links_root=prepared / "frame_links",
        cuts_root=prepared / "cut_brackets",
        cut_sides_root=prepared / "cut_sides",
        output=prepared / "partial_time_maps",
        policy_rules=kwargs.pop("policy_rules", []), max_clips=2, **kwargs,
    )


def test_guarded_map_persistence_with_zero_prod_or_gold_and_resume(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    built = []
    def measure(*args, **kwargs):
        built.append(1)
        return construct_partial_time_map(*args, **kwargs)
    first = run(provider, store, state, prepared, audio_root, build=measure)
    assert first["counts"] == {"MEASURED_UNREVIEWED_PARTIAL_MAP": 1}
    path = prepared / "partial_time_maps" / "holdout-partial-time-map.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "qualification_allowed", "benchmark_held_out", "event_split_verified",
        "frame_accurate_cuts_verified", "reviewed_gold_available",
        "dense_output_source_map_verified", "original_video_absolute_pts_verified",
    ):
        assert report[key] is False
    entry = next(iter(report["records"].values()))
    assert entry["time_map"]["status"] == "PARTIAL_LOCAL_INTERVAL_CANDIDATES"
    assert entry["time_map"]["segments"][0]["output_start"] == .5
    assert entry["time_map"]["segments"][0]["output_end"] == 2.5
    assert entry["time_map"]["segments"][0]["valid_for_edit_execution"] is False
    assert run(provider, store, state, prepared, audio_root, build=measure)[
        "counts"
    ] == first["counts"]
    assert len(built) == 1


def test_corruption_and_learning_overlap_block_before_any_network(tmp_path):
    _, store, state, prepared, audio_root = fixture(tmp_path)
    pair = next(iter(json.loads(
        (prepared / "source_pairs" / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )["records"].values()))["pairs"][0]
    media = Path(pair["source_video_path"])
    media.write_bytes(b"tampered original")
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Corrupt or leaked evidence must not hit provider")
    bad = run(NoNetwork(), store, state, prepared, audio_root)
    assert bad["attempted"] == 0
    assert bad["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}
    upsert_human_clip_examples(
        store, [trusted_example("leak", stream_id="stream-evaluation")],
    )
    leaked = run(NoNetwork(), store, state, prepared, audio_root)
    assert leaked["attempted"] == 0
    assert leaked["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_revoked_policy_and_forged_upstream_reference_fail_closed(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    forbidden = ClipPolicyRule(
        id="revoked-source-range", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=45, end=48,
        reason="previously allowed section now forbidden",
    )
    denied = run(
        provider, store, state, prepared, audio_root, policy_rules=[forbidden],
    )
    assert denied["counts"] == {"QUARANTINED_POLICY": 1}
    path = prepared / "cut_sides" / "holdout-cut-side-evidence.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    next(iter(report["records"].values()))["fingerprint"] = "wrong-hash"
    path.write_text(json.dumps(report), encoding="utf-8")
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Hash-conflicted report must fail before network")
    blocked = run(NoNetwork(), store, state, prepared, audio_root)
    assert blocked["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}


def test_map_builder_rejects_forged_qualified_flags(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    path = prepared / "frame_links" / "holdout-frame-links.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["qualification_allowed"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unpromoted"):
        run(provider, store, state, prepared, audio_root)


def test_stale_source_visual_side_fingerprint_blocks_partial_map(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    path = prepared / "cut_sides" / "holdout-cut-side-evidence.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = next(iter(payload["records"].values()))
    record["visual_only_frame_search_version"] = "obsolete-visual-search-method"
    path.write_text(json.dumps(payload), encoding="utf-8")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Stale original visual method must fail before metadata")

    report = run(NoNetwork(), store, state, prepared, audio_root)
    assert report["counts"] == {"BLOCKED_UPSTREAM_PROVENANCE": 1}
